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
import time
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
    # _inst подменяется тестами (секция 17) на фейковый проект с деревом слоёв
    _inst = None

    @staticmethod
    def instance():
        return QgsProject._inst


class QgsLayerTreeNode(object):
    NodeLayer = 0
    NodeGroup = 1


class QgsCoordinateTransform(object):
    pass


class QgsUnitTypes(object):
    RenderPixels = 0  # как в реальном API — нужен settings.tolerance_map_units


class QgsMapLayerType(object):
    VectorLayer = 0
    RasterLayer = 1
    PluginLayer = 2


class QgsVectorLayer(object):
    pass


class Qgis(object):
    Info = 0
    Warning = 1
    Critical = 2
    Success = 3


class QgsSettings(object):
    """Стаб QgsSettings: хранилище в словаре, семантика как у реального.

    Реальный QgsSettings.value(key, default, type=...) при отсутствии
    ключа возвращает default, при невозможности конвертации — тоже
    default. Стаб обязан повторять ЭТУ семантику.
    """

    _store = {}  # общий на тест; очищается в начале секции 18

    def __init__(self):
        pass

    def value(self, key, default=None, type=None):
        if key not in QgsSettings._store:
            return default
        raw = QgsSettings._store[key]
        try:
            if type is float:
                return float(raw)
            if type is str:
                return str(raw)
            if type is int:
                return int(raw)
            if type is bool:
                return str(raw).lower() in ("true", "1", "yes")
        except Exception:
            return default
        return raw

    def setValue(self, key, value):
        QgsSettings._store[key] = value

    def remove(self, key):
        QgsSettings._store.pop(key, None)


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
core_mod.QgsLayerTreeNode = QgsLayerTreeNode
core_mod.QgsSettings = QgsSettings


# --- QgsMessageLog: журнал, записи собираются для проверок (секция 20) ---
class QgsMessageLog(object):
    MESSAGES = []  # (message, tag, level)

    @staticmethod
    def logMessage(msg, tag="", level=None):
        QgsMessageLog.MESSAGES.append((str(msg), tag, level))


core_mod.QgsMessageLog = QgsMessageLog


# --- стабы qgis.PyQt / qgis.gui (utils, инструменты, BaseTopoTool) ---
qtcore_mod = types.ModuleType("qgis.PyQt.QtCore")


class Qt(object):
    class CursorShape(object):
        CrossCursor = 0

    class Key(object):
        Key_Escape = 0x01000001

    class MouseButton(object):
        LeftButton = 1

    class CheckState(object):
        Unchecked = 0
        Checked = 2

    class ItemDataRole(object):
        UserRole = 0x0100

    class ItemFlag(object):
        ItemIsEnabled = 32
        ItemIsUserCheckable = 16


qtcore_mod.Qt = Qt

qtgui_mod = types.ModuleType("qgis.PyQt.QtGui")


class QColor(object):
    def __init__(self, *a):
        pass


qtgui_mod.QColor = QColor

qtwidgets_mod = types.ModuleType("qgis.PyQt.QtWidgets")


class QMessageBox(object):
    Yes = 1
    No = 0

    @staticmethod
    def question(*a, **kw):
        return QMessageBox.No


qtwidgets_mod.QMessageBox = QMessageBox

pyqt_mod = types.ModuleType("qgis.PyQt")
pyqt_mod.__path__ = []


class QgsMapTool(object):
    def __init__(self, canvas):
        self._canvas = canvas

    def canvas(self):
        return self._canvas

    def setCursor(self, c):
        pass

    def activate(self):
        pass

    def deactivate(self):
        pass

    def keyPressEvent(self, e):
        pass


class QgsVertexMarker(object):
    ICON_BOX = 0
    ICON_CIRCLE = 1

    def __init__(self, canvas):
        self._center = None
        self._visible = False

    def setIconType(self, t):
        pass

    def setColor(self, c):
        pass

    def setPenWidth(self, w):
        pass

    def setCenter(self, pt):
        self._center = pt

    def hide(self):
        self._visible = False

    def show(self):
        self._visible = True


