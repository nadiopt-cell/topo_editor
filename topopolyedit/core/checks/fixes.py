# -*- coding: utf-8 -*-
"""Применение исправлений аудита (этап 5 ТЗ): payload находки ->
ChangeSet {fid: QgsGeometry}.

Каждая функция берёт АКТУАЛЬНЫЕ геометрии слоя (движок перечитывает
их перед исправлением) и возвращает словарь изменений; несоответствие
данным (находка устарела) — исключение FixRefused с понятным текстом,
а не тихий пропуск. Валидация и запись ChangeSet — в layer_audit.py
(FixEngine): ядро не знает ни про UI, ни про стеки отмены.

Модуль зависит только от qgis.core.
"""

from qgis.core import QgsGeometry, QgsPointXY

from ...geo_utils import (
    norm_polys, polys_to_geom, replace_vertices, insert_vertex_topo,
)


class FixRefused(Exception):
    """Исправление отказано: находка устарела или неприменима."""


# -------------------------------------------------------------------------
# Утилиты
# -------------------------------------------------------------------------

def _norm(geom):
    """(polys, was_multi) или FixRefused, если разбор не удался."""
    try:
        res = norm_polys(geom)
    except Exception as exc:
        raise FixRefused(u"геометрия не разобрана: {}".format(exc))
    if res is None:
        raise FixRefused(u"геометрия не является полигоном")
    return res


def _closed(ring):
    """Замыкает кольцо копией первой вершины, если оно не замкнуто."""
    out = [QgsPointXY(p) for p in ring]
    if len(out) >= 2 and (out[0].x() != out[-1].x()
                          or out[0].y() != out[-1].y()):
        out.append(QgsPointXY(out[0]))
    return out


def _polygon_only(g):
    """Полигональная часть результата (локально, без импорта
    topo_editor — ядро остаётся лёгким)."""
    if g is None or g.isNull() or g.isEmpty():
        return None
    try:
        from qgis.core import QgsWkbTypes
        if g.type() == QgsWkbTypes.PolygonGeometry:
            return g
        parts = [p for p in g.asGeometryCollection()
                 if p.type() == QgsWkbTypes.PolygonGeometry
                 and not p.isEmpty()]
    except Exception:
        return None
    if not parts:
        return None
    if len(parts) == 1:
        return parts[0]
    try:
        u = QgsGeometry.unaryUnion(parts)
    except Exception:
        return None
    if u is None or u.isNull() or u.isEmpty():
        return None
    return u


# -------------------------------------------------------------------------
# DUP_NODE: чистка подряд идущих дублей одной фичи
# -------------------------------------------------------------------------

def clean_dup_nodes(layer_geoms, fid, match_eps):
    """Удаляет ВСЕ подряд идущие дубли вершин фичи fid.

    Замыкание колец сохраняется; кольцо, вырождающееся после чистки,
    возвращается исходным — валидатор решает (обычно это честный отказ).
    """
    g = layer_geoms.get(fid)
    if g is None:
        raise FixRefused(u"фича #{} не найдена (данные изменились)"
                         .format(fid))
    polys, was_multi = _norm(g)
    eps2 = match_eps * match_eps
    new_polys = []
    removed = 0
    for part in polys:
        new_part = []
        for ring in part:
            out = []
            for p in ring:
                if out and p.sqrDist(out[-1]) <= eps2:
                    removed += 1
                    continue
                out.append(QgsPointXY(p))
            out = _closed(out)
            if len(out) < 4:
                # кольцо выродилось — оставить как было (валидатор
                # откажет исправлению с понятным текстом)
                new_part.append([QgsPointXY(p) for p in ring])
            else:
                new_part.append(out)
        new_polys.append(new_part)
    if not removed:
        raise FixRefused(u"дубли вершин уже исчезли (данные изменились)")
    return {fid: polys_to_geom(new_polys, was_multi)}


# -------------------------------------------------------------------------
# SPIKE: удалить вершину во всех полигонах, где она есть
# -------------------------------------------------------------------------

