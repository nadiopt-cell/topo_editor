"""Инструмент карты, этап 1: построить и показать локальный граф рёбер.

Клик по участку: берём его, соседей и соседей соседей, строим граф и рисуем
рёбра (красные - общие, серые - внешние) и узлы (жёлтые точки).
Esc или смена инструмента убирает отрисовку.
"""
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QColor
from qgis.core import (
    QgsCoordinateTransform,
    QgsPointXY,
    QgsProject,
    QgsVectorLayer,
)
from qgis.gui import QgsMapTool, QgsRubberBand

from .core.graph import build_graph
from .qgis_utils import (
    LINE,
    POINT,
    POLYGON,
    collect_neighborhood,
    faces_from_features,
    pick_feature,
    points_to_geometry,
    polylines_to_geometry,
)

SEARCH_RADIUS_PX = 6


class GraphInspectTool(QgsMapTool):
    def __init__(self, iface, get_tolerance):
        super().__init__(iface.mapCanvas())
        self.iface = iface
        self.canvas = iface.mapCanvas()
        self.get_tolerance = get_tolerance
        self.rb_boundary = self._line_band(QColor(110, 110, 110, 220), 2)
        self.rb_shared = self._line_band(QColor(225, 40, 40, 230), 3)
        self.rb_nodes = QgsRubberBand(self.canvas, POINT)
        self.rb_nodes.setColor(QColor(255, 200, 0, 255))
        self.rb_nodes.setIcon(QgsRubberBand.ICON_CIRCLE)
        self.rb_nodes.setIconSize(9)
        self.setCursor(Qt.CrossCursor)

    def _line_band(self, color, width):
        rb = QgsRubberBand(self.canvas, LINE)
        rb.setColor(color)
        rb.setWidth(width)
        return rb

    def clear(self):
        # reset() без аргумента переводит rubber band в линейный тип,
        # поэтому тип передаём явно
        self.rb_boundary.reset(LINE)
        self.rb_shared.reset(LINE)
        self.rb_nodes.reset(POINT)

    def deactivate(self):
        self.clear()
        super().deactivate()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.clear()
            event.accept()

    # ------------------------------------------------------------------
    def _layer_point_and_radius(self, layer, event):
        """Клик и радиус поиска из СК карты в СК слоя."""
        map_pt = event.mapPoint()
        radius_map = self.canvas.mapUnitsPerPixel() * SEARCH_RADIUS_PX
        ct = QgsCoordinateTransform(
            self.canvas.mapSettings().destinationCrs(), layer.crs(), QgsProject.instance()
        )
        pt = ct.transform(map_pt)
        # приближённо: радиус считаем по смещению вдоль оси X
        pt2 = ct.transform(QgsPointXY(map_pt.x() + radius_map, map_pt.y()))
        return pt, abs(pt2.x() - pt.x())

    def canvasReleaseEvent(self, event):
        if event.button() != Qt.LeftButton:
            return
        layer = self.iface.activeLayer()
        if not isinstance(layer, QgsVectorLayer) or layer.geometryType() != POLYGON:
            self.iface.messageBar().pushWarning(
                "Topo Graph Editor", "Выберите активным полигональный слой."
            )
            return

        pt, radius = self._layer_point_and_radius(layer, event)
        seed = pick_feature(layer, pt, radius)
        self.clear()
        if seed is None:
            return

        tol = self.get_tolerance()
        found = collect_neighborhood(layer, seed, tol, depth=2)
        faces, levels = faces_from_features(found)
        graph = build_graph(faces, tol)
        self._draw(layer, graph, levels)

    def _draw(self, layer, graph, levels):
        boundary, shared, node_ids = [], [], set()
        for e in graph.edges.values():
            # рисуем только рёбра выбранного объекта и его прямых соседей;
            # второй уровень нужен лишь для корректного определения узлов
            if not any(levels[fid] <= 1 for fid, _ in e.faces):
                continue
            coords = graph.edge_coords(e.id)
            (shared if e.is_shared else boundary).append(coords)
            node_ids.update((e.start, e.end))

        if boundary:
            self.rb_boundary.setToGeometry(polylines_to_geometry(boundary), layer)
        if shared:
            self.rb_shared.setToGeometry(polylines_to_geometry(shared), layer)
        if node_ids:
            pts = [graph.coords[v] for v in node_ids]
            self.rb_nodes.setToGeometry(points_to_geometry(pts), layer)

        self.iface.statusBarIface().showMessage(
            "Граф: объектов {}, узлов {}, рёбер {} (общих {})".format(
                len(levels), len(node_ids), len(boundary) + len(shared), len(shared)
            ),
            8000,
        )
