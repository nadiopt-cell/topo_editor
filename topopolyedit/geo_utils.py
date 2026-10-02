# -*- coding: utf-8 -*-
"""Геометрические утилиты для топологического редактирования полигонов.

Все функции работают с "нормализованным" представлением полигональной
геометрии::

    polys = [часть][кольцо][точка QgsPointXY]

где первое кольцо части — внешнее, кольца замкнуты (первая точка равна
последней). Поддерживаются только 2D-геометрии (Z/M отбрасываются),
кривые сегменты сегментируются средствами QGIS.
"""

from qgis.core import QgsPointXY, QgsGeometry, QgsRectangle, QgsWkbTypes

# Порог совпадения точек при дедупликации (в квадрате единиц слоя).
# 1e-12 ~ (1e-6 м)^2 — на порядок ниже любого реального допуска прилипания,
# но выше шума вычислений с плавающей запятой.
DUP_EPS2 = 1e-12


# ---------------------------------------------------------------------------
# Нормализация / сборка геометрии
# ---------------------------------------------------------------------------

def _poly_parts(geom):
    """QgsGeometry -> (polys, was_multi) без обработки кривых."""
    if geom.isMultipart():
        return geom.asMultiPolygon(), True
    return [geom.asPolygon()], False


def _polys_have_rings(polys):
    """True, если в структуре есть хотя бы одно непустое кольцо."""
    return any(len(ring) > 0 for part in polys for ring in part)


def _segmentized(geom):
    """Сегментация криволинейной геометрии (CurvePolygon/CompoundCurve).

    :return: QgsGeometry с прямолинейными сегментами или None при ошибке.
    """
    try:
        flat = QgsGeometry(geom.constGet().segmentize())
        if flat.isNull() or flat.isEmpty():
            return None
        return flat
    except Exception:
        return None


def norm_polys(geom):
    """QgsGeometry -> (polys, was_multi) или None, если геометрия пустая
    или не является полигоном/мультиполигоном.

    Криволинейные полигоны (CurvePolygon) предварительно сегментируются:
    asPolygon() для них возвращает пустой список, что ранее приводило к
    тихому пропуску геометрии при поиске узлов и рёбер."""
    if geom is None or geom.isNull() or geom.isEmpty():
        return None
    if geom.type() != QgsWkbTypes.PolygonGeometry:
        return None
    polys, was_multi = _poly_parts(geom)
    if not _polys_have_rings(polys):
        flat = _segmentized(geom)
        if flat is not None:
            polys2, was_multi2 = _poly_parts(flat)
            if _polys_have_rings(polys2):
                polys, was_multi = polys2, was_multi2
    if not _polys_have_rings(polys):
        return None
    return polys, was_multi


def polys_to_geom(polys, was_multi):
    """Обратное преобразование norm_polys -> QgsGeometry (в том же CRS)."""
    if was_multi:
        return QgsGeometry.fromMultiPolygonXY(polys)
    return QgsGeometry.fromPolygonXY(polys[0])


def iter_rings(polys):
    """Итератор по всем кольцам: yield (part_idx, ring_idx, ring)."""
    for pi, part in enumerate(polys):
        for ri, ring in enumerate(part):
            yield pi, ri, ring


def iter_ring_segments(ring):
    """Сегменты замкнутого кольца: yield (index_a, a, b)."""
    n = len(ring)
    for i in range(max(0, n - 1)):
        yield i, ring[i], ring[i + 1]


def iter_polyline_parts(geom):
    """Список линий (каждая — список QgsPointXY) для линейной геометрии.
    Криволинейные линии предварительно сегментируются; если разобрать
    геометрию не удалось, возвращает пустой список."""
    if geom is None or geom.isNull() or geom.isEmpty():
        return []
    if geom.type() != QgsWkbTypes.LineGeometry:
        return []

    def _extract(g):
        if g.isMultipart():
            lines = g.asMultiPolyline()
        else:
            lines = [g.asPolyline()]
        return [l for l in lines if l is not None and len(l) >= 2]

    lines = _extract(geom)
    if not lines:
        flat = _segmentized(geom)
        if flat is not None:
            lines = _extract(flat)
    return lines


