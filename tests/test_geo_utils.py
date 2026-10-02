# -*- coding: utf-8 -*-
"""Офлайн-тесты геометрического ядра geo_utils.py (без запуска QGIS).

Подменяет модуль qgis.core минимальными стабами и проверяет:
  * нормализацию и сборку полигональной геометрии;
  * replace_vertices — топологическое перемещение общего узла;
  * find_vertex / find_segment / point_to_segment;
  * insert_vertex_topo — точное ребро, T-надмножество, T-подмножество,
    защита от дубликатов;
  * polys_bbox, contains_vertex.
"""
import math
import os
import sys
import types

# ---------------------------------------------------------------------------
# Стабы qgis.core
# ---------------------------------------------------------------------------
qgis_mod = types.ModuleType("qgis")
core_mod = types.ModuleType("qgis.core")


class QgsPointXY(object):
    def __init__(self, *args):
        if len(args) == 1:
            src = args[0]
            self._x = float(src.x())
            self._y = float(src.y())
        elif len(args) == 2:
            self._x = float(args[0])
            self._y = float(args[1])
        else:
            self._x = self._y = 0.0

    def x(self):
        return self._x

    def y(self):
        return self._y

    def sqrDist(self, o):
        # ВАЖНО: имена методов стáба должны ТОЧНО совпадать с реальным
        # PyQGIS API (QgsPointXY.sqrDist) — иначе стаб маскирует опечатки.
        dx = self._x - o.x()
        dy = self._y - o.y()
        return dx * dx + dy * dy

    def distance(self, o):
        return math.sqrt(self.sqrDist(o))

    def __repr__(self):
        return "QgsPointXY(%r, %r)" % (self._x, self._y)

    def __eq__(self, o):
        return isinstance(o, QgsPointXY) and self._x == o.x() and self._y == o.y()


class QgsRectangle(object):
    def __init__(self, *a):
        if len(a) == 4:
            self.xmin, self.ymin, self.xmax, self.ymax = float(a[0]), float(a[1]), float(a[2]), float(a[3])
        else:
            self.xmin = self.ymin = self.xmax = self.ymax = 0.0

    def grow(self, m):
        self.xmin -= m
        self.ymin -= m
        self.xmax += m
        self.ymax += m

    def combineExtentWith(self, r):
        self.xmin = min(self.xmin, r.xmin)
        self.ymin = min(self.ymin, r.ymin)
        self.xmax = max(self.xmax, r.xmax)
        self.ymax = max(self.ymax, r.ymax)

    def intersects(self, r):
        return not (r.xmin > self.xmax or r.xmax < self.xmin or
                    r.ymin > self.ymax or r.ymax < self.ymin)


class QgsWkbTypes(object):
    PolygonGeometry = 0
    LineGeometry = 1
    PointGeometry = 2
    NullGeometry = 3


class QgsFeatureRequest(object):
    def __init__(self, rect=None):
        self.rect = rect

    def setSubsetOfAttributes(self, names):
        pass


class QgsProject(object):
    @staticmethod
    def instance():
        return None


class QgsCoordinateTransform(object):
    pass


class QgsUnitTypes(object):
    pass


class QgsMapLayerType(object):
    pass


class QgsVectorLayer(object):
    pass


class Qgis(object):
    Info = 0
    Warning = 1
    Critical = 2
    Success = 3


class QgsGeometry(object):
    def __init__(self, src=None):
        self._data = None
        if isinstance(src, QgsGeometry) and src._data is not None:
            self._data = dict(src._data)

    @classmethod
    def fromPolygonXY(cls, rings):
        g = cls()
        g._data = {"type": QgsWkbTypes.PolygonGeometry, "multi": False,
                   "polys": [list(rings)]}
        return g

    @classmethod
    def fromMultiPolygonXY(cls, polys):
        g = cls()
        g._data = {"type": QgsWkbTypes.PolygonGeometry, "multi": True,
                   "polys": [list(p) for p in polys]}
        return g

    @classmethod
    def fromPolylineXY(cls, pts):
        g = cls()
        g._data = {"type": QgsWkbTypes.LineGeometry, "multi": False,
                   "lines": [list(pts)]}
        return g

    def isNull(self):
        return self._data is None

    def isEmpty(self):
        return self._data is None

    def type(self):
        return self._data["type"]

    def isMultipart(self):
        return self._data["multi"]

    def asPolygon(self):
        return self._data["polys"][0]

    def asMultiPolygon(self):
        return self._data["polys"]

    def asPolyline(self):
        return self._data["lines"][0]

    def asMultiPolyline(self):
        return self._data["lines"]


