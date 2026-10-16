# -*- coding: utf-8 -*-
"""Офлайн-тесты геометрического ядра geo_utils.py (без запуска QGIS).

Подменяет модуль qgis.core минимальными стабами и проверяет:
  * нормализацию и сборку полигональной геометрии;
  * replace_vertices — топологическое перемещение общего узла;
  * find_vertex / find_segment / point_to_segment;
  * insert_vertex_topo — точное ребро, T-надмножество, T-подмножество,
    защита от дубликатов;
  * polys_bbox, contains_vertex;
  * translate_edge_vertices — промежуточные вершины общей границы (v1.6.3);
  * откат всей правки при сбое записи, лимиты аудита внутри одного фича.
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
        elif len(a) == 1 and isinstance(a[0], QgsRectangle):
            # реальный API допускает копию QgsRectangle(other)
            s = a[0]
            self.xmin, self.ymin = s.xmin, s.ymin
            self.xmax, self.ymax = s.xmax, s.ymax
        else:
            self.xmin = self.ymin = self.xmax = self.ymax = 0.0

    def grow(self, m):
        self.xmin -= m
        self.ymin -= m
        self.xmax += m
        self.ymax += m

    def xMinimum(self):
        return self.xmin

    def yMinimum(self):
        return self.ymin

    def xMaximum(self):
        return self.xmax

    def yMaximum(self):
        return self.ymax

    def width(self):
        return self.xmax - self.xmin

    def height(self):
        return self.ymax - self.ymin

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
    UnknownGeometry = 4
    GeometryCollection = 5


class QgsFeatureRequest(object):
    def __init__(self, rect=None):
        self.rect = rect
        self.fids = None

    def setSubsetOfAttributes(self, names):
        pass

    def setFilterFids(self, fids):
        """Этап 1: выборка по fid (используется _old_geoms валидатора)."""
        self.fids = list(fids)
        return self


class QgsFeature(object):
    """Этап 3: стаб для QgsSpatialIndex.addFeature/deleteFeature
    (ид + геометрия — больше от фичи индексу не нужно)."""

    def __init__(self, fid=None):
        self._fid = fid
        self._geom = None

    def setId(self, fid):
        self._fid = fid

    def id(self):
        return self._fid

    def setGeometry(self, geom):
        self._geom = geom

    def geometry(self):
        return self._geom


class QgsSpatialIndex(object):
    """Этап 3: стаб индекса по габаритам фич (как реальный:
    addFeature/deleteFeature/intersects; пересечение — по bbox)."""

    def __init__(self):
        self._rects = {}

    def addFeature(self, feat):
        g = feat.geometry()
        if g is None:
            return False
        self._rects[feat.id()] = g.boundingBox()
        return True

    def deleteFeature(self, feat):
        self._rects.pop(feat.id(), None)
        return True

    def intersects(self, rect):
        return [fid for fid, r in self._rects.items()
                if r.intersects(rect)]


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

    @classmethod
    def fromMultiPointXY(cls, pts):
        g = cls()
        g._data = {"type": QgsWkbTypes.PointGeometry, "multi": True,
                   "points": [list(pts)]}
        return g

    def isNull(self):
        return self._data is None

    def isEmpty(self):
        return self._data is None

    def isGeosValid(self):
        # v1.6.2: реальный API, зовётся из TopoEditor._apply_moves;
        # геометрии, собранные стаб-конструкторами, считаем валидными
        return self._data is not None

    def makeValid(self):
        # упрощение: валидный стаб-объект возвращаем как есть (копию)
        return QgsGeometry(self)

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

    def _iter_pts(self):
        d = self._data or {}
        if d.get("type") == QgsWkbTypes.PolygonGeometry:
            for poly in d["polys"]:
                for ring in poly:
                    for p in ring:
                        yield p
        elif d.get("type") == QgsWkbTypes.LineGeometry:
            for line in d["lines"]:
                for p in line:
                    yield p
        elif d.get("type") == QgsWkbTypes.PointGeometry:
            for part in d["points"]:
                for p in part:
                    yield p

    def boundingBox(self):
        """Этап 2: габарит (нужен find_overlaps «перемещаемые × перемещаемые»)."""
        xs, ys = [], []
        for p in self._iter_pts():
            xs.append(p.x())
            ys.append(p.y())
        if not xs:
            return QgsRectangle(0.0, 0.0, 0.0, 0.0)
        return QgsRectangle(min(xs), min(ys), max(xs), max(ys))

    def area(self):
        """Этап 2: площадь шиловой формулой (внешние минус внутренние)."""
        total = 0.0
        d = self._data or {}
        if d.get("type") != QgsWkbTypes.PolygonGeometry:
            return 0.0
        for poly in d["polys"]:
            for _ri, ring in enumerate(poly):
                pts = ring[:-1] if len(ring) > 1 and \
                    ring[0].x() == ring[-1].x() and \
                    ring[0].y() == ring[-1].y() else list(ring)
                s = 0.0
                n = len(pts)
                for i in range(n):
                    a, b = pts[i], pts[(i + 1) % n]
                    s += a.x() * b.y() - b.x() * a.y()
                s = abs(s) / 2.0
                total = s if _ri == 0 else total - s
        return total

    def intersects(self, other):
        # консервативно: пересечение габаритов
        return self.boundingBox().intersects(other.boundingBox())

    def intersection(self, other):
        """Стаб: площадьного пересечения не умеет — возвращает ОБЩУЮ
        ГРАНИЦУ (линию). find_overlaps получит не-полигон и пропустит
        пару (polygon_only -> None): смежные перемещаемые полигоны
        (общий край) перехлёстом не считаются — как в реальном GEOS."""
        if self._data is None or getattr(other, "_data", None) is None:
            return QgsGeometry()
        return QgsGeometry.fromPolylineXY([QgsPointXY(0, 0),
                                           QgsPointXY(0, 0)])


core_mod.QgsPointXY = QgsPointXY
core_mod.QgsRectangle = QgsRectangle
core_mod.QgsWkbTypes = QgsWkbTypes
core_mod.QgsGeometry = QgsGeometry
core_mod.QgsFeature = QgsFeature
core_mod.QgsSpatialIndex = QgsSpatialIndex
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
        ForbiddenCursor = 12  # этап 1: курсор при блокирующих нарушениях

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


class QTimer(object):
    """Стаб QTimer: singleShot запоминает выстрелы, тест выполняет сам."""

    _shots = []  # (msec, fn)

    @classmethod
    def singleShot(cls, msec, fn):
        cls._shots.append((msec, fn))

    @classmethod
    def clear(cls):
        cls._shots = []


qtcore_mod.QTimer = QTimer

qtgui_mod = types.ModuleType("qgis.PyQt.QtGui")


class QColor(object):
    def __init__(self, *a):
        if len(a) >= 3:
            self._rgb = (a[0], a[1], a[2])
        else:
            self._rgb = (0, 0, 0)

    def red(self):
        return self._rgb[0]

    def green(self):
        return self._rgb[1]

    def blue(self):
        return self._rgb[2]


qtgui_mod.QColor = QColor

qtwidgets_mod = types.ModuleType("qgis.PyQt.QtWidgets")


class QPushButton(object):
    """Стаб кнопки «Показать место» (этап 1): запоминает создание."""

    CLICKS = []  # [(text, parent)]

    def __init__(self, text, parent=None):
        self._text = text
        self._parent = parent
        self._handlers = []
        QPushButton.CLICKS.append((text, parent))

    def clicked(self):
        """Псевдосигнал: connect() запоминает обработчик."""
        btn = self

        class _Sig(object):
            def connect(self, handler):
                btn._handlers.append(handler)

        return _Sig()

    def click(self):
        for h in self._handlers:
            h()


def _run_pending_clicks():
    """Тест вручную вызывает обработчики созданных кнопок."""
    return QPushButton.CLICKS


class QMessageBox(object):
    Yes = 1
    No = 0

    @staticmethod
    def question(*a, **kw):
        return QMessageBox.No


qtwidgets_mod.QMessageBox = QMessageBox
qtwidgets_mod.QPushButton = QPushButton


class QApplication(object):
    """Стаб QApplication: занятость интерфейса для проверки _busy (v1.6.1)."""

    _modal = None    # активное модальное окно (None — нет)
    _buttons = 0     # битовая маска зажатых кнопок мыши (0 — не зажато)

    @staticmethod
    def activeModalWidget():
        return QApplication._modal

    @staticmethod
    def mouseButtons():
        return QApplication._buttons


qtwidgets_mod.QApplication = QApplication

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
    ICON_X = 2  # как в реальном API — подсветка узла-цели (v1.4.2)

    def __init__(self, canvas):
        self._center = None
        self._visible = False

    def setIconType(self, t):
        pass

    def setIconSize(self, size):
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
    ICON_BOX = 0  # как в реальном API (плоский доступ) — hints-ленты
    ICON_CIRCLE = 1
    ICON_X = 2  # крест ошибок «Аудита узлов» (v1.6.0)

    def __init__(self, canvas, gtype=None):
        self.geom = None

    def setStrokeColor(self, c):
        pass

    def setFillColor(self, c):
        pass

    def setWidth(self, w):
        pass

    def setIcon(self, icon):
        pass

    def setIconSize(self, size):
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
    def __eq__(self, other):
        # v1.6.3: CRS сравниваются ОБЪЕКТАМИ (кастомные СК с пустым
        # authid больше не считаются совпадающими)
        return isinstance(other, _Crs) and self.authid() == other.authid()

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

    def name(self):
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
r = se.SnappingEngine._pick((9.0, pt(1, 1), "L", 1, []),
                            (1.0, pt(2, 2), "L", 2, (pt(0, 0), pt(4, 4)), []))
check("приоритет: узел побеждает ребро, даже если ребро ближе",
      r is not None and r.snap_type == "vertex" and
      abs(r.point.x() - 1.0) < 1e-9)
r = se.SnappingEngine._pick(None, (1.0, pt(2, 2), "L", 2,
                                   (pt(0, 0), pt(4, 4)), []))
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
check("18c load: значения совпадают (новые ключи — дефолты)",
      st.load_settings() == {"edge_tol": 4.5, "vertex_tol": 6.5,
                             "units": st.UNITS_MAP, "disabled": ["a", "b"],
                             "overlap_mode": st.OVERLAP_ASK,
                             "show_hints": True,
                             "edge_slide": True,
                             "index_on": st.DEFAULT_INDEX_ON,
                             "max_neighbors": st.DEFAULT_MAX_NEIGHBORS,
                             "tile_factor": st.DEFAULT_TILE_FACTOR})
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
# 21. Регрессия v1.4.1: canvasPressEvent звал _update_preview(*t[1]) —
#     кортеж (dx, dy) распаковывался в 2 позиционных аргумента вместо
#     одного -> TypeError «takes 2 positional arguments but 3 were
#     given», инструмент рёбер умирал при каждом захвате ребра
#     (баг жил с v1.1.0: сигнатура (self, d_map) принимает ОДИН кортеж).
# ---------------------------------------------------------------------------
import inspect  # noqa: E402

src_press = inspect.getsource(tet.TopoEdgeMoveTool.canvasPressEvent)
check("21 edge: в canvasPressEvent нет распаковки _update_preview(*...)",
      "_update_preview(*" not in src_press)

args21 = inspect.getfullargspec(tet.TopoEdgeMoveTool._update_preview).args
check("21 edge: сигнатура _update_preview — (self, d_map), один кортеж",
      args21 == ["self", "d_map"])


class _FakeSnapEngine(object):
    """Изолирует тест от снэппинга: snap() всегда None."""

    hints = {"near": [], "target": []}

    def snap(self, point, exclude=None, editable_layer=None,
             accumulate_hints=False):
        return None

    def reset_hints(self):
        pass


etool.drag = {
    "layer": None,
    "anchors": [pt(0, 0), pt(10, 0)],
    "anchors_map": [pt(0, 0), pt(10, 0)],
    "press_map": pt(0, 0),
    "press_layer": pt(0, 0),
    "eps": 0.5,
    "match_eps": 1e-6,
    "ct_m2l": QgsCoordinateTransform(),
    "ct_l2m": QgsCoordinateTransform(),
    "entries": [],
    "exclude": set(),
}
etool.engine = _FakeSnapEngine()
try:
    # вызов РОВНО как в исправленном canvasPressEvent: [1] от
    # _translation_delta — это (dx, dy) ОДНИМ кортежем
    etool._update_preview(etool._translation_delta(pt(5, 3))[1])
    ok21 = etool.drag_edge.geom is not None
except TypeError:
    ok21 = False
check("21 edge: _update_preview((dx, dy)) одним кортежем заполняет ленту",
      ok21)
etool.drag = None
etool._cleanup()

# ---------------------------------------------------------------------------
# 22. Подсветка вершин прилипания + перехлёст (v1.5.0): hints рисуются
#     и без привязки («куда целиться»); cleanup сбрасывает ленты, hints
#     движка и красную область; заметки отчёта о перехлёсте.
# ---------------------------------------------------------------------------
tool.engine.hints = {"near": [pt(1, 1)],
                     "target": [pt(2, 2), pt(3, 3)]}
tool._show_snap_feedback(None)
check("22 hints: подсветка рисуется и без привязки (куда целиться)",
      tool.hint_near.geom is not None and
      tool.hint_target.geom is not None)

tool._show_snap_feedback(snap_v)
check("22 hints: при vertex-привязке кружок точки и подсветка на месте",
      tool.snap_marker._visible is True and
      tool.hint_target.geom is not None)

tool._cleanup()
check("22 hints: cleanup сбрасывает ленты, hints движка и красную область",
      tool.hint_near.geom is None and tool.hint_target.geom is None and
      tool.overlap_rb.geom is None and
      tool.engine.hints == {"near": [], "target": []})

tool.engine.hints = {"near": [pt(5, 5)], "target": []}
tool._show_hints()
tool._reset_hints()
check("22 hints: _reset_hints чистит ленты и hints движка",
      tool.hint_near.geom is None and
      tool.engine.hints == {"near": [], "target": []})

rep = {"neighbors_clipped": 2, "overlaps": 3,
       "overlap_choice": tb.CHOICE_KEEP}
parts = tool._overlap_notes(rep)
check("22 перехлёст: заметки отчёта (обрезано соседей / оставлен)",
      parts == [u"обрезано соседей: 2",
                u"перехлёст с 3 объектами оставлен"])

check("22 перехлёст: без событий — пустой список заметок",
      tool._overlap_notes({}) == [])

iface20.bar.messages = []
tool._push_cancelled()
check("22 перехлёст: сообщение об отмене правки в message bar",
      iface20.bar.messages[-1][2] == Qgis.Info and
      u"отменена" in iface20.bar.messages[-1][1])

# ---------------------------------------------------------------------------
# 23. Новое в v1.5.0: перехлёст с объектами своего слоя и подсветка.
#     polygon_only / union_bbox / find_overlaps / preview_overlaps /
#     _decide_overlap / collect_matching_points / enum_member.
# ---------------------------------------------------------------------------
import topopolyedit.geo_utils as tegu  # noqa: E402  (импорт пакета, не gu)


class _OvInter(object):
    """Результат intersection: полигон с площадью (утиная типизация)."""

    def __init__(self, area):
        self._area = area

    def isNull(self):
        return False

    def isEmpty(self):
        return False

    def type(self):
        return QgsWkbTypes.PolygonGeometry

    def area(self):
        return self._area


class _OvGeom(object):
    """Утиная геометрия: bbox + площадь + пересечение по запросу."""

    def __init__(self, x0, y0, x1, y1, area, inter_area=None):
        self.bb = QgsRectangle(x0, y0, x1, y1)
        self._area = area
        self._inter_area = inter_area
        self.intersection_calls = 0

    def boundingBox(self):
        return self.bb

    def area(self):
        return self._area

    def isNull(self):
        return False

    def isEmpty(self):
        return False

    def isGeosValid(self):
        return True

    def type(self):
        return QgsWkbTypes.PolygonGeometry

    def intersects(self, other):
        return self._inter_area is not None and \
            self.bb.intersects(other.bb)

    def intersection(self, other):
        self.intersection_calls += 1
        if self._inter_area is None:
            return None
        return _OvInter(self._inter_area)


# --- 23a: polygon_only ---
check("23a polygon_only: None и пустой -> None",
      te.polygon_only(None) is None and
      te.polygon_only(QgsGeometry()) is None)
g_poly = QgsGeometry.fromPolygonXY([ring((0, 0), (1, 0), (1, 1))])
check("23a polygon_only: полигон возвращается как есть",
      te.polygon_only(g_poly) is g_poly)
g_line = QgsGeometry.fromPolylineXY([pt(0, 0), pt(1, 1)])
check("23a polygon_only: не-полигон без коллекции -> None",
      te.polygon_only(g_line) is None)


class _Coll(object):
    def __init__(self, parts):
        self._parts = parts

    def isNull(self):
        return False

    def isEmpty(self):
        return False

    def type(self):
        return QgsWkbTypes.UnknownGeometry

    def asGeometryCollection(self):
        return self._parts


coll_one = _Coll([_OvGeom(0, 0, 1, 1, 1.0), g_line])
check("23a polygon_only: коллекция с 1 полигонной частью -> эта часть",
      te.polygon_only(_Coll([g_poly, g_line])) is g_poly)
check("23a polygon_only: коллекция только из линий -> None",
      te.polygon_only(_Coll([g_line])) is None)

# --- 23b: union_bbox ---
b = te.union_bbox([_OvGeom(0, 0, 1, 1, 1.0), _OvGeom(3, 2, 5, 4, 1.0)])
check("23b union_bbox: общий габарит двух геометрий",
      b is not None and b.xmin == 0 and b.ymin == 0 and
      b.xmax == 5 and b.ymax == 4)
check("23b union_bbox: пустой список -> None",
      te.union_bbox([]) is None)

# --- 23c: find_overlaps ---
moved = _OvGeom(0, 0, 2, 2, 4.0, inter_area=1.0)
neigh = _OvGeom(1, 1, 3, 3, 4.0)
res = te.find_overlaps([(1, moved)], [(10, neigh)])
check("23c find_overlaps: перехлёст найден с полями пары",
      len(res) == 1 and res[0]["moved"] == 1 and
      res[0]["neighbor"] == 10 and abs(res[0]["area"] - 1.0) < 1e-12 and
      res[0]["geom"] is not None)
sliver = _OvGeom(0, 0, 2, 2, 4.0, inter_area=1e-12)
check("23c find_overlaps: шов (площадь < порога) не перехлёст",
      te.find_overlaps([(1, sliver)], [(10, neigh)]) == [])
apart = _OvGeom(0, 0, 1, 1, 1.0, inter_area=None)
check("23c find_overlaps: далеко друг от друга — intersection не зовётся",
      te.find_overlaps([(1, apart)], [(10, _OvGeom(5, 5, 6, 6, 1.0))]) == [] and
      apart.intersection_calls == 0)
check("23c find_overlaps: intersection -> None не даёт записи",
      te.find_overlaps([(1, _OvGeom(0, 0, 2, 2, 4.0, inter_area=None))],
                       [(10, _OvGeom(1, 1, 3, 3, 4.0))]) == [])

# --- 23d: preview_overlaps (ничего не пишет; exclude работает) ---
lay_ov = _Layer([_Feat(10, _OvGeom(1, 1, 3, 3, 4.0))])
ed_ov = te.TopoEditor(None, lay_ov)
mg = _OvGeom(0, 0, 2, 2, 4.0, inter_area=1.0)
check("23d preview_overlaps: перехлёст с соседом найден",
      len(ed_ov.preview_overlaps([(1, mg)], {1})) == 1)
check("23d preview_overlaps: сосед в exclude — перехлёста нет",
      ed_ov.preview_overlaps([(1, mg)], {1, 10}) == [])

# --- 23e: _decide_overlap (настройки + callback) ---
QgsSettings._store.clear()
ed_d = te.TopoEditor(None, lay_ov)
st.save_settings({"overlap_mode": st.OVERLAP_CLIP})
called = []
ed_d.overlap_decider = lambda info: called.append(info) or st.CHOICE_CANCEL
check("23e decide: режим «всегда обрезать» — callback не зовётся",
      ed_d._decide_overlap([{"moved": 1, "neighbor": 2, "area": 1.0}]) == te.CHOICE_CLIP_MOVED and
      called == [])
st.save_settings({"overlap_mode": st.OVERLAP_KEEP})
check("23e decide: режим «не обрезать» — CHOICE_KEEP без callback",
      ed_d._decide_overlap([{"moved": 1, "neighbor": 2, "area": 1.0}]) == te.CHOICE_KEEP and called == [])
st.save_settings({"overlap_mode": st.OVERLAP_ASK})
ed_d.overlap_decider = lambda info: te.CHOICE_CLIP_NEIGHBORS
check("23e decide: режим «спрашивать» — ответ callback'а",
      ed_d._decide_overlap([{"moved": 1, "neighbor": 2, "area": 1.0}]) ==
      te.CHOICE_CLIP_NEIGHBORS)
ed_d.overlap_decider = lambda info: "мусор"
check("23e decide: неизвестный ответ callback'а -> CANCEL",
      ed_d._decide_overlap([{"moved": 1, "neighbor": 2, "area": 1.0}]) == te.CHOICE_CANCEL)


def _boom(info):
    raise RuntimeError("boom")


ed_d.overlap_decider = _boom
check("23e decide: сбой callback'а -> CANCEL (правку не применять вслепую)",
      ed_d._decide_overlap([{"moved": 1, "neighbor": 2, "area": 1.0}]) == te.CHOICE_CANCEL)
ed_d.overlap_decider = None
check("23e decide: «спрашивать» без callback (offline) -> KEEP",
      ed_d._decide_overlap([{"moved": 1, "neighbor": 2, "area": 1.0}]) == te.CHOICE_KEEP)

# --- 23f: collect_matching_points ---
sq = [[ring((0, 0), (2, 0), (2, 2), (0, 2))]]
mm = tegu.collect_matching_points(sq, pt(0, 0), 1e-12)
check("23f collect: вершина найдена, замыкающий дубликат не задвоен",
      len(mm) == 1 and abs(mm[0].x()) < 1e-12 and abs(mm[0].y()) < 1e-12)
check("23f collect: нет совпадения -> []",
      tegu.collect_matching_points(sq, pt(5, 5), 1e-12) == [])
two = [[ring((0, 0), (2, 0), (2, 2))],
       [ring((0, 0), (0, 2), (-2, 2))]]
mm2 = tegu.collect_matching_points(two, pt(0, 0), 1e-12)
check("23f collect: вершина в двух кольцах смежных полигонов — один раз",
      len(mm2) == 1)

# --- 23g: enum_member (PyQt5-плоский и PyQt6-областной доступ) ---
check("23g enum_member: плоский доступ (QgsRubberBand.ICON_BOX)",
      ut.enum_member(QgsRubberBand, "IconType", "ICON_BOX") ==
      QgsRubberBand.ICON_BOX)


class _EnumOwner(object):
    class IconType(object):
        ICON_BOX = 77


check("23g enum_member: областной доступ (PyQt6-стиль)",
      ut.enum_member(_EnumOwner, "IconType", "ICON_BOX") == 77)

# ---------------------------------------------------------------------------
# 25. Аудит узлов (node_audit, v1.6.0): classify_border_nodes — узлы
#     двух групп слоёв; красные крестики = узлы на чужой границе без
#     пары («лишние»/«недостающие» узлы). Ядро без QGIS.
# ---------------------------------------------------------------------------
import topopolyedit.node_audit as na  # noqa: E402


def _sq(x0, y0, x1, y1):
    """Квадрат: вершины и сегменты замкнутого контура (кортежи)."""
    corners = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    segs = [(corners[i], corners[(i + 1) % 4]) for i in range(4)]
    return list(corners), segs

# --- 25a: «недостающие» узлы — вершины B лежат на рёбрах A ---
pts_a, segs_a = _sq(0, 0, 4, 2)   # A: широкий квадрат 4x2
pts_b, segs_b = _sq(0, 0, 2, 2)   # B: половинка; узлы x=2 на рёбрах A
res = na.classify_border_nodes(pts_a, segs_a, pts_b, segs_b)
check("25a аудит: error_b = вершины B на рёбрах A (недостающие у A)",
      sorted(res["error_b"]) == [(2.0, 0.0), (2.0, 2.0)])
check("25a аудит: error_a пусто (узлы A спарены или вне B)",
      res["error_a"] == [])
check("25a аудит: углы B у общих узлов — plain",
      sorted(res["plain_b"]) == [(0.0, 0.0), (0.0, 2.0)])
check("25a аудит: все узлы A — plain (дальние не трогаем)",
      len(res["plain_a"]) == 4 and (4.0, 0.0) in res["plain_a"])

# --- 25b: «лишний» узел — вершина A на ребре B ---
pts_a2, segs_a2 = _sq(0, 0, 4, 2)
pts_a2.append((1.0, 0.0))                      # разбиение ребра y=0
segs_a2 = [((0, 0), (1, 0)), ((1, 0), (4, 0)),
           ((4, 0), (4, 2)), ((4, 2), (0, 2)), ((0, 2), (0, 0))]
pts_b2, segs_b2 = _sq(0, 0, 4, 2)              # B: ребро целиком
res2 = na.classify_border_nodes(pts_a2, segs_a2, pts_b2, segs_b2)
check("25b аудит: error_a = вершина A на ребре B (лишний узел A)",
      res2["error_a"] == [(1.0, 0.0)] and res2["error_b"] == [])

# --- 25c: вершина далеко от чужой границы — plain ---
pts_c = [(10.0, 10.0), (10.0, 11.0), (11.0, 11.0), (11.0, 10.0)]
segs_c = [(pts_c[i], pts_c[(i + 1) % 4]) for i in range(4)]
res3 = na.classify_border_nodes(pts_c, segs_c, pts_b2, segs_b2)
check("25c аудит: изолированный полигон — все plain, ошибок нет",
      res3["error_a"] == [] and len(res3["plain_a"]) == 4 and
      len(res3["plain_b"]) == 4 and res3["error_b"] == [])

# --- 25d: границы допусков (eps_touch=1e-4, eps_match=1e-6) ---
line_segs = [((0.0, 0.0), (4.0, 0.0))]
near_in = (2.0, 9e-5)     # ближе eps_touch -> ошибка
near_out = (2.0, 2e-4)    # дальше eps_touch -> plain
res4 = na.classify_border_nodes([near_in, near_out], [],
                                [], line_segs)
check("25d аудит: 9e-5 от чужой границы -> error, 2e-4 -> plain",
      res4["error_a"] == [near_in] and near_out in res4["plain_a"])
match_in = (2.0 + 5e-7, 0.0)     # в пределах eps_match от узла B
match_out = (2.0 + 5e-6, 0.0)    # вне eps_match, но на сегменте B
res5 = na.classify_border_nodes([match_in, match_out], [],
                                [(2.0, 0.0)], line_segs)
check("25d аудит: 5e-7 от узла B — совпадение (plain); 5e-6 — error",
      match_in in res5["plain_a"] and res5["error_a"] == [match_out])

# --- 25e: зазор (sliver) между слоями — НЕ ошибка узлов ---
pts_g1, segs_g1 = _sq(0, 0, 4, 2)
pts_g2 = [(x, y + 2.5) for x, y in _sq(0, 0, 4, 2)[0]]  # выше на 2.5
segs_g2 = [(pts_g2[i], pts_g2[(i + 1) % 4]) for i in range(4)]
res6 = na.classify_border_nodes(pts_g1, segs_g1, pts_g2, segs_g2)
check("25e аудит: зазор 2.5 между границами — ошибок узлов нет",
      res6["error_a"] == [] and res6["error_b"] == [] and
      len(res6["plain_a"]) == 4 and len(res6["plain_b"]) == 4)

# --- 25f: дедупликация вершин-двойников смежных полигонов ---
dup_pts = [(0.0, 0.0), (0.0, 0.0), (2.0, 0.0), (2.0 + 5e-9, 0.0),
           (5.0, 5.0)]
check("25f дедуп: двойники и 5e-9-близнецы склеены",
      na.dedup_points(dup_pts) == [(0.0, 0.0), (2.0, 0.0), (5.0, 5.0)])
res7 = na.classify_border_nodes(dup_pts, [], [], [])
check("25f дедуп: classify выдаёт уникальные узлы",
      sorted(res7["plain_a"]) == [(0.0, 0.0), (2.0, 0.0), (5.0, 5.0)])

# --- 25g: вырожденные входы и лимиты ---
res8 = na.classify_border_nodes([], [], [], [])
check("25g аудит: пустые входы -> пустые списки без падений",
      res8 == {"plain_a": [], "error_a": [],
               "plain_b": [], "error_b": []})
many = [(float(i), 0.0) for i in range(na.MAX_NODES + 500)]
res9 = na.classify_border_nodes(many, [], [], [])
check("25g аудит: лимит MAX_NODES не превышен",
      len(res9["plain_a"]) == na.MAX_NODES)
check("25g аудит: мусорные точки отбрасываются",
      na.dedup_points([(1, 1), "x", None, (2, 2)]) == [(1.0, 1.0),
                                                       (2.0, 2.0)])

# ---------------------------------------------------------------------------
# 26. Оверлей «Аудит узлов» (audit_overlay, v1.6.0): включение собирает
#     узлы обеих групп в видимой области, рисует 3 ленты, показывает
#     сводку; выключение чистит ленты; дебаунс пан/зума через QTimer.
# ---------------------------------------------------------------------------
import topopolyedit.audit_overlay as ao  # noqa: E402


class _AuditSignal(object):
    def __init__(self):
        self.slots = []

    def connect(self, fn):
        self.slots.append(fn)

    def disconnect(self, fn):
        if fn in self.slots:
            self.slots.remove(fn)

    def emit(self, *a):
        for fn in list(self.slots):
            fn(*a)


class _AuditFeat(object):
    def __init__(self, fid, g):
        self._fid = fid
        self._g = g

    def id(self):
        return self._fid

    def geometry(self):
        return self._g


class _AuditLyr(QgsVectorLayer):
    """Полигональный слой: проходит is_polygon_layer (isinstance)."""

    def __init__(self, lid, feats, editable=False):
        self._lid = lid
        self._feats = feats
        self._editable = editable
        self.layerModified = _AuditSignal()

    def id(self):
        return self._lid

    def name(self):
        return self._lid

    def isValid(self):
        return True

    def isEditable(self):
        return self._editable

    def type(self):
        return QgsMapLayerType.VectorLayer

    def geometryType(self):
        return QgsWkbTypes.PolygonGeometry

    def crs(self):
        return _Crs("EPSG:3857")

    def getFeatures(self, req):
        return list(self._feats)


class _AuditProject(object):
    def __init__(self, root, layers):
        self._root = root
        self._layers = layers

    def layerTreeRoot(self):
        return self._root

    def mapLayers(self):
        return dict(self._layers)


class _AuditMS(object):
    @staticmethod
    def destinationCrs():
        return _Crs("EPSG:3857")


class _AuditCanvas(object):
    def __init__(self):
        self.extentChanged = _AuditSignal()

    def extent(self):
        return QgsRectangle(-10, -10, 10, 10)

    def mapSettings(self):
        return _AuditMS


class _AuditIface(object):
    def __init__(self, edit_layer):
        self.canvas = _AuditCanvas()
        self.bar = _FakeMessageBar()
        self._edit = edit_layer

    def mapCanvas(self):
        return self.canvas

    def messageBar(self):
        return self.bar

    def activeLayer(self):
        return self._edit


# сцена: редактируемый A (0..4 x 0..2), эталон B (0..2 x 0..2)
g_edit = QgsGeometry.fromPolygonXY(
    [ring((0, 0), (4, 0), (4, 2), (0, 2))])
g_ref = QgsGeometry.fromPolygonXY(
    [ring((0, 0), (2, 0), (2, 2), (0, 2))])
lyr_edit = _AuditLyr("A-edit", [_AuditFeat(1, g_edit)], editable=True)
lyr_ref = _AuditLyr("B-ref", [_AuditFeat(2, g_ref)])
audit_root = _TreeRoot(children=[
    _TreeNode(layer=lyr_edit), _TreeNode(layer=lyr_ref)])
QgsProject._inst = _AuditProject(audit_root,
                                 {"A-edit": lyr_edit, "B-ref": lyr_ref})
se.invalidate_snap_layers_cache()  # TTL-кэш эталонных слоёв секций 17-19
QgsSettings._store.clear()
QTimer.clear()

audit = ao.NodeAuditOverlay(_AuditIface(lyr_edit))
check("26 оверлей: изначально выключен, ленты пусты",
      audit.enabled is False and audit.band_edit.geom is None)

audit.set_enabled(True)
check("26 оверлей: включен, extentChanged подключен",
      audit.enabled is True and len(audit.canvas.extentChanged.slots) == 1)
check("26 оверлей: узлы редактируемого слоя на оранжевой ленте (4 угла)",
      audit.band_edit.geom is not None and
      len(audit.band_edit.geom._data["points"][0]) == 4)
check("26 оверлей: узлы эталона на синей ленте (4 угла)",
      audit.band_ref.geom is not None and
      len(audit.band_ref.geom._data["points"][0]) == 4)
err_pts = [(round(p.x(), 9), round(p.y(), 9))
           for p in audit.band_err.geom._data["points"][0]]
check("26 оверлей: крестики — вершины B на рёбрах A (недостающие)",
      err_pts == [(2.0, 0.0), (2.0, 2.0)])
check("26 оверлей: сводка при включении — 4/4 узла, 2 ошибки",
      len(audit.iface.bar.messages) == 1 and
      u"Аудит узлов" == audit.iface.bar.messages[0][0] and
      u"— 2 (красные крестики)" in audit.iface.bar.messages[0][1] and
      audit.iface.bar.messages[0][2] == Qgis.Warning)

# --- дебаунс пан/зума: сигнал -> QTimer -> пересчёт ---
QTimer.clear()
audit.canvas.extentChanged.emit()
check("26 оверлей: extentChanged ставит отложенный пересчёт",
      audit._pending is True and len(QTimer._shots) == 1)
QTimer._shots[0][1]()  # выстрел таймера
check("26 оверлей: выстрел таймера выполняет пересчёт",
      audit._pending is False)

# --- правка слоя: layerModified -> schedule_refresh ---
QTimer.clear()
lyr_ref.layerModified.emit()
check("26 оверлей: layerModified слоя ставит отложенный пересчёт",
      len(QTimer._shots) == 1)
QTimer._shots[0][1]()

# --- усечение: маленький MAX_NODES -> предупреждение ---
iface26 = audit.iface
iface26.bar.messages = []
saved_max = na.MAX_NODES
try:
    na.MAX_NODES = 2
    audit.refresh()
finally:
    na.MAX_NODES = saved_max
check("26 оверлей: усечение — подсказка «приблизьте масштаб»",
      len(iface26.bar.messages) == 1 and
      u"Приблизьте" in iface26.bar.messages[0][1] and
      iface26.bar.messages[0][2] == Qgis.Warning)

# --- выключение: ленты чистятся, сигналы отключаются ---
audit.set_enabled(False)
check("26 оверлей: выключение чистит ленты и отключает сигналы",
      audit.enabled is False and audit.band_edit.geom is None and
      audit.band_ref.geom is None and audit.band_err.geom is None and
      audit.canvas.extentChanged.slots == [] and
      audit._last is None)

# --- без слоёв: не падает, сообщение не ошибка ---
QgsProject._inst = _AuditProject(_TreeRoot(children=[]), {})
se.invalidate_snap_layers_cache()
empty_iface = _AuditIface(None)
empty_audit = ao.NodeAuditOverlay(empty_iface)
empty_audit.set_enabled(True)
check("26 оверлей: без слоёв — включается, ленты пусты, сводка 0/0/0",
      empty_audit.enabled is True and empty_audit.band_edit.geom is None and
      u"— 0 (красные крестики)" in empty_audit.iface.bar.messages[-1][1])
empty_audit.set_enabled(False)

# ---------------------------------------------------------------------------
# 27. Настройки «Аудита узлов» (v1.6.0): audit_enabled/set_audit_enabled —
#     состояние кнопки запоминается между сеансами QGIS.
# ---------------------------------------------------------------------------
QgsSettings._store.clear()
check("27 аудит-настройки: по умолчанию выключен",
      st.audit_enabled() is False)
check("27 аудит-настройки: сохранение возвращает True",
      st.set_audit_enabled(True) is True)
check("27 аудит-настройки: состояние читается обратно",
      st.audit_enabled() is True)
st.set_audit_enabled(False)
check("27 аудит-настройки: выключение сохраняется",
      st.audit_enabled() is False)
check("27 аудит-настройки: ключ изолирован от load_settings",
      "audit" not in str(st.load_settings()))

# ---------------------------------------------------------------------------
# 28. Невмешательство «Аудита узлов» в интерфейс (v1.6.1): отложенный
#     пересчёт НЕ выполняется при открытом модальном диалоге или зажатой
#     мыши (запрос не теряется — откладывается), сбор ограничен бюджетом
#     времени; ленты создаются без кэша в стабах (setCacheMode гасится).
# ---------------------------------------------------------------------------
QgsProject._inst = _AuditProject(audit_root,
                                 {"A-edit": lyr_edit, "B-ref": lyr_ref})
se.invalidate_snap_layers_cache()
QgsSettings._store.clear()
QTimer.clear()
ao.QApplication._modal = None
ao.QApplication._buttons = 0

ov28 = ao.NodeAuditOverlay(_AuditIface(lyr_edit))
check("28 занятость: по умолчанию интерфейс свободен",
      ov28._busy() is False)

ao.QApplication._modal = object()
check("28 занятость: открыт модальный диалог (например, перехлёст)",
      ov28._busy() is True)
ao.QApplication._modal = None

ao.QApplication._buttons = 1
check("28 занятость: зажата кнопка мыши (перетаскивание/пан)",
      ov28._busy() is True)
ao.QApplication._buttons = 0
check("28 занятость: мышь отпущена — интерфейс снова свободен",
      ov28._busy() is False)

# --- запрос на пересчёт при занятом интерфейсе откладывается, не теряется ---
ov28.enabled = True
ov28._pending = False
calls28 = []
ov28.refresh = lambda *a, **kw: calls28.append(1)  # monkeypatch
ao.QApplication._buttons = 1
QTimer.clear()
ov28.schedule_refresh()
check("28 отложенный: сигнал ставит запрос в очередь",
      ov28._pending is True and len(QTimer._shots) == 1)
QTimer._shots[-1][1]()  # выстрел при зажатой мыши
check("28 отложенный: занятый интерфейс — пересчёт отложен ещё раз",
      calls28 == [] and ov28._pending is True and
      len(QTimer._shots) == 2 and
      QTimer._shots[-1][0] == ao.REFRESH_DELAY_MS)
ao.QApplication._buttons = 0
QTimer._shots[-1][1]()  # выстрел после отпускания кнопки
check("28 отложенный: после освобождения интерфейса пересчёт выполнен",
      calls28 == [1] and ov28._pending is False)
ov28.enabled = False

# --- бюджет времени: сбор останавливается, группа помечается усечённой ---
many_feats = [_AuditFeat(i, QgsGeometry.fromPolygonXY(
    [ring((0, 0), (4, 0), (4, 2), (0, 2))])) for i in range(1000)]
lyr_many = _AuditLyr("A-many", many_feats, editable=True)
iface28b = _AuditIface(lyr_many)
ov28b = ao.NodeAuditOverlay(iface28b)
dest28 = iface28b.canvas.mapSettings().destinationCrs()
raw28 = []
segs28 = []
saved_budget = ao.COLLECT_BUDGET_S
try:
    stopped28 = ov28b._collect_layer(lyr_many, iface28b.canvas.extent(),
                                     dest28, raw28, segs28,
                                     time.monotonic() - 1.0)
    check("28 бюджет: дедлайн в прошлом — сбор мгновенно остановлен",
          stopped28 is True and len(raw28) == 0 and len(segs28) == 0)
    ao.COLLECT_BUDGET_S = -1.0
    pts_g, segs_g, tr_g = ov28b._collect_group(
        [lyr_many], iface28b.canvas.extent(), dest28)
finally:
    ao.COLLECT_BUDGET_S = saved_budget
check("28 бюджет: _collect_group помечает усечение по времени",
      tr_g is True and len(pts_g) == 0 and len(segs_g) == 0)
raw28b = []
stopped28b = ov28b._collect_layer(lyr_many, iface28b.canvas.extent(),
                                  dest28, raw28b, [])
check("28 бюджет: без дедлайна слой собирается целиком",
      stopped28b is False and len(raw28b) == 4000)

# --- лимиты вершин/сегментов срабатывают ВНУТРИ одного огромного фича --
huge_ring = [QgsPointXY(i * 0.001, (i % 7) * 0.001)
             for i in range(na.MAX_RAW_POINTS + 10)]
huge_ring.append(QgsPointXY(huge_ring[0].x(), huge_ring[0].y()))
feat_huge = _AuditFeat(0, QgsGeometry.fromPolygonXY([huge_ring]))
lyr_huge = _AuditLyr("A-huge", [feat_huge], editable=True)
raw_h = []
segs_h = []
stopped_h = ov28b._collect_layer(lyr_huge, iface28b.canvas.extent(),
                                 dest28, raw_h, segs_h, None)
check("28 лимит: одна фича с >MAX_RAW_POINTS вершин усечена по лимиту",
      stopped_h is True and len(raw_h) == na.MAX_RAW_POINTS)

# ---------------------------------------------------------------------------
# 29. Кнопка «Отменить» (v1.6.2). Штатная отмена QGIS привязана к
#     АКТИВНОМУ слою, а плагин правит любой слой в режиме правки — поэтому
#     ядро запоминает последний изменённый слой (note_edited_layer) и
#     кнопка ищет цель отката: 1) последний слой плагина, 2) активный,
#     3) любой подходящий (полигон, в правке, undoStack().canUndo()).
# ---------------------------------------------------------------------------
import topopolyedit.utils as ut  # noqa: E402  (псевдоним секции)


class _UndoStack(object):
    def __init__(self, can_undo):
        self._can = can_undo
        self.undone = 0

    def canUndo(self):
        return self._can

    def undo(self):
        if self._can:
            self.undone += 1
            self._can = False


class _UndoLyr(QgsVectorLayer):
    def __init__(self, name, editable, can_undo,
                 gtype=QgsWkbTypes.PolygonGeometry):
        self._name = name
        self._gt = gtype
        self._ed = editable
        self.stack = _UndoStack(can_undo)

    def name(self):
        return self._name

    def id(self):
        return "id-" + self._name

    def isValid(self):
        return True

    def geometryType(self):
        return self._gt

    def isEditable(self):
        return self._ed

    def undoStack(self):
        return self.stack


class _DeadLyr(QgsVectorLayer):
    """Слой, удалённый из проекта: обращение к C++ объекту падает."""

    def __getattr__(self, item):
        raise RuntimeError(
            "wrapped C/C++ object of type QgsVectorLayer has been deleted")


ut.note_edited_layer(None)
check("29 note/last: после сброса last_edited_layer() -> None",
      ut.last_edited_layer() is None)

la = _UndoLyr("L-A", True, True)
lb = _UndoLyr("L-B", True, True)
ut.note_edited_layer(la)
check("29 note/last: note_edited_layer запоминает слой",
      ut.last_edited_layer() is la)
ut.note_edited_layer(lb)
check("29 note/last: повторный вызов замещает предыдущий слой",
      ut.last_edited_layer() is lb)

check("29 цель: последний слой плагина побеждает активный",
      ut.find_undo_target([lb, la], last_edited=la, active=lb) is la)
check("29 цель: активный слой, если плагин ничего не менял",
      ut.find_undo_target([lb, la], last_edited=None, active=la) is la)
check("29 цель: без памяти и активного — первый слой в правке с историей",
      ut.find_undo_target([lb, la], last_edited=None, active=None) is lb)
check("29 цель: все негодны (не в правке / пустой стек) -> None",
      ut.find_undo_target([_UndoLyr("L-C", False, True),
                           _UndoLyr("L-D", True, False)],
                          last_edited=None, active=None) is None)
check("29 цель: неполигональный слой (линия) не цель отката",
      ut.find_undo_target(
          [_UndoLyr("L-line", True, True, QgsWkbTypes.LineGeometry)],
          last_edited=None, active=None) is None)
check("29 цель: кандидатов нет вовсе -> None",
      ut.find_undo_target([], last_edited=None, active=None) is None)

dead = _DeadLyr()
check("29 цель: удалённый слой (RuntimeError) молча пропускается",
      ut.find_undo_target([dead, la], last_edited=dead, active=None) is la)
check("29 can_undo: удалённый слой -> False (без исключения)",
      ut.can_undo(dead) is False)
check("29 can_undo: None -> False", ut.can_undo(None) is False)
check("29 can_undo: слой вне правки -> False",
      ut.can_undo(_UndoLyr("L-off", False, True)) is False)


# --- хук ядра: после успешной операции запомнен редактируемый слой -------
class _UndoCrs(object):
    def __eq__(self, other):
        return isinstance(other, _UndoCrs) and self.authid() == other.authid()

    def authid(self):
        return "EPSG:4326"


class _UndoFeat(object):
    def __init__(self, fid, geom):
        self._fid = fid
        self._g = geom

    def id(self):
        return self._fid

    def geometry(self):
        return self._g


class _UndoEditLyr(object):
    """Фейковый слой правки: чтение фич + запись edit-командами."""

    def __init__(self, feats):
        self._feats = feats
        self.stack = _UndoStack(True)
        self.commands = []
        self.written = {}
        self.repainted = 0

    def id(self):
        return "L-edit"

    def name(self):
        return "L-edit"

    def isValid(self):
        return True

    def crs(self):
        return _UndoCrs()

    def geometryType(self):
        return QgsWkbTypes.PolygonGeometry

    def isEditable(self):
        return True

    def undoStack(self):
        return self.stack

    def getFeatures(self, req):
        return list(self._feats)

    def beginEditCommand(self, text):
        self.commands.append(("begin", text))

    def endEditCommand(self):
        self.commands.append(("end", None))

    def destroyEditCommand(self):
        self.commands.append(("destroy", None))

    def changeGeometry(self, fid, g):
        self.written[fid] = g
        return True

    def triggerRepaint(self):
        self.repainted += 1


# проект без других слоёв: эталонов для автоотсечения нет
QgsProject._inst = _FakeProject(_TreeRoot(children=[]))

feat_a = _UndoFeat(1, QgsGeometry.fromPolygonXY(
    [ring((0, 0), (2, 0), (2, 2), (2, 4), (0, 4))]))
feat_b = _UndoFeat(2, QgsGeometry.fromPolygonXY(
    [ring((2, 0), (5, 0), (5, 4), (2, 4), (2, 2))]))

ed_layer = _UndoEditLyr([feat_a, feat_b])
ut.note_edited_layer(None)
ed2 = te.TopoEditor(None, ed_layer)
rep = ed2.move_vertex(pt(2, 2), pt(3, 3))
check("29 хук: move_vertex применился (A и B обновлены)",
      rep["error"] is None and rep["moved"] == 2 and
      len(ed_layer.written) == 2)
check("29 хук: после успешной move_vertex last_edited_layer() — слой правки",
      ut.last_edited_layer() is ed_layer)

ed_layer2 = _UndoEditLyr([feat_a])
ut.note_edited_layer(None)
ed3 = te.TopoEditor(None, ed_layer2)
rep3 = ed3.move_vertex(pt(0, 0), pt(1, 1))
check("29 хук: правка другого слоя замещает запомненный",
      rep3["error"] is None and ut.last_edited_layer() is ed_layer2)

rep4 = ed3.move_vertex(pt(0, 0), pt(0, 0))
check("29 хук: операция без смещения — ошибка, слой-цель не меняется",
      rep4["error"] == u"узел не смещён" and
      ut.last_edited_layer() is ed_layer2)

ed_layer3 = _UndoEditLyr([feat_a, feat_b])
ut.note_edited_layer(None)
ed4 = te.TopoEditor(None, ed_layer3)
rep5 = ed4.add_vertex_on_edge(pt(2, 0), pt(2, 2), pt(2, 1))
check("29 хук: add_vertex_on_edge применился и запомнил слой",
      rep5["error"] is None and rep5["features"] >= 1 and
      ut.last_edited_layer() is ed_layer3)

# ---------------------------------------------------------------------------
# 30. Вливание кандидата-фикса (v1.6.3): translate_edge_vertices,
#     откат всей правки при сбое записи, отмена при >MAX_OVERLAP_NEIGHBORS
# ---------------------------------------------------------------------------

# --- промежуточные коллинеарные вершины на общей границе -----------------
polys_a = [[[QgsPointXY(0, 0), QgsPointXY(2, 0), QgsPointXY(4, 0),
             QgsPointXY(4, 4), QgsPointXY(0, 4), QgsPointXY(0, 0)]]]
anchors30 = [QgsPointXY(0, 0), QgsPointXY(4, 0)]
new30, ch30 = gu.translate_edge_vertices(polys_a, anchors30, (1, 0),
                                         gu.TOPO_MATCH_EPS2)
xs30 = [p.x() for p in new30[0][0]]
check("30 границы: промежуточная коллинеарная вершина двигается с ребром",
      ch30 == 4 and xs30 == [1.0, 3.0, 5.0, 4.0, 0.0, 1.0])

# вершина на той же прямой, но ЗА концом цепочки — не двигается
polys_c = [[[QgsPointXY(8, 0), QgsPointXY(12, 0), QgsPointXY(12, 4),
             QgsPointXY(8, 4), QgsPointXY(8, 0)]]]
new_c, ch_c = gu.translate_edge_vertices(polys_c, anchors30, (1, 0),
                                         gu.TOPO_MATCH_EPS2)
check("30 границы: вершина вне цепочки (за её концом) не сдвигается",
      ch_c == 0)

# вершина НА линии цепочки, но без смежного ребра вдоль неё — не двигается
polys_h = [[[QgsPointXY(-4, -4), QgsPointXY(4, -4), QgsPointXY(4, -8),
             QgsPointXY(-4, -8), QgsPointXY(-4, -4)],
            [QgsPointXY(1, 0), QgsPointXY(3, 1), QgsPointXY(5, 0),
             QgsPointXY(1, 0)]]]
new_h, ch_h = gu.translate_edge_vertices(polys_h, anchors30, (1, 0),
                                         gu.TOPO_MATCH_EPS2)
check("30 границы: вершина дыры на линии цепочки без ребра вдоль неё — не сдвигается",
      ch_h == 0)

# --- сбой записи одной фичи откатывает ВСЮ команду -----------------------
class _FailEditLyr(_UndoEditLyr):
    """changeGeometry возвращает False для заданной фичи."""

    def __init__(self, feats, fail_fid):
        _UndoEditLyr.__init__(self, feats)
        self._fail_fid = fail_fid
        self.destroyed = 0

    def changeGeometry(self, fid, g):
        if fid == self._fail_fid:
            return False
        return _UndoEditLyr.changeGeometry(self, fid, g)

    def destroyEditCommand(self):
        self.commands.append(("destroy", None))
        self.destroyed += 1


fail_lyr = _FailEditLyr([feat_a, feat_b], fail_fid=2)
ut.note_edited_layer(None)
ed5 = te.TopoEditor(None, fail_lyr)
rep_f = ed5.move_vertex(pt(2, 2), pt(3, 3))
check("30 откат: сбой записи фичи отменяет всю команду целиком",
      rep_f["error"] is not None and rep_f["moved"] == 0 and
      fail_lyr.destroyed >= 1)
check("30 откат: после отката слой НЕ запоминается как последний правленый",
      ut.last_edited_layer() is None)

# --- соседей больше MAX_OVERLAP_NEIGHBORS — операция отменяется ---------
fillers30 = [_UndoFeat(100 + i, QgsGeometry.fromPolygonXY(
    [ring((20, 20), (22, 20), (22, 22), (20, 22))]))
    for i in range(te.MAX_OVERLAP_NEIGHBORS + 1)]
dense_lyr = _UndoEditLyr([feat_a, feat_b] + fillers30)
ed6 = te.TopoEditor(None, dense_lyr)
rep_d = ed6.move_vertex(pt(2, 2), pt(3, 3))
check("30 соседи: больше MAX_OVERLAP_NEIGHBORS — операция отменена",
      rep_d["error"] is not None and rep_d["moved"] == 0)

# ---------------------------------------------------------------------------
# 31. T-примыкания (этап 2 ТЗ): find_t_edges / chain_riders / slide_delta
# ---------------------------------------------------------------------------

# A: прямоугольник (0,0)-(20,10); pt строго внутри верхнего ребра
poly_a = [[ring((0, 0), (20, 0), (20, 10), (0, 10))]]
t_edges = gu.find_t_edges(poly_a, pt(5, 10), 1e-12)
check("31 find_t_edges: точка на ребре без вершины найдена",
      len(t_edges) == 1 and
      ((t_edges[0][0].x(), t_edges[0][0].y()) in
       ((0.0, 10.0), (20.0, 10.0))) and
      ((t_edges[0][1].x(), t_edges[0][1].y()) in
       ((0.0, 10.0), (20.0, 10.0))))
check("31 find_t_edges: точка-вершина не T-примыкание",
      gu.find_t_edges(poly_a, pt(0, 10), 1e-12) == [])
check("31 find_t_edges: точка вне рёбер — пусто",
      gu.find_t_edges(poly_a, pt(5, 5), 1e-12) == [])
check("31 find_t_edges: точка чуть вне ребра за допуском — пусто",
      gu.find_t_edges(poly_a, pt(5, 10.001), 1e-12) == [])

# дедупликация: одно и то же ребро в двух частях фичи
poly_dup = [[ring((0, 0), (10, 0), (10, 10), (0, 10)),
             ring((10, 0), (20, 0), (20, 10), (10, 10))]]
check("31 find_t_edges: дубликат ребра (две части) вернулся один раз",
      len(gu.find_t_edges(poly_dup, pt(5, 0), 1e-12)) == 1)

# chain_riders: T-вершина B(5,0) на цепочке A y=0, соседи вне цепочки
poly_t = [[ring((5, -5), (5, 0), (8, 3), (2, -2))]]
riders = gu.chain_riders(poly_t, [pt(0, 0), pt(10, 0)], 1e-12)
check("31 chain_riders: T-вершина сбоку — пассажир",
      len(riders) == 1 and abs(riders[0].x() - 5.0) < 1e-9 and
      abs(riders[0].y()) < 1e-9)
# коллинеарный попутчик (сосед по кольцу на той же цепочке) — НЕ пассажир
poly_follow = [[ring((0, 0), (5, 0), (10, 0), (10, 5), (0, 5))]]
check("31 chain_riders: коллинеарный попутчик не пассажир",
      gu.chain_riders(poly_follow, [pt(0, 0), pt(10, 0)], 1e-12) == [])
# якорная копия (вершина совпадает с якорем) — не пассажир
poly_anchor = [[ring((0, 0), (10, 0), (10, 5), (0, 5))]]
check("31 chain_riders: якорная вершина не пассажир",
      gu.chain_riders(poly_anchor, [pt(0, 0), pt(10, 0)], 1e-12) == [])
# вершина на ПРОДОЛЖЕНИИ цепочки за её концом — не пассажир (не на рёбрах)
poly_line = [[ring((5, 15), (5, 20), (8, 23), (2, 18))]]
check("31 chain_riders: точка на продолжении цепочки — не пассажир",
      gu.chain_riders(poly_line, [pt(10, 20), pt(20, 20)], 1e-12) == [])
# вершина дыры на прямой цепочки, но вне её рёбер — не пассажир
poly_hole = [[ring((0, 0), (20, 0), (20, 20), (0, 20)),
              ring((5, 5), (5, 15), (15, 15), (15, 5))]]
check("31 chain_riders: точка на линии цепочки вне сегментов — не пассажир",
      gu.chain_riders(poly_hole, [pt(0, 30), pt(20, 30)], 1e-12) == [])

# slide_delta: проекция переноса на направление ребра
check("31 slide_delta: горизонтальное ребро съедает dy",
      gu.slide_delta((3, 4), (1, 0)) == (3.0, 0.0))
check("31 slide_delta: вертикальное ребро съедает dx",
      gu.slide_delta((3, 4), (0, 2)) == (0.0, 4.0))
sd = gu.slide_delta((3, 4), (1, 1))
check("31 slide_delta: диагональ 45° — равные компоненты",
      abs(sd[0] - 3.5) < 1e-9 and abs(sd[1] - 3.5) < 1e-9)
check("31 slide_delta: нулевое направление — вектор без изменений",
      gu.slide_delta((3, 4), (0, 0)) == (3, 4))

# ---------------------------------------------------------------------------
# 32. Интеграция залатывания (этап 2): T-узел тянет ребро соседа
# ---------------------------------------------------------------------------

# приёмка этапа 2, тест 1: B имеет вершину на ребре A без пары;
# после patch+move в A появляется вершина, оба полигона двигаются
polys_a32 = [[ring((0, 0), (20, 0), (20, 10), (0, 10))]]
polys_b32 = [[ring((5, 10), (25, 10), (25, 20), (5, 20))]]
old_pt, new_pt = pt(5, 10), pt(8, 13)
edges = gu.find_t_edges(polys_a32, old_pt, 1e-12)
inserted = sum(gu.insert_vertex_topo(polys_a32, a, b, old_pt, 1e-12)
               for a, b in edges)
check("32 залатывание: вершина вставлена в ребро A",
      inserted >= 1 and gu.contains_vertex(polys_a32, old_pt, 1e-12))
new_a, ch_a = gu.replace_vertices(polys_a32, old_pt, new_pt, 1e-12)
new_b, ch_b = gu.replace_vertices(polys_b32, old_pt, new_pt, 1e-12)
# ch_b == 2: замыкающий дубликат кольца заменяется согласованно
check("32 залатывание: обе фичи обновлены (A и B)",
      ch_a == 1 and ch_b == 2)
check("32 залатывание: в A вершина в новом положении узла",
      gu.contains_vertex(new_a, new_pt, 1e-12))
check("32 залатывание: в B вершина в новом положении узла",
      gu.contains_vertex(new_b, new_pt, 1e-12))
check("32 залатывание: концы ребра A на месте (форма сохранна)",
      gu.contains_vertex(new_a, pt(0, 10), 1e-12) and
      gu.contains_vertex(new_a, pt(20, 10), 1e-12))

# приёмка этапа 2, тест 2: ребро с T-концами — якорь на ребре соседа
# вставляется и уезжает вместе с цепочкой
polys_a33 = [[ring((0, 0), (30, 0), (30, 20), (0, 20))]]
polys_b33 = [[ring((10, 20), (20, 20), (20, 30), (10, 30))]]
anchors33 = [pt(10, 20), pt(20, 20)]
delta33 = (0.0, 2.0)
for anc in anchors33:
    for a, b in gu.find_t_edges(polys_a33, anc, 1e-12):
        gu.insert_vertex_topo(polys_a33, a, b, anc, 1e-12)
new_a33, _ch_a33 = gu.translate_edge_vertices(polys_a33, anchors33,
                                              delta33, 1e-12)
new_b33, _ch_b33 = gu.translate_edge_vertices(polys_b33, anchors33,
                                              delta33, 1e-12)
check("32 T-концы: в A вставлены обе вершины и уехали с цепочкой",
      gu.contains_vertex(new_a33, pt(10, 22), 1e-12) and
      gu.contains_vertex(new_a33, pt(20, 22), 1e-12))
check("32 T-концы: нижнее ребро B уехало вверх",
      gu.contains_vertex(new_b33, pt(10, 22), 1e-12) and
      gu.contains_vertex(new_b33, pt(20, 22), 1e-12))
check("32 T-концы: верх B не тронут",
      gu.contains_vertex(new_b33, pt(10, 30), 1e-12))

# пассажир: чужая T-вершина НА цепочке (между якорями) следует за ней
# (кольцо с вершиной (15,20) на ребре y=20, соседи вне цепочки)
polys_c33 = [[ring((15, 15), (15, 20), (18, 23), (12, 18))]]
riders33 = gu.chain_riders(polys_c33, anchors33, 1e-12)
check("32 пассажир: T-вершина найдена на цепочке",
      len(riders33) == 1 and abs(riders33[0].x() - 15.0) < 1e-9 and
      abs(riders33[0].y() - 20.0) < 1e-9)
new_c33, ch_c33 = gu.replace_vertices(
    polys_c33, riders33[0],
    pt(riders33[0].x() + delta33[0], riders33[0].y() + delta33[1]), 1e-12)
check("32 пассажир: вершина переехала на новое ребро",
      ch_c33 == 1 and gu.contains_vertex(new_c33, pt(15, 22), 1e-12))

# ---------------------------------------------------------------------------
# 33. Индекс топологии (этап 3 ТЗ): плитки, узлы, рёбра, соседи,
#     хирургическая инвалидация, откат, бюджет, целостность
# ---------------------------------------------------------------------------
import topopolyedit.topology_index as ti  # noqa: E402


class _Sig(object):
    """Мини-сигнал Qt: connect/disconnect/emit (имена как у PyQt)."""

    def __init__(self):
        self._slots = []

    def connect(self, slot):
        self._slots.append(slot)

    def disconnect(self, slot):
        try:
            self._slots.remove(slot)
        except ValueError:
            pass

    def emit(self, *args):
        for s in list(self._slots):
            s(*args)


class _IndexFeat(object):
    def __init__(self, fid, geom):
        self._fid = fid
        self._g = geom

    def id(self):
        return self._fid

    def geometry(self):
        return self._g


class _IndexLyr(object):
    """Полигональный слой с сигналами правки и bbox-запросом.

    change_geom/add_geom/del_geom эмулируют правки через edit buffer
    (QGIS издаёт те же сигналы); set_geom_quiet — правка БЕЗ сигнала
    (так выглядит состояние до отката)."""

    def __init__(self, geoms, lid="idx_lyr"):
        self._feats = {}
        next_fid = 1
        for g in geoms:
            self._feats[next_fid] = _IndexFeat(next_fid, g)
            next_fid += 1
        self._next_fid = next_fid
        self._lid = lid
        self.geometryChanged = _Sig()
        self.featureAdded = _Sig()
        self.featureDeleted = _Sig()
        self.committedGeometriesChanges = _Sig()
        self.editingStopped = _Sig()
        self.afterRollBack = _Sig()

    def id(self):
        return self._lid

    def name(self):
        return self._lid

    def isValid(self):
        return True

    def geometryType(self):
        return QgsWkbTypes.PolygonGeometry

    def crs(self):
        return _Crs("EPSG:3857")

    def extent(self):
        bb = None
        for f in self._feats.values():
            r = f.geometry().boundingBox()
            if bb is None:
                bb = r
            else:
                bb.combineExtentWith(r)
        return bb

    def getFeatures(self, req):
        if getattr(req, "fids", None) is not None:
            return [self._feats[f] for f in req.fids if f in self._feats]
        feats = list(self._feats.values())
        if req.rect is not None:
            return [f for f in feats
                    if f.geometry().boundingBox().intersects(req.rect)]
        return feats

    def getFeature(self, fid):
        f = self._feats.get(fid)
        if f is None:
            raise KeyError("фича #{} отсутствует".format(fid))
        return f

    # --- edit-команды и запись (как у реального QgsVectorLayer;
    #     changeGeometry издаёт geometryChanged — по нему живёт индекс) ---

    def beginEditCommand(self, text):
        pass

    def endEditCommand(self):
        pass

    def destroyEditCommand(self):
        pass

    def triggerRepaint(self):
        pass

    def changeGeometry(self, fid, geom):
        self._feats[fid]._g = geom
        self.geometryChanged.emit(fid, geom)
        return True

    # --- симуляция правок ------------------------------------------------

    def change_geom(self, fid, geom):
        self._feats[fid]._g = geom
        self.geometryChanged.emit(fid, geom)

    def add_geom(self, fid, geom):
        self._feats[fid] = _IndexFeat(fid, geom)
        self.featureAdded.emit(fid)

    def del_geom(self, fid):
        del self._feats[fid]
        self.featureDeleted.emit(fid)

    def set_geom_quiet(self, fid, geom):
        self._feats[fid]._g = geom  # без сигналов (состояние до отката)


def _square(x0, y0, x1, y1):
    return QgsGeometry.fromPolygonXY([ring((x0, y0), (x1, y0),
                                           (x1, y1), (x0, y1))])


def _grid_geoms():
    """Сетка 2x2 (ячейка 10): узел (10,10) общий у 4 фич."""
    return [_square(0, 0, 10, 10), _square(10, 0, 20, 10),
            _square(0, 10, 10, 20), _square(10, 10, 20, 20)]


lyr33 = _IndexLyr(_grid_geoms(), lid="grid33")
idx33 = ti.TopologyIndex(lyr33)

# --- узлы: сетка и NodeRef -------------------------------------------------
refs33 = idx33.nodes_at(pt(10, 10))
check("33 узлы: узел (10,10) найден во всех 4 фичах",
      len(refs33) == 4 and {r.fid for r in refs33} == {1, 2, 3, 4})
check("33 узлы: NodeRef несёт позицию и структуру",
      all(abs(r.point.x() - 10.0) < 1e-9 and
          abs(r.point.y() - 10.0) < 1e-9 for r in refs33) and
      all(r.layer_id == "grid33" and r.part == 0 and r.ring == 0
          for r in refs33))
check("33 узлы: вне узла — пусто",
      idx33.nodes_at(pt(5, 5)) == [] and
      len(idx33.nodes_at(pt(10, 10), eps=0.5)) == 4)

# --- рёбра ------------------------------------------------------------------
edges33 = idx33.edges_near(pt(5, 0))
check("33 рёбра: ребро (0,0)-(10,0) найдено",
      len(edges33) == 1 and edges33[0].fid == 1 and
      abs(edges33[0].a.x() - 0.0) < 1e-9 and
      abs(edges33[0].b.x() - 10.0) < 1e-9)
edges33v = idx33.edges_near(pt(10, 5))
check("33 рёбра: вертикальное ребро x=10 есть у обеих смежных фич",
      {e.fid for e in edges33v} == {1, 2})
check("33 рёбра: точка вдали от рёбер — пусто",
      idx33.edges_near(pt(5, 5)) == [])

# --- features_touching / bbox_features / снимки / соседи ---------------------
touch33 = idx33.features_touching(pt(10, 10))
check("33 touching: узел задевает 4 фичи, ключ — layer_id",
      touch33 == {"grid33": [1, 2, 3, 4]})
bbox33 = idx33.bbox_features(QgsRectangle(11, 11, 19, 19))
check("33 bbox: внутренний прямоугольник — только фича 4",
      bbox33 == {"grid33": [4]})
snap33 = idx33.snapshot_geometry(lyr33, 4)
check("33 снимок: norm_polys фичи 4 (5 точек, одиночный)",
      snap33 is not None and snap33[1] is False and
      len(snap33[0][0][0]) == 5)
nbrs33 = idx33.neighbors(4)
check("33 соседи: у фичи 4 три соседа по узлу (10,10)",
      nbrs33 == {"grid33": [1, 2, 3]})

# --- хирургическая инвалидация: geometryChanged ------------------------------
_orig33 = _grid_geoms()


def _move_node(geom, old, new):
    """Новая геометрия квадрата с вершиной old -> new (замена копий)."""
    polys, was_multi = gu.norm_polys(geom)
    new_polys, _cnt = gu.replace_vertices(
        polys, pt(old[0], old[1]), pt(new[0], new[1]), 1e-12)
    return gu.polys_to_geom(new_polys, was_multi)


for fid33 in (1, 2, 3, 4):
    lyr33.change_geom(fid33, _move_node(_orig33[fid33 - 1],
                                        (10, 10), (12, 9)))
check("33 surgery: старого узла (10,10) больше нет",
      idx33.nodes_at(pt(10, 10)) == [])
check("33 surgery: новый узел (12,9) найден в 4 фичах",
      len(idx33.nodes_at(pt(12, 9))) == 4)
check("33 surgery: индекс равен полной пересборке",
      idx33.check_integrity() == [])

# --- featureAdded / featureDeleted -------------------------------------------
lyr33.add_geom(5, _square(30, 30, 40, 40))
check("33 добавление: новая фича видна узлам и bbox-поиску",
      len(idx33.nodes_at(pt(30, 30))) == 1 and
      idx33.bbox_features(QgsRectangle(31, 31, 39, 39)) ==
      {"grid33": [5]})
check("33 добавление: целостность сохранена",
      idx33.check_integrity() == [])
lyr33.del_geom(5)
check("33 удаление: фича исчезла из индекса",
      idx33.nodes_at(pt(30, 30)) == [] and
      idx33.bbox_features(QgsRectangle(31, 31, 39, 39)) is not None and
      idx33.bbox_features(QgsRectangle(31, 31, 39, 39))["grid33"] == [] and
      idx33.check_integrity() == [])

# --- afterRollBack: плитки bbox последней операции ----------------------------
# габарит операции — НОВЫЕ и СТАРЫЕ положения затронутых фич (как
# пишет редактор): узел (10,10)->(12,9) трогает фичи 1-4, их габариты
# (0,0)-(20,20); плитки, пересекающие этот bbox, инвалидируются целиком
ut.note_operation_bbox(lyr33, QgsRectangle(0, 0, 20, 20))
for fid33 in (1, 2, 3, 4):
    lyr33.set_geom_quiet(fid33, _orig33[fid33 - 1])  # откат БЕЗ сигналов
lyr33.afterRollBack.emit()
check("33 откат: узел (10,10) вернулся (плитки bbox пересобраны)",
      len(idx33.nodes_at(pt(10, 10))) == 4 and
      idx33.nodes_at(pt(12, 9)) == [])
check("33 откат: индекс равен полной пересборке",
      idx33.check_integrity() == [])

# откат без сведений в журнале — полная пересборка
for fid33 in (1, 2, 3, 4):
    lyr33.change_geom(fid33, _move_node(_orig33[fid33 - 1],
                                        (10, 10), (12, 9)))
ut.note_operation_bbox(lyr33, None)  # журнал очищен
for fid33 in (1, 2, 3, 4):
    lyr33.set_geom_quiet(fid33, _orig33[fid33 - 1])
lyr33.afterRollBack.emit()
check("33 откат без сведений: полная пересборка вернула узел",
      len(idx33.nodes_at(pt(10, 10))) == 4 and
      idx33.check_integrity() == [])

# --- editingStopped: полный сброс ---------------------------------------------
lyr33.change_geom(1, _move_node(_orig33[0], (10, 10), (12, 9)))
lyr33.editingStopped.emit()
check("33 стоп правки: плитки сброшены, данные пересобираются лениво",
      idx33._tiles == {} and
      len(idx33.nodes_at(pt(10, 10))) == 3 and  # фича 1 увела узел
      len(idx33.nodes_at(pt(12, 9))) == 1 and
      idx33.check_integrity() == [])

# --- бюджет сборки: плитка не строится, редактор идёт прямым запросом ---------
_budget_s, _budget_every = ti.TILE_BUILD_BUDGET_S, ti._BUDGET_CHECK_EVERY
try:
    ti.TILE_BUILD_BUDGET_S, ti._BUDGET_CHECK_EVERY = 0.0, 1
    lyr33b = _IndexLyr(_grid_geoms(), lid="grid33b")
    idx33b = ti.TopologyIndex(lyr33b)
    served33b = idx33b.ensure_for(QgsRectangle(0, 0, 20, 20))
    check("33 бюджет: плитка помечена несобранной",
          len(idx33b._failed) >= 1 and idx33b._tiles == {})
    check("33 бюджет: агрегированные запросы честно возвращают None",
          idx33b.bbox_features(QgsRectangle(0, 0, 20, 20)) is None and
          idx33b.features_near_points([pt(10, 10)]) is None)
    ed33b = te.TopoEditor(None, lyr33b)
    rep33b = ed33b.move_vertex(pt(10, 10), pt(11, 11))
    check("33 бюджет: редактор работает прямым запросом (узел перемещён)",
          rep33b.get("error") is None and rep33b.get("moved") == 4)
finally:
    ti.TILE_BUILD_BUDGET_S, ti._BUDGET_CHECK_EVERY = _budget_s, _budget_every

# --- целостность ловит порчу ---------------------------------------------------
tk33 = sorted(idx33._tiles.keys())[0]
idx33._tiles[tk33].snapshots.pop(1, None)
check("33 целостность: пропавший снимок обнаружен",
      len(idx33.check_integrity()) == 1)
idx33._dirty.add(tk33)
idx33.nodes_at(pt(10, 10))  # обращение пересобирает грязную плитку
check("33 целостность: после пересборки расхождений нет",
      idx33.check_integrity() == [])

# --- фича на стыке плиток -------------------------------------------------------
import topopolyedit.settings as st33  # noqa: E402
lyr33c = _IndexLyr(_grid_geoms(), lid="grid33c")
idx33c = ti.TopologyIndex(lyr33c)
_tf33 = st33.tile_factor
try:
    st33.tile_factor = lambda: 0.5
    idx33c.seed_extent(QgsRectangle(0, 0, 20, 20))  # сетка плиток 10x10
    check("33 стык плиток: узел на углу 4 плиток найден во всех фичах",
          len(idx33c.nodes_at(pt(10, 10))) == 4 and
          idx33c.check_integrity() == [] and
          len(idx33c._tiles) == 4)
finally:
    st33.tile_factor = _tf33

# --- настройка «индекс включён» и реестр ----------------------------------------
_on33 = st33.index_enabled
lyr33e = _IndexLyr(_grid_geoms(), lid="grid33e")  # свежий слой для реестра
try:
    st33.index_enabled = lambda: False
    check("33 настройка: индекс выключен — index_for_layer даёт None",
          ti.index_for_layer(lyr33e) is None)
finally:
    st33.index_enabled = _on33
idxA33 = ti.index_for_layer(lyr33e)
idxB33 = ti.index_for_layer(lyr33e)
check("33 реестр: индекс создаётся один раз и переиспользуется",
      idxA33 is not None and idxA33 is idxB33)
check("33 реестр: drop_index снимает подписки и удаляет из реестра",
      ti.drop_index(lyr33e) and lyr33e.geometryChanged._slots == [] and
      "grid33e" not in ti._registry)

# --- лимит соседей из настроек (ТЗ этап 3, п. 7: 5000 по умолчанию) -------------
check("33 лимит: константа редактора = 5000 (было 500)",
      te.MAX_OVERLAP_NEIGHBORS == 5000 and
      st33.DEFAULT_MAX_NEIGHBORS == 5000)
# наполнители ВНУТРИ bbox запроса (в углу фичи 4 — перехлёст легитимно
# остаётся: режим по умолчанию «спрашивать» без диалога = оставить)
_fill33 = [_square(19.0 + i * 0.2, 19.0, 19.15 + i * 0.2, 19.15)
           for i in range(6)]
lyr33d = _IndexLyr(_grid_geoms() + _fill33, lid="grid33d")
_lim33 = st33.max_overlap_neighbors
try:
    st33.max_overlap_neighbors = lambda: 3
    ed33d = te.TopoEditor(None, lyr33d)
    rep33d = ed33d.move_vertex(pt(10, 10), pt(11, 11))
    check("33 лимит: превышение даёт явное сообщение с числом",
          rep33d.get("error") is not None and
          "больше 3" in rep33d["error"] and
          rep33d.get("moved", 0) == 0)
    check("33 лимит: слой битово цел при отказе",
          gu.norm_polys(lyr33d.getFeature(1).geometry())[0] ==
          gu.norm_polys(_orig33[0])[0])
finally:
    st33.max_overlap_neighbors = _lim33

# очистка журнала/реестра секции
ti.shutdown_all()
ut.note_operation_bbox(lyr33, None)

# ---------------------------------------------------------------------------
# 34. Этап 4 ТЗ: правка нескольких слоёв одной операцией
#     (OperationLog, MultiLayerCommitter, участники, per-layer отсечение)
# ---------------------------------------------------------------------------
import topopolyedit.operation_log as oplog  # noqa: E402
import topopolyedit.multi_editor as me  # noqa: E402


class _OpStack(object):
    """Стек отмены с index/text (как QgsUndoStack для журнала операций)."""

    def __init__(self, n):
        self._n = n
        self.undone = 0

    def index(self):
        return self._n - self.undone

    def canUndo(self):
        return self._n - self.undone > 0

    def text(self, i):
        return "cmd%d" % (i + 1)

    def undo(self):
        if self.canUndo():
            self.undone += 1
            return True
        return False


class _OpLyr(object):
    def __init__(self, lid, name, n_cmds=1):
        self._lid = lid
        self._name = name
        self.stack = _OpStack(n_cmds)
        self.undone_calls = []

    def id(self):
        return self._lid

    def name(self):
        return self._name

    def undoStack(self):
        return self.stack

    def triggerRepaint(self):
        pass


def _resolver(*layers):
    mapping = {l.id(): l for l in layers}

    def resolve(lid):
        if lid not in mapping:
            raise RuntimeError(u"слой не найден")
        return mapping[lid]
    return resolve


oplog.clear()
lA, lB, lC = _OpLyr("A", "Слой А", 2), _OpLyr("B", "Слой Б", 1), _OpLyr("C", "Слой В", 1)
# запись: [(layer_id, text, index_after)]
seq34 = oplog.note_operation([("A", "cmd2", 2), ("B", "cmd1", 1)])
check("34 журнал: операция зарегистрирована с seq",
      seq34 is not None and oplog.last_operation() is not None and
      len(oplog.last_operation()["layers"]) == 2)

ok34, layers34, why34 = oplog.can_undo_operation(
    oplog.last_operation(), _resolver(lA, lB))
check("34 журнал: согласованная операция отменяется целиком",
      ok34 and [l.id() for l in layers34] == ["A", "B"] and why34 == u"")

# ручная правка после операции: index вырос -> отказ с объяснением
lB.stack._n += 1  # пользователь добавил правку
ok34, _l, why34 = oplog.can_undo_operation(oplog.last_operation(),
                                           _resolver(lA, lB))
check("34 журнал: правка слоя после операции — отказ «вручную»",
      not ok34 and u"вручную" in why34)
lB.stack._n -= 1

# ручной undo после операции: index упал -> отказ
lB.stack.undone += 1
ok34, _l, why34 = oplog.can_undo_operation(oplog.last_operation(),
                                           _resolver(lA, lB))
check("34 журнал: частичный undo после операции — отказ «уже отменена»",
      not ok34 and u"уже отменена" in why34)
lB.stack.undone -= 1

# верхняя команда другая (текст) — отказ
lA.stack.text = lambda i: "чужая команда"
ok34, _l, why34 = oplog.can_undo_operation(oplog.last_operation(),
                                           _resolver(lA, lB))
check("34 журнал: чужая команда сверху — отказ «вручную»",
      not ok34 and u"вручную" in why34)
del lA.stack.text  # вернуть исходный метод класса

# слой удалён из проекта — отказ
ok34, _l, why34 = oplog.can_undo_operation(oplog.last_operation(),
                                           _resolver(lA))
check("34 журнал: слой исчез из проекта — отказ без частичной отмены",
      not ok34 and u"не в проекте" in why34)

# успешный откат: обратный порядок записи (B, A)
ok34, undone34 = oplog.undo_operation(oplog.last_operation(),
                                      _resolver(lA, lB))
check("34 журнал: undo_operation прошёл и вернул тексты команд",
      ok34 and undone34 == ["cmd1", "cmd2"])
check("34 журнал: undo() вызван в ОБРАТНОМ порядке записи (B первым)",
      lB.stack.undone == 1 and lA.stack.undone == 1 and
      lB.stack.undone >= lA.stack.undone)
oplog.drop_last_operation()  # (кнопка «Отменить» снимает запись после успеха)
check("34 журнал: запись снята после успешного отката",
      oplog.last_operation() is None)

# транзакционная группа: один undo() на первом слое (этап 4, п. 5)
oplog.clear()
lD, lE = _OpLyr("D", "Группа-1", 1), _OpLyr("E", "Группа-2", 1)
oplog.note_operation([("D", "cmd1", 1), ("E", "cmd1", 1)],
                     transaction_group=True)
ok34, undone34 = oplog.undo_operation(oplog.last_operation(),
                                      _resolver(lD, lE))
check("34 ТГ: операция в группе откатывается ОДНИМ undo",
      ok34 and undone34 == ["cmd1"] and
      lD.stack.undone == 1 and lE.stack.undone == 0)

# --- MultiLayerCommitter: атомарность записи (этап 4, п. 3) ---------------

class _RealStack(object):
    """Стек отмены с РЕАЛЬНЫМИ текстами команд и откатом-колбэком.

    begin(text)/end() фиксируют команду (как в QGIS), undo() откатывает
    через колбэк (восстановление геометрий слоя)."""

    def __init__(self, on_undo=None):
        self._cmds = []
        self.undone = 0
        self._open = None
        self._on_undo = on_undo

    def begin(self, text):
        self._open = text

    def end(self):
        if self._open is not None:
            self._cmds.append(self._open)
            self._open = None

    def destroy(self):
        self._open = None

    def index(self):
        return len(self._cmds) - self.undone

    def canUndo(self):
        return self.index() > 0

    def text(self, i):
        return self._cmds[i]

    def undo(self):
        if self.canUndo():
            self.undone += 1
            if self._on_undo is not None:
                self._on_undo()
            return True
        return False


class _CommitLyr(_UndoEditLyr, QgsVectorLayer):
    """Слой правки с буфером команд: changeGeometry копит изменения,
    endEditCommand применяет (с geometryChanged), destroyEditCommand
    отбрасывает — битовая целостность как у реального QgsVectorLayer.
    undo стека восстанавливает исходные геометрии."""

    def __init__(self, lid, feats, fail_fid=None):
        _UndoEditLyr.__init__(self, feats)
        self._lid = lid
        self._fail_fid = fail_fid
        self.destroyed = 0
        self.begins = 0
        self.ends = 0
        self._orig = {f.id(): f._g for f in self._feats}
        self._pending = {}
        self.geometryChanged = _Sig()  # endEditCommand издаёт изменение
        self.stack = _RealStack(on_undo=self._undo_geoms)

    def _undo_geoms(self):
        for f in self._feats:
            g = self._orig.get(f.id())
            if g is not None:
                f._g = g

    def id(self):
        return self._lid

    def name(self):
        return self._lid

    def type(self):
        return QgsMapLayerType.VectorLayer

    def beginEditCommand(self, text):
        self.stack.begin(text)
        self.begins += 1

    def endEditCommand(self):
        self.stack.end()
        self.ends += 1
        by_id = {f.id(): f for f in self._feats}
        for fid, g in self._pending.items():
            f = by_id.get(fid)
            if f is not None:  # план может трогать чужие fid (стаб)
                f._g = g
                self.geometryChanged.emit(fid, g)
        self._pending = {}

    def destroyEditCommand(self):
        self.stack.destroy()
        self.destroyed += 1
        self._pending = {}

    def changeGeometry(self, fid, g):
        if self._fail_fid is not None and fid == self._fail_fid:
            return False
        self._pending[fid] = g
        return True


def _feat34(layer, fid):
    for f in layer._feats:
        if f.id() == fid:
            return f
    raise KeyError(fid)


feat_a34 = _UndoFeat(1, QgsGeometry.fromPolygonXY(
    [ring((0, 0), (2, 0), (2, 2), (2, 4), (0, 4))]))
feat_b34 = _UndoFeat(2, QgsGeometry.fromPolygonXY(
    [ring((2, 0), (5, 0), (5, 4), (2, 4), (2, 2))]))

plan34 = {"final": {1: "g1", 2: "g2"}, "write_order": [1, 2],
          "final_clipped": {1: 0, 2: 0}, "t_fids": set(),
          "old_geoms": {}, "eps": 1e-6, "bbox": None}

cA = _CommitLyr("M-A", [feat_a34])
cB = _CommitLyr("M-B", [feat_b34])
committer34 = me.MultiLayerCommitter(u"Тестовый коммит")
entries34 = committer34.commit_plans([(cA, plan34), (cB, plan34)])
check("34 коммиттер: begin/end на ОБОИХ слоях, записи в оба",
      cA.begins == 1 and cA.ends == 1 and cB.begins == 1 and cB.ends == 1
      and cA.destroyed == 0 and cB.destroyed == 0 and
      _feat34(cA, 1)._g == "g1" and _feat34(cB, 2)._g == "g2")
check("34 коммиттер: возвращает индексы стеков после коммита",
      [e[0] for e in entries34] == ["M-A", "M-B"] and
      all(e[2] == 1 for e in entries34))

# сбой записи во ВТОРОМ слое: ВСЕ команды уничтожены (этап 4, п. 3)
cA2 = _CommitLyr("M-A", [feat_a34])
cB2 = _CommitLyr("M-B", [feat_b34], fail_fid=1)
committer34 = me.MultiLayerCommitter(u"Тестовый коммит")
failed34 = False
try:
    committer34.commit_plans([(cA2, plan34), (cB2, plan34)])
except RuntimeError:
    failed34 = True
check("34 коммиттер: сбой записи второго слоя даёт RuntimeError",
      failed34)
check("34 коммиттер: при сбое destroyEditCommand на ВСЕХ слоях (частичной записи нет)",
      cA2.destroyed == 1 and cB2.destroyed == 1 and
      cA2.ends == 0 and cB2.ends == 0)

# --- транзакционная группа в коммиттере: ОДНА команда на группу -----------
_key34 = oplog.layer_group_key
try:
    oplog.layer_group_key = lambda lyr: u"gpkg-conn-1"
    cA3 = _CommitLyr("M-A", [feat_a34])
    cB3 = _CommitLyr("M-B", [feat_b34])
    committer34 = me.MultiLayerCommitter(u"ТГ-коммит")
    committer34.commit_plans([(cA3, plan34), (cB3, plan34)])
    check("34 ТГ-коммит: begin/end ОДИН раз (на первом слое), записи в оба",
          committer34.transaction_group and cA3.begins == 1 and
          cB3.begins == 0 and cA3.ends == 1 and cB3.ends == 0 and
          _feat34(cA3, 1)._g == "g1" and _feat34(cB3, 2)._g == "g2")
finally:
    oplog.layer_group_key = _key34

# --- участники операции (этап 4, п. 1(а)(б)) ------------------------------


class _MapProject(object):
    """Фейковый проект с mapLayers()/mapLayer() и деревом для прилипания."""

    def __init__(self, layers, root=None):
        self._layers = {l.id(): l for l in layers}
        self._root = root if root is not None else _TreeRoot(children=[])

    def mapLayers(self):
        return dict(self._layers)

    def mapLayer(self, lid):
        return self._layers.get(lid)

    def layerTreeRoot(self):
        return self._root


ed_l34 = _UndoLyr("ed34", True, True)     # в правке — участник
off_l34 = _UndoLyr("off34", False, True)  # не в правке — не участник
line_l34 = _UndoLyr("line34", True, True,
                    QgsWkbTypes.LineGeometry)  # не полигон — не участник
QgsProject._inst = _MapProject([ed_l34, off_l34, line_l34])
_prim34 = _UndoLyr("prim34", True, True)

got34 = me.candidate_edit_layers(_prim34)
check("34 участники: режим «все слои в правке» — полигональные в правке",
      [l.id() for l in got34] == ["id-prim34", "id-ed34"])

_st34 = st.edit_scope
try:
    st.edit_scope = lambda: st.EDIT_SCOPE_ACTIVE
    got34 = me.candidate_edit_layers(_prim34)
    check("34 участники: режим «только активный слой» — один первичный",
          [l.id() for l in got34] == ["id-prim34"])
finally:
    st.edit_scope = _st34

_dis34 = st.edit_disabled_ids
try:
    st.edit_disabled_ids = lambda: {"id-ed34"}
    got34 = me.candidate_edit_layers(_prim34)
    check("34 участники: чёрный список правки исключает слой (п. 1(б))",
          [l.id() for l in got34] == ["id-prim34"])
finally:
    st.edit_disabled_ids = _dis34

# --- per-layer отсечение (этап 4, п. 7) -----------------------------------
se.invalidate_snap_layers_cache()  # кэш дерева слоёв секций 29-33 пуст
ref34 = _CommitLyr("REF34", [_UndoFeat(9, QgsGeometry.fromPolygonXY(
    [ring((0, 0), (2, 0), (2, 4), (0, 4))]))])
host34 = _CommitLyr("HOST34", [feat_a34])
QgsProject._inst = _MapProject([ref34, host34], root=_TreeRoot(children=[
    _TreeNode(layer=ref34)]))
_clipmode34 = st.clip_mode_for
try:
    st.clip_mode_for = lambda lid: st.CLIP_CLIP
    refs34 = te.TopoEditor(None, host34)._clip_references(
        QgsRectangle(0, 0, 2, 4))
    check("34 отсечение: режим «отсекать» (по умолчанию) — эталон взят",
          len(refs34[0]) == 1 and refs34[1] is False and refs34[2] == [])

    st.clip_mode_for = lambda lid: st.CLIP_IGNORE
    refs34 = te.TopoEditor(None, host34)._clip_references(
        QgsRectangle(0, 0, 2, 4))
    check("34 отсечение: «не трогать» — слой вне эталонов",
          len(refs34[0]) == 0 and len(refs34[2]) == 0)

    st.clip_mode_for = lambda lid: st.CLIP_ASK
    refs34 = te.TopoEditor(None, host34)._clip_references(
        QgsRectangle(0, 0, 2, 4))
    check("34 отсечение: «спрашивать» — геометрии отдельно с именем слоя",
          len(refs34[0]) == 0 and len(refs34[2]) == 1 and
          refs34[2][0][1] == "REF34")
finally:
    st.clip_mode_for = _clipmode34

# участник операции не является эталоном (двигается вместе)
st.clip_mode_for = lambda lid: st.CLIP_CLIP
QgsProject._inst = _MapProject([ref34, host34, cA], root=_TreeRoot(children=[
    _TreeNode(layer=ref34)]))
refs34 = te.TopoEditor(None, host34)._clip_references(
    QgsRectangle(0, 0, 2, 4), moving_layer_ids={"REF34"})
check("34 отсечение: слой-участник операции исключён из эталонов",
      len(refs34[0]) == 0)
st.clip_mode_for = _clipmode34

# --- многослойное перемещение узла end-to-end ------------------------------
feat_a34 = _UndoFeat(1, QgsGeometry.fromPolygonXY(
    [ring((0, 0), (2, 0), (2, 2), (2, 4), (0, 4))]))
feat_b34 = _UndoFeat(2, QgsGeometry.fromPolygonXY(
    [ring((2, 0), (5, 0), (5, 4), (2, 4), (2, 2))]))
QgsProject._inst = _MapProject([])
ut.note_edited_layer(None)
oplog.clear()
mA = _CommitLyr("ML-A", [feat_a34, feat_b34])
# слой B: полигон с вершиной (2,2) и ребром (2,2)-(2,4) — общий узел с A
mB = _CommitLyr("ML-B", [_UndoFeat(1, QgsGeometry.fromPolygonXY(
    [ring((2, 2), (2, 4), (0, 4), (0, 2))]))])
QgsProject._inst = _MapProject([mA, mB])
_ed = me.TopoEditor(None, mB)
_en = _ed._collect_moved(pt(2,2), pt(3,3), 1e-6)
mel = me.MultiLayerEditor(None, mA)
rep34 = mel.move_vertex(pt(2, 2), pt(3, 3))
check("34 multi: перемещение узла меняет ОБА слоя (moved=3)",
      rep34.get("error") is None and rep34.get("moved") == 3 and
      len(rep34.get("participants") or []) == 2)
check("34 multi: команды завершены на обоих слоях",
      mA.ends == 1 and mB.ends == 1 and mA.destroyed == 0 and
      mB.destroyed == 0)
check("34 multi: геометрия слоя B уехала вместе с узлом",
      gu.norm_polys(mB.getFeatures(None)[0].geometry())[0][0][0][0] ==
      pt(3, 3))

op34 = oplog.last_operation()
check("34 multi: операция в журнале с ДВУМЯ слоями",
      op34 is not None and len(op34["layers"]) == 2 and
      not op34["transaction_group"])
_res = _MapProject([mA, mB]).mapLayer
ok34, undone34 = oplog.undo_operation(op34, _res)
check("34 multi: одна отмена откатывает ОБА слоя",
      ok34 and len(undone34) == 2 and
      gu.norm_polys(mB.getFeatures(None)[0].geometry())[0][0][0][0] ==
      pt(2, 2))
oplog.drop_last_operation()

# сбой записи во втором слое: ОБОИ слоя битово неизменены (приёмка ТЗ)
mC = _CommitLyr("ML-C", [feat_a34, feat_b34])
mD = _CommitLyr("ML-D", [_UndoFeat(1, QgsGeometry.fromPolygonXY(
    [ring((2, 2), (2, 4), (0, 4), (0, 2))]))], fail_fid=1)
QgsProject._inst = _MapProject([mC, mD])
mel = me.MultiLayerEditor(None, mC)
rep34 = mel.move_vertex(pt(2, 2), pt(3, 3))
check("34 multi-сбой: операция отклонена с объяснением, moved=0",
      rep34.get("error") is not None and rep34.get("moved") == 0)
check("34 multi-сбой: destroyEditCommand на ОБОИХ слоях (битово целы)",
      mC.destroyed == 1 and mD.destroyed == 1 and
      mC.ends == 0 and mD.ends == 0)
check("34 multi-сбой: геометрия первого слоя не изменилась",
      gu.norm_polys(mC.getFeatures(None)[0].geometry())[0][0][0][2] ==
      pt(2, 2))
check("34 multi-сбой: журнальной записи нет (отмена не обещана)",
      oplog.last_operation() is None)

# один слой в правке: многослойный путь не ломает одиночную операцию
QgsProject._inst = _MapProject([])
oplog.clear()
mE = _CommitLyr("ML-E", [feat_a34, feat_b34])
mel = me.MultiLayerEditor(None, mE)
rep34 = mel.move_vertex(pt(2, 2), pt(3, 3))
check("34 multi-одиночный: один слой — moved=2, команда завершена",
      rep34.get("error") is None and rep34.get("moved") == 2 and
      mE.ends == 1 and mE.destroyed == 0)

# ---------------------------------------------------------------------------
print("")
if FAILURES:
    print("ИТОГ: ПРОВАЛЕНО тестов: %d -> %s" % (len(FAILURES), FAILURES))
    sys.exit(1)
print("ИТОГ: все тесты пройдены")