class QgsRubberBand(object):
    def __init__(self, canvas, gtype=None):
        self.geom = None

    def setStrokeColor(self, c):
        pass

    def setFillColor(self, c):
        pass

    def setWidth(self, w):
        pass

    def reset(self, gtype):
        self.geom = None

    def setToGeometry(self, g, layer):
        self.geom = g


gui_mod = types.ModuleType("qgis.gui")
gui_mod.QgsMapTool = QgsMapTool
gui_mod.QgsVertexMarker = QgsVertexMarker
gui_mod.QgsRubberBand = QgsRubberBand

sys.modules["qgis"] = qgis_mod
sys.modules["qgis.core"] = core_mod
sys.modules["qgis.PyQt"] = pyqt_mod
sys.modules["qgis.PyQt.QtCore"] = qtcore_mod
sys.modules["qgis.PyQt.QtGui"] = qtgui_mod
sys.modules["qgis.PyQt.QtWidgets"] = qtwidgets_mod
sys.modules["qgis.gui"] = gui_mod

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
# 17. Прилипание (snapping_engine): строгий снэп к узлам эталонных слоёв
#     и прилипание к ЛИНЕЙНЫМ слоям. Список слоёв — ручной обход дерева
#     панели слоёв (v1.1.x зависел только от checkedLayers() — эталонные
#     слои могли тихо выпадать из прилипания).
# ---------------------------------------------------------------------------
import topopolyedit.snapping_engine as se  # noqa: E402


class _Crs(object):
    def __init__(self, authid):
        self._a = authid

    def authid(self):
        return self._a


class _SnapLyr(object):
    def __init__(self, lid, gtype, feats):
        self._lid = lid
        self._gt = gtype
        self._feats = feats

    def id(self):
        return self._lid

    def isValid(self):
        return True

    def type(self):
        return QgsMapLayerType.VectorLayer

    def geometryType(self):
        return self._gt

    def crs(self):
        return _Crs("EPSG:3857")

    def getFeatures(self, req):
        return list(self._feats)


class _TreeNode(object):
    def __init__(self, layer=None, children=None, checked=True):
        self._layer = layer
        self._children = children if children is not None else []
        self._checked = checked

    def children(self):
        return self._children

    def itemVisibilityChecked(self):
        return self._checked

    def nodeType(self):
        return (QgsLayerTreeNode.NodeLayer if self._layer is not None
                else QgsLayerTreeNode.NodeGroup)

    def layer(self):
        return self._layer


class _TreeRoot(_TreeNode):
    def checkedLayers(self):
        out = []

        def walk(n):
            for c in n.children():
                if not c.itemVisibilityChecked():
                    continue
                if c._layer is not None:
                    out.append(c._layer)
                else:
                    walk(c)

        walk(self)
        return out


class _FakeProject(object):
    def __init__(self, root):
        self._root = root

    def layerTreeRoot(self):
        return self._root


# --- 17a: get_snap_layers ---
poly_l = _SnapLyr("L-poly", QgsWkbTypes.PolygonGeometry, [])
line_l = _SnapLyr("L-line", QgsWkbTypes.LineGeometry, [])
null_l = _SnapLyr("L-null", QgsWkbTypes.NullGeometry, [])
off_l = _SnapLyr("L-off", QgsWkbTypes.PolygonGeometry, [])
root = _TreeRoot(children=[
    _TreeNode(layer=poly_l),
    _TreeNode(layer=null_l),
    _TreeNode(layer=off_l, checked=False),
    _TreeNode(children=[_TreeNode(layer=line_l)]),  # вложенная группа
])
QgsProject._inst = _FakeProject(root)