core_mod.QgsPointXY = QgsPointXY
core_mod.QgsRectangle = QgsRectangle
core_mod.QgsWkbTypes = QgsWkbTypes
core_mod.QgsGeometry = QgsGeometry
# имена, нужные для импорта topopolyedit.topo_editor (см. секцию 16)
core_mod.QgsFeatureRequest = QgsFeatureRequest
core_mod.QgsProject = QgsProject
core_mod.QgsCoordinateTransform = QgsCoordinateTransform
core_mod.QgsUnitTypes = QgsUnitTypes
core_mod.QgsMapLayerType = QgsMapLayerType
core_mod.QgsVectorLayer = QgsVectorLayer
core_mod.Qgis = Qgis

sys.modules["qgis"] = qgis_mod
sys.modules["qgis.core"] = core_mod

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "topopolyedit"))
import geo_utils as gu  # noqa: E402

FAILURES = []


def check(name, cond, extra=""):
    if cond:
        print("OK  ", name)
    else:
        print("FAIL", name, extra)
        FAILURES.append(name)


def pt(x, y):
    return QgsPointXY(x, y)


def ring(*coords):
    c = list(coords)
    c.append(c[0])  # замыкаем
    return [pt(x, y) for x, y in c]


def assert_pts_equal(actual, expected, name):
    ok = len(actual) == len(expected) and all(
        abs(a.x() - e[0]) < 1e-9 and abs(a.y() - e[1]) < 1e-9
        for a, e in zip(actual, expected))
    check(name, ok, "got: %r" % [(p.x(), p.y()) for p in actual])


# ---------------------------------------------------------------------------
# 1. Нормализация и сборка: одиночный полигон
# ---------------------------------------------------------------------------
rings_a = [ring((0, 0), (2, 0), (2, 4), (0, 4))]
g = QgsGeometry.fromPolygonXY(rings_a)
nr = gu.norm_polys(g)
check("norm_polys: одиночный полигон распознан", nr is not None)
polys, multi = nr
check("norm_polys: was_multi=False", multi is False)
g2 = gu.polys_to_geom(polys, multi)
nr2 = gu.norm_polys(g2)
check("round-trip одиночный: структура совпадает",
      nr2[1] is False and len(nr2[0]) == 1 and
      [(p.x(), p.y()) for p in nr2[0][0][0]] ==
      [(p.x(), p.y()) for p in rings_a[0]])

# ---------------------------------------------------------------------------
# 2. Нормализация: мультиполигон
# ---------------------------------------------------------------------------
gm = QgsGeometry.fromMultiPolygonXY([rings_a, [ring((5, 5), (6, 5), (6, 6), (5, 6))]])
nrm = gu.norm_polys(gm)
check("norm_polys: мультиполигон, was_multi=True", nrm[1] is True and len(nrm[0]) == 2)
gm2 = gu.polys_to_geom(nrm[0], nrm[1])
check("round-trip мультиполигон", gu.norm_polys(gm2)[1] is True)

# ---------------------------------------------------------------------------
# 3. point_to_segment: проекция и ограничение концами
# ---------------------------------------------------------------------------
proj, d2, t = gu.point_to_segment(pt(1, 1), pt(0, 0), pt(2, 0))
check("point_to_segment: внутренняя проекция",
      abs(proj.x() - 1.0) < 1e-9 and abs(proj.y() - 0.0) < 1e-9 and
      abs(d2 - 1.0) < 1e-9 and abs(t - 0.5) < 1e-9)