# ---------------------------------------------------------------------------
# Базовая планиметрия
# ---------------------------------------------------------------------------

def point_to_segment(p, a, b):
    """Проекция точки p на отрезок ab.

    :return: (proj QgsPointXY, dist2, t), t in [0, 1]
    """
    ax, ay = a.x(), a.y()
    bx, by = b.x(), b.y()
    px, py = p.x(), p.y()
    dx, dy = bx - ax, by - ay
    len2 = dx * dx + dy * dy
    if len2 <= 0.0:
        proj = QgsPointXY(ax, ay)
        return proj, p.sqrDist(proj), 0.0
    t = ((px - ax) * dx + (py - ay) * dy) / len2
    if t < 0.0:
        t = 0.0
    elif t > 1.0:
        t = 1.0
    proj = QgsPointXY(ax + t * dx, ay + t * dy)
    return proj, p.sqrDist(proj), t


def _on_seg(p, a, b, eps2):
    """True, если точка p лежит на отрезке ab в пределах eps."""
    _proj, d2, _t = point_to_segment(p, a, b)
    return d2 <= eps2


def _t_unclamped(p, a, b):
    """Неклампированный параметр проекции точки p на прямую ab (0..1 на отрезке)."""
    ax, ay = a.x(), a.y()
    dx, dy = b.x() - ax, b.y() - ay
    len2 = dx * dx + dy * dy
    if len2 <= 0.0:
        return 0.0
    return ((p.x() - ax) * dx + (p.y() - ay) * dy) / len2


# ---------------------------------------------------------------------------
# Поиск узлов и рёбер
# ---------------------------------------------------------------------------

def find_vertex(polys, pt, eps2):
    """Ближайшая вершина к точке pt в пределах eps2.

    :return: (point QgsPointXY, dist2, [(pi, ri, vi), ...]) или None.
    Замыкающая дублирующая точка кольца в поиск не включается.
    """
    best_d2 = None
    best_pt = None
    locs = []
    for _pi, _ri, ring in iter_rings(polys):
        n = len(ring)
        for vi in range(n):
            if vi == n - 1 and n > 1 and ring[0].sqrDist(ring[-1]) <= DUP_EPS2:
                continue  # пропускаем замыкающий дубликат
            d2 = pt.sqrDist(ring[vi])
            if best_d2 is None or d2 < best_d2:
                best_d2 = d2
                best_pt = ring[vi]
                locs = [(_pi, _ri, vi)]
            elif d2 == best_d2:
                locs.append((_pi, _ri, vi))
    if best_pt is None or best_d2 > eps2:
        return None
    return best_pt, best_d2, locs


def find_segment(polys, pt, eps2):
    """Ближайший к точке сегмент в пределах eps2.

    :return: (pi, ri, si, a, b, proj, dist2) или None.
    """
    best = None
    for pi, ri, ring in iter_rings(polys):
        for si, a, b in iter_ring_segments(ring):
            proj, d2, _t = point_to_segment(pt, a, b)
            if best is None or d2 < best[6]:
                best = (pi, ri, si, a, b, proj, d2)
    if best is None or best[6] > eps2:
        return None
    return best


def contains_vertex(polys, pt, eps2):
    """True, если хотя бы одна вершина структур совпадает с pt в пределах eps."""
    for _pi, _ri, ring in iter_rings(polys):
        for v in ring:
            if v.sqrDist(pt) <= eps2:
                return True
    return False


# ---------------------------------------------------------------------------
# Топологические операции
# ---------------------------------------------------------------------------