got = se.get_snap_layers(None)
check("get_snap_layers: включённые полигон и ЛИНИЯ в списке, выключенный и безгеометричный пропущены",
      [l.id() for l in got] == ["L-poly", "L-line"])

edit_l = _SnapLyr("L-edit", QgsWkbTypes.PolygonGeometry, [])
got2 = se.get_snap_layers(edit_l)
check("get_snap_layers: редактируемый слой добавлен первым",
      [l.id() for l in got2] == ["L-edit", "L-poly", "L-line"])

# --- 17b: _snap_layer по полигону-эталону ---
eng = se.SnappingEngine(None)
TOL2 = 4.0     # рёбра: радиус 2 ед.
TOLV2 = 100.0  # узлы: радиус 10 ед. (строгий снэп)
SAME = _Crs("EPSG:3857")

gpoly = QgsGeometry.fromPolygonXY(
    [ring((10, 10), (20, 10), (20, 20), (10, 20))])
lyr_p = _SnapLyr("P", QgsWkbTypes.PolygonGeometry, [_Feat(7, gpoly)])

v, e = eng._snap_layer(lyr_p, pt(19, 19), None, SAME, None, TOL2, TOLV2)
check("snap полигон: узел (20,20) найден у эталонного слоя",
      v is not None and abs(v[1].x() - 20.0) < 1e-9 and
      abs(v[1].y() - 20.0) < 1e-9)
check("snap полигон: ребро тоже найдено", e is not None)

v, e = eng._snap_layer(lyr_p, pt(25, 20), None, SAME, None, TOL2, TOLV2)
check("snap полигон: СТРОГИЙ узел берётся в увеличенном радиусе (5 ед > 2 ед)",
      v is not None and abs(v[1].x() - 20.0) < 1e-9)
check("snap полигон: ребро вне малого допуска -> None", e is None)

v, e = eng._snap_layer(lyr_p, pt(19, 19), None, SAME, {("P", 7)}, TOL2, TOLV2)
check("snap полигон: исключённая фича игнорируется",
      v is None and e is None)

# --- 17c: приоритет узел > ребро ---
r = se.SnappingEngine._pick((9.0, pt(1, 1), "L", 1),
                            (1.0, pt(2, 2), "L", 2))
check("приоритет: узел побеждает ребро, даже если ребро ближе",
      r is not None and r.snap_type == "vertex" and
      abs(r.point.x() - 1.0) < 1e-9)
r = se.SnappingEngine._pick(None, (1.0, pt(2, 2), "L", 2, (pt(0, 0), pt(4, 4))))
check("приоритет: без узла берётся ребро",
      r is not None and r.snap_type == "edge" and
      abs(r.point.x() - 2.0) < 1e-9)
r = se.SnappingEngine._pick(None, None)
check("приоритет: без кандидатов -> None", r is None)

# --- 17d: прилипание к ЛИНЕЙНОМУ эталонному слою ---
gline = QgsGeometry.fromPolylineXY([pt(0, 0), pt(10, 0), pt(10, 10)])
lyr_l = _SnapLyr("L", QgsWkbTypes.LineGeometry, [_Feat(3, gline)])

v, e = eng._snap_layer(lyr_l, pt(10, 4), None, SAME, None, TOL2, TOLV2)
check("snap линия: узел вершины (10,0) найден",
      v is not None and abs(v[1].x() - 10.0) < 1e-9 and
      abs(v[1].y() - 0.0) < 1e-9)
check("snap линия: прилипание к отрезку линии",
      e is not None and abs(e[1].x() - 10.0) < 1e-9 and
      abs(e[1].y() - 4.0) < 1e-9 and
      abs(e[4][0].x() - 10.0) < 1e-9 and abs(e[4][0].y() - 0.0) < 1e-9 and
      abs(e[4][1].y() - 10.0) < 1e-9)