proj, d2, t = gu.point_to_segment(pt(-5, 3), pt(0, 0), pt(2, 0))
check("point_to_segment: кламп к началу", t == 0.0 and abs(d2 - 34.0) < 1e-9)
proj, d2, t = gu.point_to_segment(pt(9, 1), pt(0, 0), pt(2, 0))
check("point_to_segment: кламп к концу", t == 1.0 and abs(d2 - 50.0) < 1e-9)

# ---------------------------------------------------------------------------
# 4. replace_vertices: общий узел двух полигонов
# ---------------------------------------------------------------------------
polys_a, _ = gu.norm_polys(QgsGeometry.fromPolygonXY(rings_a))
rings_b = [ring((2, 0), (5, 0), (5, 4), (2, 4))]
polys_b, _ = gu.norm_polys(QgsGeometry.fromPolygonXY(rings_b))

new_a, ch_a = gu.replace_vertices(polys_a, pt(2, 4), pt(3, 5), 1.0)
new_b, ch_b = gu.replace_vertices(polys_b, pt(2, 4), pt(3, 5), 1.0)
check("replace_vertices: узел заменён в A (1 раз, кольцо осталось замкнутым)",
      ch_a == 1 and len(new_a[0][0]) == 5 and
      abs(new_a[0][0][0].x() - new_a[0][0][-1].x()) < 1e-12)
check("replace_vertices: узел заменён в B", ch_b == 1)
assert_pts_equal(new_a[0][0], [(0, 0), (2, 0), (3, 5), (0, 4), (0, 0)],
                 "replace_vertices: координаты A верны")
assert_pts_equal(new_b[0][0], [(2, 0), (5, 0), (5, 4), (3, 5), (2, 0)],
                 "replace_vertices: координаты B верны")
check("replace_vertices: исходная структура не изменена",
      abs(polys_a[0][0][2].x() - 2.0) < 1e-12)

# узел в вершине-замыкании кольца (0,0)
new_c, ch_c = gu.replace_vertices(polys_a, pt(0, 0), pt(1, 1), 0.5)
check("replace_vertices: замыкающий дубликат заменён согласованно (2 точки)",
      ch_c == 2 and abs(new_c[0][0][0].x() - new_c[0][0][-1].x()) < 1e-12 and
      abs(new_c[0][0][0].y() - 1.0) < 1e-9)

# ---------------------------------------------------------------------------
# 5. find_vertex: ближайший узел, пропуск замыкающего дубликата
# ---------------------------------------------------------------------------
r = gu.find_vertex(polys_a, pt(1.9, 3.9), 1.0)
check("find_vertex: найден ближайший (2,4)", r is not None and
      abs(r[0].x() - 2.0) < 1e-9 and abs(r[0].y() - 4.0) < 1e-9 and
      len(r[2]) == 1)
r = gu.find_vertex(polys_a, pt(0.05, 0.05), 1.0)
check("find_vertex: вершина (0,0) без учёта дубликата",
      r is not None and abs(r[0].x()) < 1e-9 and len(r[2]) == 1)
r = gu.find_vertex(polys_a, pt(100, 100), 1.0)
check("find_vertex: вне допуска -> None", r is None)

# ---------------------------------------------------------------------------
# 6. contains_vertex
# ---------------------------------------------------------------------------
check("contains_vertex: узел общий для A и B",
      gu.contains_vertex(polys_a, pt(2, 0), 0.5) and
      gu.contains_vertex(polys_b, pt(2, 0), 0.5))
check("contains_vertex: (3,4) не узел", not gu.contains_vertex(polys_a, pt(3, 4), 0.5))

# ---------------------------------------------------------------------------
# 7. insert_vertex_topo: точное ребро в двух полигонах
# ---------------------------------------------------------------------------
cnt_a = gu.insert_vertex_topo(polys_a, pt(2, 0), pt(2, 4), pt(2, 2), 0.5)
cnt_b = gu.insert_vertex_topo(polys_b, pt(2, 0), pt(2, 4), pt(2, 2), 0.5)
check("insert точное ребро: вставлено в A", cnt_a == 1)
check("insert точное ребро: вставлено в B", cnt_b == 1)
assert_pts_equal(polys_a[0][0], [(0, 0), (2, 0), (2, 2), (2, 4), (0, 4), (0, 0)],
                 "insert точное ребро: порядок точек A верный")
