# -*- coding: utf-8 -*-
"""Проверка GAP: щель между полигонами (этап 5 ТЗ).

Щели — внутренние кольца объединения полигонов слоя; объединение
считается ПО ПЛИТКАМ (unaryUnion на подмножестве фич плитки — бюджет
не зависит от размера слоя, ТЗ этап 5 п. 4). Фичи в объединение идут
ЦЕЛИКОМ (не обрезанные плиткой), поэтому щель, окружённая фичами
плитки, всегда видна как целая; одна и та же щель может найтись в
нескольких плитках — дедупликация по представительной точке.

Кандидат проверяется:
  * покрытием локальным запросом (все фичи габарита щели объединяются
    заново — представительская точка не должна покрыться: отсекает
    «щели», у которых дыру на самом деле закрывает фича, не попавшая
    в плитку);
  * числом соседей по общей границе: 0 — дегенерация, 1 — собственная
    дыра фичи (не ошибка), 2+ — щель между полигонами;
  * порогами: площадь <= SEAM_MIN_RATIO × площадь меньшего соседа —
    «шов» стыковки, игнорируется; <= GAP_MAX_AREA — исправимо
    («приклеить»), больше — только показ.

Исправление: геометрия щели объединяется (union) с соседом, у которого
ДЛИНЬШЕ общая граница со щелью; площадь соседа вырастает ровно на
площадь щели. Payload: {"gap_geom", "neighbor_fid"}.
"""

import math
import os

from qgis.core import (
    QgsGeometry, QgsPointXY, QgsRectangle, QgsWkbTypes,
)

from .base import (
    Finding, CODE_GAP, _cancelled, _progress, _opts, _polys, _geoms,
    candidate_fids, as_point,
)

# целевое число фич в объединении одной плитки (10 000 -> 5x5 плиток)
_TILE_TARGET = 500
_TILE_MAX_DIM = 16
# минимальная длина общей границы, чтобы фича считалась соседом щели
_SHARED_MIN_LEN = 1e-9


def _dbg(message):
    """Диагностика GAP под env-флагом (в CI виден captured stdout)."""
    if os.environ.get("TOPOPOLEDIT_GAP_DEBUG") == "1":
        print("[topopolyedit][gap] {}".format(message))


def _rect_empty(r):
    """Пустой габарит (устойчиво к упрощённым стабам QgsRectangle)."""
    if r is None:
        return True
    try:
        if r.isEmpty():
            return True
    except Exception:
        pass
    try:
        return r.width() <= 0.0 or r.height() <= 0.0
    except Exception:
        return False


def _seg_overlap_len(a1, a2, b1, b2, eps):
    """Длина перекрытия коллинеарных сегментов (a1,a2) и (b1,b2).

    Сегменты считаются лежащими на одной прямой, если концы b
    отстоят от прямой a не дальше eps; перекрытие — по проекциям
    на направление a. Неколлинеарные — 0."""
    dx = a2.x() - a1.x()
    dy = a2.y() - a1.y()
    length = math.hypot(dx, dy)
    if length <= eps:
        return 0.0
    cross1 = dx * (b1.y() - a1.y()) - dy * (b1.x() - a1.x())
    cross2 = dx * (b2.y() - a1.y()) - dy * (b2.x() - a1.x())
    if abs(cross1) > eps * length or abs(cross2) > eps * length:
        return 0.0
    t1 = ((b1.x() - a1.x()) * dx + (b1.y() - a1.y()) * dy) \
        / (length * length)
    t2 = ((b2.x() - a1.x()) * dx + (b2.y() - a1.y()) * dy) \
        / (length * length)
    lo = max(min(t1, t2), 0.0)
    hi = min(max(t1, t2), 1.0)
    if hi <= lo:
        return 0.0
    return (hi - lo) * length


def _ring_segments(ring):
    """Сегменты замкнутого/незамкнутого списка точек."""
    out = []
    for i in range(len(ring) - 1):
        out.append((ring[i], ring[i + 1]))
    return out


def _shared_boundary_len(polys, hole_ring, eps):
    """Суммарная длина колец структуры polys, лежащая НА границе щели
    (коллинеарные перекрытия сегментов; дубли границ дают сумму —
    для выбора соседа это не критично, у смежных фич она одна)."""
    if not polys:
        return 0.0
    border = _ring_segments(hole_ring)
    if not border:
        return 0.0
    total = 0.0
    for _pi, _ri, ring in _iter_rings_g(polys):
        for a1, a2 in _ring_segments(ring):
            for b1, b2 in border:
                ov = _seg_overlap_len(a1, a2, b1, b2, eps)
                if ov > 0.0:
                    total += ov
    return total


def _iter_rings_g(polys):
    for part in polys or []:
        for ring in part:
            yield None, None, ring