v, e = eng._snap_layer(lyr_l, pt(5, 1), None, SAME, None, TOL2, TOLV2)
check("snap линия: ребро найдено с малого расстояния, узлы далеко",
      e is not None and abs(e[1].y() - 0.0) < 1e-9)

# мультилиния: вершины обеих частей
gml = QgsGeometry()
gml._data = {"type": QgsWkbTypes.LineGeometry, "multi": True,
             "lines": [[pt(0, 0), pt(5, 0)], [pt(50, 50), pt(60, 50)]]}
lyr_m = _SnapLyr("M", QgsWkbTypes.LineGeometry, [_Feat(4, gml)])
v, e = eng._snap_layer(lyr_m, pt(55, 50), None, SAME, None, TOL2, TOLV2)
check("snap мультилиния: вершина второй части найдена",
      v is not None and abs(v[1].x() - 50.0) < 1e-9 and
      abs(v[1].y() - 50.0) < 1e-9)

# ---------------------------------------------------------------------------
# 18. Настройки прилипания (settings.py): чёрный список слоёв + допуски.
#     QgsSettings стаб словарный; семантика value(key, default, type)
#     повторяет реальную (нет ключа / не конвертируется -> default).
# ---------------------------------------------------------------------------
import topopolyedit.settings as st  # noqa: E402

QgsSettings._store.clear()

# --- 18a: значения по умолчанию ---
check("18a default: tolerance_values = (10.0, 15.0, px)",
      st.tolerance_values() == (10.0, 15.0, st.UNITS_PX))
check("18a default: чёрный список пуст", st.disabled_layer_ids() == set())
check("18a default: константы движка совпадают с дефолтами настроек",
      se.TOLERANCE_PX == 10.0 and se.VERTEX_TOLERANCE_PX == 15.0)

# --- 18b: parse_layer_ids ---
check("18b parse: пробелы и пустые части отброшены",
      st.parse_layer_ids(u" a , b ,, c ") == ["a", "b", "c"])
check("18b parse: пустая строка -> []", st.parse_layer_ids(u"") == [])
check("18b parse: None -> []", st.parse_layer_ids(None) == [])

# --- 18c: сохранение и чтение (round-trip) ---
check("18c save: True при сохранении",
      st.save_settings({"edge_tol": 4.5, "vertex_tol": 6.5,
                        "units": st.UNITS_MAP,
                        "disabled": ["a", "b"]}) is True)
check("18c load: значения совпадают",
      st.load_settings() == {"edge_tol": 4.5, "vertex_tol": 6.5,
                             "units": st.UNITS_MAP, "disabled": ["a", "b"]})
check("18c disabled_layer_ids: множество {'a','b'}",
      st.disabled_layer_ids() == {"a", "b"})

# --- 18d: защита от мусора и границ ---
QgsSettings._store.clear()
st.save_settings({"edge_tol": 0.0, "vertex_tol": 1e9,
                  "units": "bogus", "disabled": ["  ", ""]})
cfg = st.load_settings()
check("18d clamp: 0 -> MIN_TOL", cfg["edge_tol"] == st.MIN_TOL)
check("18d clamp: 1e9 -> MAX_TOL", cfg["vertex_tol"] == st.MAX_TOL)
check("18d units: мусор -> px", cfg["units"] == st.UNITS_PX)
check("18d disabled: пустые части отброшены", cfg["disabled"] == [])

# --- 18e: get_snap_layers учитывает чёрный список ---
QgsSettings._store.clear()
st.save_settings({"disabled": ["L-line"]})
check("18e снэп: линия исключена настройками",
      [l.id() for l in se.get_snap_layers(None)] == ["L-poly"])
st.save_settings({"disabled": ["L-edit"]})
check("18e снэп: редактируемый слой тоже можно исключить",
      [l.id() for l in se.get_snap_layers(edit_l)] == ["L-poly", "L-line"])
st.save_settings({"disabled": ["L-poly", "L-line", "L-edit"]})
check("18e снэп: все исключены -> пустой список",
      se.get_snap_layers(edit_l) == [])