# у B совпал замыкающий сегмент (2,4)->(2,0): узел встаёт перед замыканием
actual_b = [(p.x(), p.y()) for p in polys_b[0][0]]
expected_b = [(2, 0), (5, 0), (5, 4), (2, 4), (2, 2), (2, 0)]
check("insert точное ребро: порядок точек B верный", actual_b == expected_b,
      "got %r" % actual_b)

# повторная вставка в ту же точку -> защита от дубликата
cnt_dup = gu.insert_vertex_topo(polys_a, pt(2, 0), pt(2, 4), pt(2, 2), 0.5)
check("insert: защита от дубликата узла", cnt_dup == 0)

# ---------------------------------------------------------------------------
# 8. insert_vertex_topo: T-надмножество (соседнее ребро длиннее кликнутого)
# ---------------------------------------------------------------------------
polys_c, _ = gu.norm_polys(QgsGeometry.fromPolygonXY(
    [ring((2, -1), (2, 5), (4, 5), (4, -1))]))  # ребро (2,-1)-(2,5) — надмножество
cnt_c = gu.insert_vertex_topo(polys_c, pt(2, 0), pt(2, 4), pt(2, 2), 0.5)
check("insert T-надмножество: узел вставлен в соседа", cnt_c == 1)
actual_c = [(p.x(), p.y()) for p in polys_c[0][0]]
check("insert T-надмножество: порядок сохранён",
      actual_c == [(2, -1), (2, 2), (2, 5), (4, 5), (4, -1), (2, -1)],
      "got %r" % actual_c)

# ---------------------------------------------------------------------------
# 9. insert_vertex_topo: T-подмножество (ребро соседа короче кликнутого)
# ---------------------------------------------------------------------------
polys_d, _ = gu.norm_polys(QgsGeometry.fromPolygonXY(
    [ring((2, 1), (2, 3), (4, 3), (4, 1))]))  # ребро (2,1)-(2,3) — подмножество
cnt_d_in = gu.insert_vertex_topo(polys_d, pt(2, 0), pt(2, 4), pt(2, 2), 0.5)
check("insert T-подмножество: точка внутри ребра соседа -> вставка",
      cnt_d_in == 1 and
      [(p.x(), p.y()) for p in polys_d[0][0]] ==
      [(2, 1), (2, 2), (2, 3), (4, 3), (4, 1), (2, 1)])

polys_e, _ = gu.norm_polys(QgsGeometry.fromPolygonXY(
    [ring((2, 1), (2, 3), (4, 3), (4, 1))]))
cnt_e_out = gu.insert_vertex_topo(polys_e, pt(2, 0), pt(2, 4), pt(2, 3.5), 0.5)
check("insert T-подмножество: точка ВНЕ ребра соседа -> без вставки",
      cnt_e_out == 0)

# ---------------------------------------------------------------------------
# 10. find_segment
# ---------------------------------------------------------------------------
polys_f, _ = gu.norm_polys(QgsGeometry.fromPolygonXY(rings_a))
r = gu.find_segment(polys_f, pt(2.2, 2.0), 1.0)
check("find_segment: ближайшее ребро x=2", r is not None and
      abs(r[3].x() - 2.0) < 1e-9 and abs(r[4].x() - 2.0) < 1e-9 and
      abs(r[6] - 0.04) < 1e-9)
r = gu.find_segment(polys_f, pt(100, 100), 1.0)
check("find_segment: вне допуска -> None", r is None)

# ---------------------------------------------------------------------------
# 11. polys_bbox
# ---------------------------------------------------------------------------
bb = gu.polys_bbox(polys_a)
check("polys_bbox: габариты A", abs(bb.xmin) < 1e-9 and abs(bb.ymin) < 1e-9 and
      abs(bb.xmax - 2.0) < 1e-9 and abs(bb.ymax - 4.0) < 1e-9)

