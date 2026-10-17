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

from qgis.core import (
    QgsGeometry, QgsPointXY, QgsRectangle, QgsWkbTypes,
)

from .base import (
    Finding, CODE_GAP, _cancelled, _progress, _opts, _geoms,
    candidate_fids,
)

# целевое число фич в объединении одной плитки (10 000 -> 5x5 плиток)
_TILE_TARGET = 500
_TILE_MAX_DIM = 16
# минимальная длина общей границы, чтобы фича считалась соседом щели
_SHARED_MIN_LEN = 1e-9


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
        except Exception:
            return
        seam_ratio = float(_opts(ctx, "seam_min_ratio") or 0.0)
        gap_max = float(_opts(ctx, "gap_max_area") or 0.0)
        for part in parts:
            for ring in part[1:]:
                self._check_hole(ctx, ring, lookup, accepted, out,
                                 seam_ratio, gap_max)

    def _check_hole(self, ctx, ring, lookup, accepted, out,
                    seam_ratio, gap_max):
        if len(ring) < 4:
            return
        try:
            hole = QgsGeometry.fromPolygonXY([ring])
        except Exception:
            return
        if hole.isNull() or hole.isEmpty():
            return
        area = hole.area()
        if area <= 0.0:
            return
        rep = self._rep_point(hole, ring)
        if rep is None:
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
            return
        try:
            local_union = QgsGeometry.unaryUnion(local)
        except Exception:
            return
        if local_union is None or local_union.isNull():
            return
        try:
            if local_union.contains(QgsGeometry.fromPointXY(rep)):
                return  # дыру закрывает фича вне плитки — не щель
        except Exception:
            return
        # соседи по общей границе
        try:
            hole_border = hole.boundary()
        except Exception:
            return
        if hole_border is None or hole_border.isNull() \
                or hole_border.isEmpty():
            return
        adj = []  # (длина общей границы, fid)
        for fid in sorted(set(candidate_fids(ctx, rect))):
            g = lookup.get(fid)
            if g is None:
                continue
            try:
                border = g.boundary()
                if border is None or border.isNull():
                    continue
                shared = border.intersection(hole_border)
                length = shared.length() if shared is not None \
                    and not shared.isNull() else 0.0
            except Exception:
                continue
            if length > _SHARED_MIN_LEN:
                adj.append((length, fid))
        if len(adj) < 2:
            return  # дегенерация или собственная дыра фичи
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
        """Представительская точка щели (строго внутри)."""
        cand = []
        try:
            cand.append(hole.pointOnSurface())
        except Exception:
            pass
        try:
            cand.append(hole.centroid())
        except Exception:
            pass
        for p in cand:
            if p is None:
                continue
            try:
                if math.isnan(p.x()) or math.isnan(p.y()):
                    continue
                if hole.contains(QgsGeometry.fromPointXY(
                        QgsPointXY(p))):
                    return QgsPointXY(p)
            except Exception:
                continue
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