QgsSettings._store.clear()
check("18e снэп: без настроек поведение прежнее (редактируемый первым)",
      [l.id() for l in se.get_snap_layers(edit_l)] ==
      ["L-edit", "L-poly", "L-line"])

# --- 18f: допуски движка из настроек ---
QgsSettings._store.clear()
eng2 = se.SnappingEngine(None)  # canvas=None: px не конвертируются
check("18f движок: px, canvas None -> значения как есть",
      (eng2.tolerance_map_units(), eng2.vertex_tolerance_map_units())
      == (10.0, 15.0))
st.save_settings({"edge_tol": 3.0, "vertex_tol": 9.0,
                  "units": st.UNITS_MAP})
check("18f движок: единицы карты берутся напрямую (canvas не нужен)",
      (eng2.tolerance_map_units(), eng2.vertex_tolerance_map_units())
      == (3.0, 9.0))


class _FakeMS(object):
    @staticmethod
    def convertToMapUnits(value, unit):
        return value * 0.5


class _FakeCanvas(object):
    @staticmethod
    def mapSettings():
        return _FakeMS


QgsSettings._store.clear()
eng3 = se.SnappingEngine(_FakeCanvas)
check("18f движок: px конвертируются через mapSettings (x0.5)",
      (eng3.tolerance_map_units(), eng3.vertex_tolerance_map_units())
      == (5.0, 7.5))
eng4 = se.SnappingEngine(_FakeCanvas, tolerance_px=20.0)
check("18f движок: явный tolerance_px перекрывает рёбра (20*0.5=10)",
      (eng4.tolerance_map_units(), eng4.vertex_tolerance_map_units())
      == (10.0, 7.5))

# --- 18g: is_snap_disabled ---
QgsSettings._store.clear()
st.save_settings({"disabled": ["L-line"]})
check("18g is_snap_disabled: исключён -> True",
      st.is_snap_disabled(line_l) is True)
check("18g is_snap_disabled: не исключён -> False",
      st.is_snap_disabled(poly_l) is False)
check("18g is_snap_disabled: None -> False", st.is_snap_disabled(None) is False)

# --- 18h: сброс к значениям по умолчанию ---
st.save_settings({"edge_tol": 777.0, "vertex_tol": 888.0,
                  "units": st.UNITS_MAP, "disabled": ["x", "y"]})
check("18h reset: True", st.reset_to_defaults() is True)
check("18h reset: допуски по умолчанию",
      st.tolerance_values() == (10.0, 15.0, st.UNITS_PX))
check("18h reset: чёрный список очищен", st.disabled_layer_ids() == set())

# ---------------------------------------------------------------------------
# 19. TTL-кэш списка эталонных слоёв (v1.4.0): обход дерева панели НЕ
#     выполняется на каждый вызов get_snap_layers; чёрный список
#     применяется при каждом вызове БЕЗ повторного обхода.
# ---------------------------------------------------------------------------
_orig_clock = se._cache_clock


class _CountingRoot(_TreeRoot):
    walks = 0

    def checkedLayers(self):
        _CountingRoot.walks += 1
        return super(_CountingRoot, self).checkedLayers()


root19 = _CountingRoot(children=[
    _TreeNode(layer=poly_l),
    _TreeNode(layer=line_l),
])
QgsProject._inst = _FakeProject(root19)
se.invalidate_snap_layers_cache()

fake_time = [1000.0]
se._cache_clock = lambda: fake_time[0]

got_a = se.get_snap_layers(None)
check("19 кэш: первый вызов обходит дерево",
      _CountingRoot.walks == 1 and
      [l.id() for l in got_a] == ["L-poly", "L-line"])

se.get_snap_layers(None)
check("19 кэш: второй вызов взят из кэша (дерево не обходилось)",
      _CountingRoot.walks == 1)