class GapCheck(object):
    """Щели = дыры объединения полигонов, окружённые 2+ фичами."""

    CODE = CODE_GAP

    def run(self, ctx):
        out = []
        geoms = [(fid, g) for fid, g in _geoms(ctx)
                 if g is not None and not g.isNull() and not g.isEmpty()
                 and g.type() == QgsWkbTypes.PolygonGeometry]
        if len(geoms) < 2:
            return out
        _progress(ctx, 0.0, u"GAP: подготовка плиток")
        tiles = self._tiles(ctx, geoms)
        accepted = []  # (rep_point, area) уже найденных щелей
        total = max(len(tiles), 1)
        for i, rect in enumerate(tiles):
            if _cancelled(ctx):
                return out
            self._run_tile(ctx, rect, accepted, out)
            _progress(ctx, (i + 1) / float(total),
                      u"GAP: объединение полигона {}/{}".format(
                          i + 1, total))
        return out

    # ------------------------------------------------------------------
    # Плитки
    # ------------------------------------------------------------------

    def _tiles(self, ctx, geoms):
        """Сетка плиток: dim x dim по габариту слоя; dim растёт с числом
        фич (~_TILE_TARGET фич на плитку)."""
        bbox = None
        for _fid, g in geoms:
            r = g.boundingBox()
            if bbox is None:
                bbox = QgsRectangle(r)
            else:
                bbox.combineExtentWith(r)
        if bbox is None or _rect_empty(bbox):
            return []
        n = len(geoms)
        dim = int(math.ceil(math.sqrt(n / float(_TILE_TARGET))))
        dim = max(1, min(dim, _TILE_MAX_DIM))
        w = bbox.width() / dim
        h = bbox.height() / dim
        tiles = []
        for j in range(dim):
            for i in range(dim):
                x0 = bbox.xMinimum() + i * w
                y0 = bbox.yMinimum() + j * h
                x1 = x0 + (w if i < dim - 1 else bbox.width() - i * w)
                y1 = y0 + (h if j < dim - 1 else bbox.height() - j * h)
                tiles.append(QgsRectangle(x0, y0, x1, y1))
        return tiles

    # ------------------------------------------------------------------
    # Обработка одной плитки
    # ------------------------------------------------------------------

    def _run_tile(self, ctx, rect, accepted, out):
        fids = candidate_fids(ctx, rect)
        if len(fids) < 2:
            return
        lookup = dict(_geoms(ctx))
        geoms = []
        for fid in sorted(set(fids)):
            g = lookup.get(fid)
            if g is None:
                continue
            # невалидные геометрии не участвуют в объединении: GEOS
            # может уронить unaryUnion и обесценить всю плитку
            try:
                if not g.isGeosValid():
                    continue
            except Exception:
                pass
            geoms.append(g)
        if len(geoms) < 2:
            return
        try:
            union = QgsGeometry.unaryUnion(geoms)
        except Exception:
            return
        if union is None or union.isNull() or union.isEmpty():
            return
        try:
            parts = (union.asMultiPolygon() if union.isMultipart()
                     else [union.asPolygon()])
        except Exception as exc:
            _dbg(u"плитка: asMultiPolygon сбой: {}".format(exc))
            return
        n_holes = sum(len(part) - 1 for part in parts)
        if n_holes:
            _dbg(u"плитка: {} частей, {} дыр".format(len(parts),
                                                     n_holes))
        seam_ratio = float(_opts(ctx, "seam_min_ratio") or 0.0)
        gap_max = float(_opts(ctx, "gap_max_area") or 0.0)
        for part in parts:
            for ring in part[1:]:
                self._check_hole(ctx, ring, lookup, accepted, out,
                                 seam_ratio, gap_max)

    def _check_hole(self, ctx, ring, lookup, accepted, out,
                    seam_ratio, gap_max):
        _dbg(u"_check_hole: кольцо из {} точек".format(len(ring or [])))
        if len(ring) < 4:
            return
        try:
            hole = QgsGeometry.fromPolygonXY([ring])
        except Exception as exc:
            _dbg(u"_check_hole: fromPolygonXY сбой: {}".format(exc))
            return
        if hole.isNull() or hole.isEmpty():
            _dbg(u"_check_hole: дыра пуста/null")
            return
        area = hole.area()
        _dbg(u"_check_hole: площадь {:g}".format(area))
        if area <= 0.0:
            return
        rep = self._rep_point(hole, ring)
        if rep is None:
            _dbg(u"дыра {:g}: нет представительской точки".format(area))
            return
        # дедупликация: представительская точка уже внутри найденной
        for acc_pt, acc_geom in accepted:
            try:
                if acc_geom.contains(QgsGeometry.fromPointXY(rep)):
                    return
            except Exception:
                continue
        # локальная проверка покрытия: union ВСЕХ фич габарита щели
        rect = QgsRectangle(hole.boundingBox())
        rect.grow(max(1e-9, math.sqrt(area)))
        local = []
        for fid in sorted(set(candidate_fids(ctx, rect))):
            g = lookup.get(fid)
            if g is None:
                continue
            try:
                if not g.isGeosValid():
                    continue  # невалидные не участвуют (GEOS-стойкость)
            except Exception:
                pass
            local.append(g)
        if len(local) < 2:
            _dbg(u"дыра {:g}: локальных фич {}".format(area, len(local)))
            return
        try:
            local_union = QgsGeometry.unaryUnion(local)
        except Exception as exc:
            _dbg(u"_check_hole: local_union сбой: {}".format(exc))
            return
        if local_union is None or local_union.isNull():
            _dbg(u"_check_hole: local_union null")
            return
        try:
            if local_union.contains(QgsGeometry.fromPointXY(rep)):
                _dbg(u"_check_hole: дыра {:g} ПОКРЫТА локальным "
                     u"объединением — не щель".format(area))
                return  # дыру закрывает фича вне плитки — не щель
        except Exception as exc:
            _dbg(u"_check_hole: contains сбой: {}".format(exc))
            return
        # соседи по общей границе: сумма коллинеарных перекрытий
        # сегментов колец фичи с границей щели (без GEOS boundary() —
        # он давал тихие сбои в CI; чистая математика над кольцами)
        eps = float(_opts(ctx, "match_eps") or 1e-6)
        adj = []  # (длина общей границы, fid)
        for fid in sorted(set(candidate_fids(ctx, rect))):
            g = lookup.get(fid)
            if g is None:
                continue
            try:
                length = _shared_boundary_len(_polys(ctx, fid, g),
                                              ring, eps)
            except Exception as exc:
                _dbg(u"дыра: shared #{} сбой: {}".format(fid, exc))
                continue
            if length > _SHARED_MIN_LEN:
                adj.append((length, fid))
        if len(adj) < 2:
            _dbg(u"дыра {:g}: соседей по границе {} (нужно 2+)"
                 .format(area, len(adj)))
            return  # дегенерация или собственная дыра фичи
        _dbg(u"дыра {:g}: соседи {}".format(
            area, [(round(l, 4), f) for l, f in adj]))
        min_adj_area = min(lookup[f].area() for _l, f in adj
                           if f in lookup) if adj else 0.0
        if seam_ratio > 0.0 and area <= seam_ratio * max(min_adj_area,
                                                        1e-30):
            return  # «шов» стыковки — игнорируется
        adj.sort(key=lambda t: (-t[0], t[1]))
        neighbor_fid = adj[0][1]
        shared_len = adj[0][0]
        fixable = bool(gap_max > 0.0 and area <= gap_max)
        accepted.append((rep, hole))
        out.append(self._finding(ctx, neighbor_fid, hole, area,
                                 shared_len, len(adj), fixable,
                                 gap_max))

    # ------------------------------------------------------------------
    # Утилиты
    # ------------------------------------------------------------------

    @staticmethod
    def _rep_point(hole, ring):
        """Представительская точка щели (строго внутри).

        pointOnSurface()/centroid() в PyQGIS возвращают QgsGeometry
        (точку-геометрию) — нормализуем через base.as_point()."""
        cand = []
        try:
            cand.append((u"pointOnSurface", hole.pointOnSurface()))
        except Exception as exc:
            _dbg(u"rep: pointOnSurface сбой: {}".format(exc))
        try:
            cand.append((u"centroid", hole.centroid()))
        except Exception as exc:
            _dbg(u"rep: centroid сбой: {}".format(exc))
        for name, obj in cand:
            p = as_point(obj)
            if p is None:
                _dbg(u"rep: {} -> нет точки".format(name))
                continue
            try:
                ok = hole.contains(QgsGeometry.fromPointXY(p))
                _dbg(u"rep: {} = ({:g},{:g}) contains={}"
                     .format(name, p.x(), p.y(), ok))
                if ok:
                    return p
            except Exception as exc:
                _dbg(u"rep: {} contains сбой: {}".format(name, exc))
                continue
        if ring:
            _dbg(u"rep: кольцо {} точек, первая=({:g},{:g}) "
                 u"последняя=({:g},{:g})".format(
                     len(ring), ring[0].x(), ring[0].y(),
                     ring[-1].x(), ring[-1].y()))
        return None

    @staticmethod
    def _finding(ctx, neighbor_fid, hole, area, shared_len, n_adj,
                 fixable, gap_max):
        rep = GapCheck._rep_point(hole, hole.asPolygon()
                                  if not hole.isMultipart()
                                  else hole.asMultiPolygon()[0][0])
        msg = (u"щель площадью {:g} между {} полигонами; сосед для "
               u"клейки: #{} (общая граница {:g})".format(
                   area, n_adj, neighbor_fid, shared_len))
        if not fixable:
            msg += u"; больше порога {:g} — только показ".format(gap_max)
        return Finding(
            CODE_GAP, fid=neighbor_fid, point=rep, area=area,
            message=msg, fixable=fixable,
            payload={"neighbor_fid": neighbor_fid, "gap_geom": hole,
                     "gap_area": area},
            layer_id=ctx.get("layer_id", u""),
            layer_name=ctx.get("layer_name", u""))