# ---------------------------------------------------------------------------
# 12. Криволинейная геометрия: segmentize-фолбэк в norm_polys / iter_polyline_parts
# ---------------------------------------------------------------------------
real_poly = QgsGeometry.fromPolygonXY([ring((0, 0), (1, 0), (1, 1), (0, 1))])


class _CurveSeg(object):
    """Стаб QgsAbstractGeometry с сегментацией."""
    def __init__(self, flat):
        self._flat = flat

    def segmentize(self):
        return self._flat


class CurvePolygonStub(QgsGeometry):
    """Стаб CurvePolygon: asPolygon() пуст, сегментация даёт полигон."""
    def __init__(self, flat):
        super(CurvePolygonStub, self).__init__(flat)
        self._flat = flat

    def asPolygon(self):
        return []  # как у реального CurvePolygon

    def constGet(self):
        return _CurveSeg(self._flat)


curve = CurvePolygonStub(real_poly)
nrc = gu.norm_polys(curve)
check("norm_polys: CurvePolygon сегментируется",
      nrc is not None and nrc[1] is False and len(nrc[0]) == 1 and
      len(nrc[0][0][0]) == 5)
r = gu.find_vertex(nrc[0], pt(1, 1), 0.1)
check("find_vertex: узел найден в сегментированной кривой",
      r is not None and abs(r[0].x() - 1.0) < 1e-9)

real_line = QgsGeometry.fromPolylineXY([pt(0, 0), pt(1, 0), pt(1, 1)])


class CurveLineStub(QgsGeometry):
    """Стаб CurveLine: asPolyline() пуст, сегментация даёт линию."""
    def __init__(self, flat):
        super(CurveLineStub, self).__init__(flat)
        self._flat = flat

    def asPolyline(self):
        return []

    def constGet(self):
        return _CurveSeg(self._flat)


parts = gu.iter_polyline_parts(CurveLineStub(real_line))
check("iter_polyline_parts: CurveLine сегментируется",
      len(parts) == 1 and len(parts[0]) == 3)

# ---------------------------------------------------------------------------
# 13. Внешний контур: угол одиночного полигона (без соседей) находится
#     и перемещается теми же операциями, что и общий узел
# ---------------------------------------------------------------------------
polys_solo, _ = gu.norm_polys(QgsGeometry.fromPolygonXY(
    [ring((0, 0), (3, 0), (3, 2), (0, 2))]))
r = gu.find_vertex(polys_solo, pt(0.1, 1.9), 0.5)
check("внешний контур: угол (0,2) найден в одиночном полигоне",
      r is not None and abs(r[0].x()) < 1e-9 and abs(r[0].y() - 2.0) < 1e-9)
new_solo, ch_solo = gu.replace_vertices(polys_solo, pt(0, 2), pt(1, 3), 0.5)
check("внешний контур: угол одиночного полигона перемещается",
      ch_solo == 1 and
      [(p.x(), p.y()) for p in new_solo[0][0]] ==
      [(0, 0), (3, 0), (3, 2), (1, 3), (0, 0)])

# ---------------------------------------------------------------------------
# 14. collinear_run: прямолинейная цепочка вершин ребра
# ---------------------------------------------------------------------------
ring_run = ring((0, 0), (2, 0), (2, 2), (2, 4), (0, 4))
polys_run, _ = gu.norm_polys(QgsGeometry.fromPolygonXY([ring_run]))
rr = polys_run[0][0]
run1 = gu.collinear_run(rr, 1, 0.25)   # сегмент (2,0)-(2,2)
run2 = gu.collinear_run(rr, 2, 0.25)   # сегмент (2,2)-(2,4)
check("collinear_run: цепочка расширена вперёд",
      [(p.x(), p.y()) for p in run1] == [(2, 0), (2, 2), (2, 4)])
check("collinear_run: цепочка расширена назад",
      [(p.x(), p.y()) for p in run2] == [(2, 0), (2, 2), (2, 4)])