fake_time[0] += se.CACHE_TTL - 0.01
se.get_snap_layers(None)
check("19 кэш: до истечения TTL список всё ещё кэширован",
      _CountingRoot.walks == 1)

fake_time[0] += 0.1  # за границей TTL
got_d = se.get_snap_layers(None)
check("19 кэш: после TTL дерево обходится заново",
      _CountingRoot.walks == 2 and
      [l.id() for l in got_d] == ["L-poly", "L-line"])

QgsSettings._store.clear()
st.save_settings({"disabled": ["L-line"]})
check("19 кэш: чёрный список фильтрует КАШИРОВАННЫЙ список",
      [l.id() for l in se.get_snap_layers(None)] == ["L-poly"])
check("19 кэш: фильтр чёрного списка НЕ приводит к обходу дерева",
      _CountingRoot.walks == 2)
QgsSettings._store.clear()

se.invalidate_snap_layers_cache()
se.get_snap_layers(None)
check("19 кэш: invalidate() вынуждает свежий обход",
      _CountingRoot.walks == 3)

got_e = se.get_snap_layers(edit_l)
check("19 кэш: редактируемый слой вставляется первым и в кэш-режиме",
      _CountingRoot.walks == 3 and
      [l.id() for l in got_e] == ["L-edit", "L-poly", "L-line"])

se._cache_clock = _orig_clock
se.invalidate_snap_layers_cache()

# ---------------------------------------------------------------------------
# 20. BaseTopoTool (v1.4.0): общее поведение трёх инструментов + логгер.
# ---------------------------------------------------------------------------
import topopolyedit.topo_tool_base as tb  # noqa: E402


class _FakeEvent(object):
    def __init__(self, key=None):
        self._key = key

    def key(self):
        return self._key


class _FakeAction(object):
    def __init__(self):
        self.checked = False

    def isChecked(self):
        return self.checked

    def setChecked(self, v):
        self.checked = v


class _FakeMessageBar(object):
    def __init__(self):
        self.messages = []

    def pushMessage(self, title, text, level=None, duration=0):
        self.messages.append((title, text, level, duration))


class _FakePanAction(object):
    def __init__(self):
        self.triggers = 0

    def trigger(self):
        self.triggers += 1


class _FakeCanvas(object):
    def __init__(self):
        self.tool = None

    def mapTool(self):
        return self.tool

    def setMapTool(self, t):
        self.tool = t

    def unsetMapTool(self, t):
        if self.tool is t:
            self.tool = None


class _FakeIface(object):
    def __init__(self):
        self.canvas = _FakeCanvas()
        self.bar = _FakeMessageBar()
        self.pan = _FakePanAction()

    def mapCanvas(self):
        return self.canvas

    def messageBar(self):
        return self.bar

    def actionPan(self):
        return self.pan


iface20 = _FakeIface()
act20 = _FakeAction()
tool = tb.BaseTopoTool(iface20, act20)
check("20 base: движок прилипания и состояние созданы",
      tool.engine is not None and tool.drag is None and tool.rubbers == [])

extra_calls = []
tool._cleanup_extra = lambda: extra_calls.append("extra")
tool.rubbers.append(QgsRubberBand(None, QgsWkbTypes.PolygonGeometry))
tool.drag = {"x": 1}
tool.snap_marker.show()
tool._cleanup()
check("20 base: _cleanup сбрасывает drag и ленты, вызывает хук подкласса",
      tool.drag is None and tool.rubbers == [] and extra_calls == ["extra"])

tool.drag = {"x": 1}
pan_before = iface20.pan.triggers
tool.keyPressEvent(_FakeEvent(key=Qt.Key.Key_Escape))
check("20 base: Esc при перетаскивании — только сброс, actionPan не дёргается",
      tool.drag is None and iface20.pan.triggers == pan_before)