def remove_spike_vertex(layer_geoms, pos, match_eps):
    """Удаляет вершину pos (в пределах match_eps) ВО ВСЕХ фичах слоя.

    Кольцо, в котором после удаления осталось бы меньше 4 точек
    (треугольник), — отказ: шип на треугольнике исправлению не
    подлежит."""
    eps2 = match_eps * match_eps
    tgt = QgsPointXY(pos[0], pos[1])
    out = {}
    for fid in sorted(layer_geoms.keys()):
        polys, was_multi = _norm(layer_geoms[fid])
        new_polys = []
        hit = False
        for part in polys:
            new_part = []
            for ring in part:
                if not any(v.sqrDist(tgt) <= eps2 for v in ring):
                    new_part.append([QgsPointXY(p) for p in ring])
                    continue
                hit = True
                nr = [QgsPointXY(p) for p in ring
                      if p.sqrDist(tgt) > eps2]
                if len(nr) < 3:
                    raise FixRefused(
                        u"фича #{}: кольцо выродилось бы после удаления "
                        u"шипа — исправьте вручную".format(fid))
                nr = _closed(nr)
                if len(nr) < 4:
                    raise FixRefused(
                        u"фича #{}: кольцо выродилось бы после удаления "
                        u"шипа — исправьте вручную".format(fid))
                new_part.append(nr)
            new_polys.append(new_part)
        if hit:
            out[fid] = polys_to_geom(new_polys, was_multi)
    if not out:
        raise FixRefused(u"вершина шипа не найдена (данные изменились)")
    return out


# -------------------------------------------------------------------------
# NEAR_NODE: слив кластера в центроид
# -------------------------------------------------------------------------

def merge_near_cluster(layer_geoms, positions, centroid, match_eps):
    """Сливает кластер почти совпавших узлов в его центроид.

    Копии узла во ВСЕХ фичах слоя двигаются вместе (replace_vertices)
    — общие границы не рвутся."""
    eps2 = match_eps * match_eps
    tgt = QgsPointXY(centroid[0], centroid[1])
    out = {}
    for fid in sorted(layer_geoms.keys()):
        polys, was_multi = _norm(layer_geoms[fid])
        cur = polys
        changed_any = False
        for pos in positions:
            new_polys, changed = replace_vertices(
                cur, QgsPointXY(pos[0], pos[1]), tgt, eps2)
            if changed:
                cur = new_polys
                changed_any = True
        if changed_any:
            out[fid] = polys_to_geom(cur, was_multi)
    if not out:
        raise FixRefused(u"узлы кластера не найдены (данные изменились)")
    return out


# -------------------------------------------------------------------------
# T_JUNCTION: вставить вершину в ребро (во все фичи с этим ребром)
# -------------------------------------------------------------------------

def insert_t_vertex(layer_geoms, pos, edges, match_eps):
    """Вставляет вершину pos на каждое ребро из edges = [(fid, p1, p2)].

    Рёбра одной фичи группируются: вставки применяются последовательно
    к одной и той же структуре (в payload одно ребро может встретиться
    у нескольких фич — общая граница)."""
    eps2 = match_eps * match_eps
    pt = QgsPointXY(pos[0], pos[1])
    by_fid = {}
    for fid, p1, p2 in edges:
        by_fid.setdefault(fid, []).append(
            (QgsPointXY(p1[0], p1[1]), QgsPointXY(p2[0], p2[1])))
    out = {}
    total = 0
    for fid in sorted(by_fid.keys()):
        g = layer_geoms.get(fid)
        if g is None:
            continue
        polys, was_multi = _norm(g)
        inserted = 0
        for a, b in by_fid[fid]:
            try:
                inserted += insert_vertex_topo(polys, a, b, pt, eps2)
            except Exception as exc:
                raise FixRefused(
                    u"фича #{}: сбой вставки вершины — {}".format(fid,
                                                                  exc))
        if inserted:
            total += inserted
            out[fid] = polys_to_geom(polys, was_multi)
    if not total:
        raise FixRefused(u"ребро под точкой не найдено "
                         u"(данные изменились)")
    return out