run3 = gu.collinear_run(rr, 4, 0.25)   # замыкающий сегмент (0,4)-(0,0)
check("collinear_run: замыкающий сегмент -> левое ребро",
      [(p.x(), p.y()) for p in run3] == [(0, 4), (0, 0)])

polys_rect, _ = gu.norm_polys(QgsGeometry.fromPolygonXY(
    [ring((0, 0), (2, 0), (2, 4), (0, 4))]))
run4 = gu.collinear_run(polys_rect[0][0], 1, 0.25)
check("collinear_run: без промежуточных вершин цепочка = сегмент",
      [(p.x(), p.y()) for p in run4] == [(2, 0), (2, 4)])

# ---------------------------------------------------------------------------
# 15. Схема перемещения ребра (move_edge): однопроходный сдвиг всех
#     якорей прямолинейной цепочки во всех затронутых полигонах.
#     Полигон с общим ребром обновляется целиком (граница остаётся
#     прямой), полигон, примыкающий только концом, следует за ним.
# ---------------------------------------------------------------------------
polys_ea, _ = gu.norm_polys(QgsGeometry.fromPolygonXY(
    [ring((0, 0), (2, 0), (2, 2), (2, 4), (0, 4))]))  # A: ребро x=2 с узлом (2,2)
polys_eb, _ = gu.norm_polys(QgsGeometry.fromPolygonXY(
    [ring((2, 0), (5, 0), (5, 4), (2, 4), (2, 2))]))  # B: то же ребро
polys_ec, _ = gu.norm_polys(QgsGeometry.fromPolygonXY(
    [ring((2, 4), (4, 4), (4, 6), (2, 6))]))          # C: примыкает только (2,4)

anchors = [pt(2, 0), pt(2, 2), pt(2, 4)]
na, cha = gu.translate_vertices(polys_ea, anchors, (1, 0), 1.0)
check("move ребра: A обновлена целиком, граница осталась прямой",
      cha == 3 and
      [(p.x(), p.y()) for p in na[0][0]] ==
      [(0, 0), (3, 0), (3, 2), (3, 4), (0, 4), (0, 0)])

nb, chb = gu.translate_vertices(polys_eb, anchors, (1, 0), 1.0)
check("move ребра: B обновлена целиком (другой порядок вершин)",
      chb == 4 and  # (2,0) — и начало, и замыкание кольца: 4 вхождения
      [(p.x(), p.y()) for p in nb[0][0]] ==
      [(3, 0), (5, 0), (5, 4), (3, 4), (3, 2), (3, 0)])

nc, chc = gu.translate_vertices(polys_ec, anchors, (1, 0), 1.0)
check("move ребра: C (примыкание только концом) следует за вершиной",
      chc == 2 and  # (2,4) встречается дважды: начало + замыкание кольца
      [(p.x(), p.y()) for p in nc[0][0]] ==
      [(3, 4), (4, 4), (4, 6), (2, 6), (3, 4)])

nx, chx = gu.translate_vertices(polys_ec, [pt(10, 10)], (1, 0), 1.0)
check("move ребра: чужие вершины не сдвигаются",
      chx == 0 and
      [(p.x(), p.y()) for p in nx[0][0]] ==
      [(2, 4), (4, 4), (4, 6), (2, 6), (2, 4)])

check("move ребра: исходные структуры A/B/C не изменены",
      abs(polys_ea[0][0][2].x() - 2.0) < 1e-12 and
      abs(polys_eb[0][0][0].x() - 2.0) < 1e-12 and
      abs(polys_ec[0][0][0].x() - 2.0) < 1e-12)

# ---------------------------------------------------------------------------
# 16. points_bbox + регрессия «'NoneType' object has no attribute 'grow'»
#     В v1.1.0 было: rect = rect.combineExtentWith(r) — но combineExtentWith
#     в PyQGIS меняет прямоугольник на месте и ВОЗВРАЩАЕТ None (void),
#     поэтому при 2+ якорях rect обнулялся и rect.grow(...) падал.
#     Регрессия ловится на TopoEditor._collect_edge_moved с 2+ якорями.
# ---------------------------------------------------------------------------
rb0 = gu.points_bbox([])
check("points_bbox: пустой список -> None", rb0 is None)