def replace_vertices(polys, old_pt, new_pt, eps2):
    """Заменяет ВСЕ вершины в радиусе eps от old_pt на new_pt.

    Возвращает (new_polys, changed_count). Исходная структура не изменяется.
    Замыкающие дубликаты заменяются согласованно, кольца остаются замкнутыми.
    """
    new_polys = []
    changed = 0
    for part in polys:
        new_part = []
        for ring in part:
            new_ring = []
            for v in ring:
                if v.sqrDist(old_pt) <= eps2:
                    new_ring.append(QgsPointXY(new_pt))
                    changed += 1
                else:
                    new_ring.append(QgsPointXY(v))
            new_part.append(new_ring)
        new_polys.append(new_part)
    return new_polys, changed


def _ring_has_point(ring, pt):
    """True, если в кольце уже есть вершина, совпадающая с pt (дубликат)."""
    for v in ring:
        if v.sqrDist(pt) <= DUP_EPS2:
            return True
    return False


def insert_vertex_topo(polys, p1, p2, new_pt, eps2):
    """Вставляет new_pt на ребро (p1, p2) во ВСЕ кольца, где это ребро есть.

    Распознаются три случая:
      1) точное совпадение ребра кольца с (p1, p2) — в любом направлении;
      2) ребро кольца — НАДмножество (обе точки p1 и p2 лежат на ребре
         кольца) — случай T-образного примыкания без узла в точке вставки;
      3) ребро кольца — ПОДмножество (обе точки ребра кольца лежат на
         (p1, p2)) и точка new_pt попадает внутрь этого ребра.

    Если в кольце уже есть вершина в точке new_pt — вставка не выполняется
    (защита от дубликатов при T-примыканиях).

    :param polys: нормализованная структура (модифицируется на месте)
    :return: число выполненных вставок
    """
    inserted = 0
    for _pi, _ri, ring in iter_rings(polys):
        n = len(ring)
        if n < 4:
            continue  # минимальное замкнутое кольцо — треугольник
        if _ring_has_point(ring, new_pt):
            continue
        done = False
        for si in range(n - 1):
            a, b = ring[si], ring[si + 1]
            exact = ((a.sqrDist(p1) <= eps2 and b.sqrDist(p2) <= eps2) or
                     (a.sqrDist(p2) <= eps2 and b.sqrDist(p1) <= eps2))
            if exact:
                ring.insert(si + 1, QgsPointXY(new_pt))
                inserted += 1
                done = True
                break
            # надмножество: p1 и p2 лежат на ребре кольца (T-примыкание)
            if _on_seg(p1, a, b, eps2) and _on_seg(p2, a, b, eps2):
                ring.insert(si + 1, QgsPointXY(new_pt))
                inserted += 1
                done = True
                break
            # подмножество: ребро кольца целиком лежит на (p1, p2),
            # а точка вставки попадает внутрь ребра кольца (строгая
            # проверка по параметру t — иначе получился бы "шип")
            if _on_seg(a, p1, p2, eps2) and _on_seg(b, p1, p2, eps2):
                t_new = _t_unclamped(new_pt, a, b)
                if -1e-12 <= t_new <= 1.0 + 1e-12:
                    ring.insert(si + 1, QgsPointXY(new_pt))
                    inserted += 1
                    done = True
                    break
        # одно кольцо — не более одной вставки на данное ребро
    return inserted


# ---------------------------------------------------------------------------
# Габариты
# ---------------------------------------------------------------------------

def polys_bbox(polys):
    """Габаритный прямоугольник нормализованной структуры (QgsRectangle)."""
    minx = miny = maxx = maxy = None
    for _pi, _ri, ring in iter_rings(polys):
        for v in ring:
            x, y = v.x(), v.y()
            if minx is None or x < minx:
                minx = x
            if maxx is None or x > maxx:
                maxx = x
            if miny is None or y < miny:
                miny = y
            if maxy is None or y > maxy:
                maxy = y
    if minx is None:
        return QgsRectangle()
    return QgsRectangle(minx, miny, maxx, maxy)