# -------------------------------------------------------------------------
# GAP: «приклеить» щель к соседу с длинной общей границей
# -------------------------------------------------------------------------

def glue_gap(layer_geoms, gap_geom, neighbor_fid, changed_fids=None):
    """Объединяет геометрию щели с соседом: площадь соседа вырастает
    ровно на площадь щели (контроль приёмки ТЗ).

    Если щель уже закрыта ПРЕДЫДУЩИМИ исправлениями пачки (например,
    слив NEAR_NODE свёл зазор к нулю) — отказ: клейка создала бы
    перехлёст с соседями, закрывшими щель."""
    g = layer_geoms.get(neighbor_fid)
    if g is None:
        raise FixRefused(u"сосед #{} не найден (данные изменились)"
                         .format(neighbor_fid))
    if gap_geom is None or gap_geom.isNull() or gap_geom.isEmpty():
        raise FixRefused(u"геометрия щели пуста (данные изменились)")
    if changed_fids:
        try:
            covered = [layer_geoms[f] for f in changed_fids
                       if f in layer_geoms]
            if covered:
                rest = gap_geom.difference(
                    QgsGeometry.unaryUnion(covered))
                if rest is None or rest.isNull() or rest.isEmpty() \
                        or rest.area() < 0.5 * gap_geom.area():
                    raise FixRefused(
                        u"щель уже закрыта исправлениями этой пачки "
                        u"(NEAR_NODE) — клейка не требуется")
        except FixRefused:
            raise
        except Exception:
            pass  # проверить не удалось — клейка идёт как есть
    try:
        merged = g.combine(gap_geom)  # PyQGIS 3: union = combine()
    except Exception as exc:
        raise FixRefused(u"сбой объединения со щелью: {}".format(exc))
    m = _polygon_only(merged)
    if m is None:
        raise FixRefused(u"объединение со щелью не дало полигона")
    try:
        area_before = float(g.area())
        area_after = float(m.area())
    except Exception:
        area_before = area_after = 0.0
    if area_after < area_before - 1e-12:
        raise FixRefused(u"площадь соседа уменьшилась при клейке щели")
    return {neighbor_fid: m}


# -------------------------------------------------------------------------
# OVERLAP: обрезать меньший / больший
# -------------------------------------------------------------------------

def trim_overlap(layer_geoms, payload, choice):
    """Обрезает один из пары по другому: choice "smaller"|"larger"."""
    smaller = payload.get("smaller")
    larger = payload.get("larger")
    if choice == "smaller":
        tgt_fid, other_fid = smaller, larger
    elif choice == "larger":
        tgt_fid, other_fid = larger, smaller
    else:
        raise FixRefused(u"выбор обрезаемого полигона не сделан")
    tg = layer_geoms.get(tgt_fid)
    og = layer_geoms.get(other_fid)
    if tg is None or og is None:
        raise FixRefused(u"полигоны перехлёста не найдены "
                         u"(данные изменились)")
    try:
        diff = tg.difference(og)
    except Exception as exc:
        raise FixRefused(u"сбой вычитания перехлёста: {}".format(exc))
    m = _polygon_only(diff)
    if m is None:
        raise FixRefused(u"после обрезки полигон исчез — исправьте "
                         u"вручную")
    return {tgt_fid: m}


# -------------------------------------------------------------------------
# INVALID: makeValid (явный выбор пользователя с превью)
# -------------------------------------------------------------------------

def make_valid_geom(g):
    """Результат makeValid (полигональная часть) или FixRefused."""
    if g is None:
        raise FixRefused(u"фича не найдена (данные изменились)")
    try:
        fixed = g.makeValid()
    except Exception as exc:
        raise FixRefused(u"сбой makeValid: {}".format(exc))
    m = _polygon_only(fixed)
    if m is None or m.isNull() or m.isEmpty():
        raise FixRefused(u"makeValid не дал полигона")
    return m