rb1 = gu.points_bbox([pt(3, 7)])
check("points_bbox: одна точка",
      rb1 is not None and abs(rb1.xmin - 3.0) < 1e-9 and
      abs(rb1.ymin - 7.0) < 1e-9 and abs(rb1.xmax - 3.0) < 1e-9 and
      abs(rb1.ymax - 7.0) < 1e-9)

rb3 = gu.points_bbox([pt(2, 0), pt(5, 4), pt(1, 6)])
check("points_bbox: границы трёх точек",
      abs(rb3.xmin - 1.0) < 1e-9 and abs(rb3.ymin - 0.0) < 1e-9 and
      abs(rb3.xmax - 5.0) < 1e-9 and abs(rb3.ymax - 6.0) < 1e-9)
rb3.grow(1.0)
check("points_bbox: grow после накопления работает",
      abs(rb3.xmin - 0.0) < 1e-9 and abs(rb3.ymax - 7.0) < 1e-9)

# стаб обязан повторять семантику реального PyQGIS:
# изменение на месте + возврат None (иначе стаб снова замаскирует баг)
r_a = QgsRectangle(0, 0, 1, 1)
_ret = r_a.combineExtentWith(QgsRectangle(2, 2, 3, 3))
check("стаб QgsRectangle: combineExtentWith меняет на месте и возвращает None",
      _ret is None and abs(r_a.xmax - 3.0) < 1e-9 and abs(r_a.ymax - 3.0) < 1e-9)

# --- регрессия: _collect_edge_moved с 2+ якорями (падало в v1.1.0) ---
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import topopolyedit.topo_editor as te  # noqa: E402


class _Feat(object):
    def __init__(self, fid, geom):
        self._fid = fid
        self._g = geom

    def id(self):
        return self._fid

    def geometry(self):
        return self._g


class _Layer(object):
    def __init__(self, feats):
        self._feats = feats

    def getFeatures(self, req):
        return list(self._feats)


gA = QgsGeometry.fromPolygonXY([ring((0, 0), (2, 0), (2, 2), (2, 4), (0, 4))])
gB = QgsGeometry.fromPolygonXY([ring((2, 0), (5, 0), (5, 4), (2, 4), (2, 2))])
gC = QgsGeometry.fromPolygonXY([ring((2, 4), (4, 4), (4, 6), (2, 6))])
lay = _Layer([_Feat(1, gA), _Feat(2, gB), _Feat(3, gC)])
ed = te.TopoEditor(None, lay)

entries = ed._collect_edge_moved([pt(2, 0), pt(2, 2), pt(2, 4)], (1, 0), 1.0)
check("collect_edge_moved: 3 якоря — нет AttributeError, найдены все фичи",
      len(entries) == 3 and sorted(e["fid"] for e in entries) == [1, 2, 3])

ent_a = [e for e in entries if e["fid"] == 1][0]
check("collect_edge_moved: A сместилась целиком, граница прямая",
      ent_a["multi"] is False and
      [(p.x(), p.y()) for p in ent_a["polys"][0][0]] ==
      [(0, 0), (3, 0), (3, 2), (3, 4), (0, 4), (0, 0)])

ent_c = [e for e in entries if e["fid"] == 3][0]
check("collect_edge_moved: C следует за концом (2,4)",
      [(p.x(), p.y()) for p in ent_c["polys"][0][0]] ==
      [(3, 4), (4, 4), (4, 6), (2, 6), (3, 4)])

check("collect_edge_moved: пустые якоря -> [] без падения",
      ed._collect_edge_moved([], (1, 0), 1.0) == [])

# ---------------------------------------------------------------------------
print("")
if FAILURES:
    print("ИТОГ: ПРОВАЛЕНО тестов: %d -> %s" % (len(FAILURES), FAILURES))
    sys.exit(1)
print("ИТОГ: все тесты пройдены")