tool.keyPressEvent(_FakeEvent(key=Qt.Key.Key_Escape))
check("20 base: Esc без перетаскивания — выход в панорамирование",
      iface20.pan.triggers == pan_before + 1)

tool.drag = {"x": 1}
act20.checked = True
tool.deactivate()
check("20 base: deactivate — cleanup + снятие галочки кнопки",
      tool.drag is None and act20.checked is False)

tool._push_error(ValueError("boom"))
check("20 base: _push_error — единое сообщение Critical в message bar",
      len(iface20.bar.messages) == 1 and
      iface20.bar.messages[0][1] == u"Ошибка: boom" and
      iface20.bar.messages[0][2] == Qgis.Critical)

tool.snap_marker.show()
tool._show_snap_feedback(None)
check("20 base: feedback(None) прячет маркер и линию привязки",
      tool.snap_marker._visible is False and tool.snap_seg.geom is None)

snap_e = se.SnapResult(pt(1, 2), "edge", None, 5, (pt(0, 0), pt(4, 4)))
tool._show_snap_feedback(snap_e)
check("20 base: feedback(edge) показывает маркер и ставит линию сегмента",
      tool.snap_marker._visible is True and tool.snap_seg.geom is not None)

snap_v = se.SnapResult(pt(3, 3), "vertex", None, 7)
tool._show_snap_feedback(snap_v)
check("20 base: feedback(vertex) сбрасывает линию ребра",
      tool.snap_seg.geom is None)

# --- подклассы: собственные маркеры сбрасываются через хук ---
import topopolyedit.topo_move_tool as tmt  # noqa: E402
import topopolyedit.topo_edge_move_tool as tet  # noqa: E402
import topopolyedit.topo_add_vertex_tool as tat  # noqa: E402

mtool = tmt.TopoMoveTool(iface20, _FakeAction())
mtool.hover_marker.show()
mtool._cleanup()
check("20 move: cleanup прячет hover-маркер через _cleanup_extra",
      mtool.hover_marker._visible is False)

etool = tet.TopoEdgeMoveTool(iface20, _FakeAction())
etool.snap_marker.show()
etool._cleanup()
check("20 edge: cleanup сбрасывает собственные ленты ребра",
      etool.drag_edge.geom is None and etool.hover_edge.geom is None and
      etool.rubbers == [] and etool.snap_marker._visible is False)

atool = tat.TopoAddVertexTool(iface20, _FakeAction())
atool.marker.show()
atool.edge_rb.setToGeometry(QgsGeometry.fromPolylineXY([pt(0, 0), pt(1, 1)]),
                            None)
atool._cleanup()
check("20 add: cleanup сбрасывает edge_rb и маркер вставки",
      atool.edge_rb.geom is None and atool.marker._visible is False)

pan_before = iface20.pan.triggers
atool.keyPressEvent(_FakeEvent(key=Qt.Key.Key_Escape))
check("20 add: Esc (drag всегда None) — выход в панорамирование",
      iface20.pan.triggers == pan_before + 1)

# --- логгер ---
import topopolyedit.utils as ut  # noqa: E402

QgsMessageLog.MESSAGES = []
ut.log(u"тест-сообщение")
check("20 лог: log() пишет в QgsMessageLog с тегом TopoPolyEdit и Warning",
      QgsMessageLog.MESSAGES[-1] == (u"тест-сообщение", u"TopoPolyEdit",
                                     Qgis.Warning))
ut.log(u"критично", level=Qgis.Critical)
check("20 лог: явный уровень передаётся в журнал",
      QgsMessageLog.MESSAGES[-1][2] == Qgis.Critical)
QgsMessageLog.MESSAGES = []

# ---------------------------------------------------------------------------
print("")
if FAILURES:
    print("ИТОГ: ПРОВАЛЕНО тестов: %d -> %s" % (len(FAILURES), FAILURES))
    sys.exit(1)
print("ИТОГ: все тесты пройдены")
