"""Инструмент карты, этап 2: перенос вершины вместе с соседними участками.

Нажать на вершину, тянуть, отпустить. Во время перетаскивания показывается
предпросмотр новых контуров всех затронутых участков.

Shift+клик (или кнопка "Добавить вершину на ребро" в панели) - тот же жест,
но вместо переноса во все участки, проходящие через выбранное ребро/вершину,
добавляется НОВАЯ вершина: щель между участками не появляется, общая граница
остаётся общей.

Если правка создаёт самопересечение (или пересечение с чужим участком),
она блокируется: применять ничего не нужно, нарушение подсвечивается красным
(отрезки и точка) и остаётся на экране до следующего клика или Esc.

Применение - одна команда редактирования: Ctrl+Z откатывает сразу все участки.
"""
import time
from collections import defaultdict

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QColor
from qgis.core import (
    QgsCoordinateTransform,
    QgsGeometry,
    QgsPointXY,
    QgsProject,
    QgsVectorLayer,
    QgsWkbTypes,
)
from qgis.gui import QgsMapTool, QgsRubberBand

from .core.graph import build_graph
from .core.ops import find_snap_target, nearest_vertex, plan_vertex_move
from .qgis_utils import (
    LINE,
    POINT,
    POLYGON,
    collect_extra_faces,
    collect_neighborhood,
    faces_from_features,
    geometry_parts,
    pick_feature,
    points_to_geometry,
    polylines_to_geometry,
)

SEARCH_RADIUS_PX = 8
SNAP_RADIUS_PX = 12      # радиус привязки к вершине соседнего участка
PREVIEW_INTERVAL = 0.04  # секунд между пересчётами плана при перетаскивании
TITLE = "Topo Graph Editor"

KIND_TEXT = {
    "self_intersection": "самопересечение",
    "crosses_other_face": "пересечение с другим участком",
    "merge_same_face": "слияние вершин одного участка",
}


class VertexMoveTool(QgsMapTool):
    def __init__(self, iface, get_tolerance):
        super().__init__(iface.mapCanvas())
        self.iface = iface
        self.canvas = iface.mapCanvas()
        self.get_tolerance = get_tolerance

        self.rb_preview = self._band(LINE, QColor(40, 170, 70, 230), 2)
        self.rb_bad = self._band(LINE, QColor(225, 30, 30, 240), 4)
        self.rb_marks = self._band(POINT, QColor(225, 30, 30, 255), 0)
        self.rb_marks.setIcon(QgsRubberBand.ICON_X)
        self.rb_marks.setIconSize(14)
        self.rb_vertex = self._band(POINT, QColor(255, 200, 0, 255), 0)
        self.rb_vertex.setIcon(QgsRubberBand.ICON_CIRCLE)
        self.rb_vertex.setIconSize(10)
        self.rb_snap = self._band(POINT, QColor(0, 190, 220, 255), 0)
        self.rb_snap.setIcon(QgsRubberBand.ICON_CIRCLE)
        self.rb_snap.setIconSize(16)
        self.rb_insert = self._band(POINT, QColor(40, 170, 70, 255), 0)
        self.rb_insert.setIcon(QgsRubberBand.ICON_CROSS)
        self.rb_insert.setIconSize(14)
        self.setCursor(Qt.CrossCursor)

        self.insert_mode = False   # True - добавление вершины (Shift или кнопка)
        self._reset_state()

    def _band(self, geom_type, color, width):
        rb = QgsRubberBand(self.canvas, geom_type)
        rb.setColor(color)
        if width:
            rb.setWidth(width)
        return rb

    def _reset_state(self):
        self.layer = None
        self.graph = None
        self.vid = None
        self.start_xy = None
        self.tol = None
        self.ct = None
        self.known_fids = set()
        self.plan = None
        self.snap_radius = None
        self.snap_target = None
        self.insert = False   # режим текущей операции (ставится из canvasPressEvent)
        self.dragging = False
        self._last_plan_time = 0.0

    def set_insert_mode(self, enabled):
        """Включает/выключает режим добавления вершины без перетаскивания."""
        self.insert_mode = bool(enabled)

    def clear(self):
        self.rb_preview.reset(LINE)
        self.rb_bad.reset(LINE)
        self.rb_marks.reset(POINT)
        self.rb_vertex.reset(POINT)
        self.rb_snap.reset(POINT)
        self.rb_insert.reset(POINT)

    def deactivate(self):
        self.clear()
        self._reset_state()
        super().deactivate()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.clear()
            self._reset_state()
            event.accept()

    # ----------------------------------------------------------- события мыши
    def canvasPressEvent(self, event):
        if event.button() != Qt.LeftButton:
            return
        self.clear()
        self._reset_state()

        layer = self.iface.activeLayer()
        problem = self._check_layer(layer)
        if problem:
            self.iface.messageBar().pushWarning(TITLE, problem)
            return

        ct = QgsCoordinateTransform(
            self.canvas.mapSettings().destinationCrs(), layer.crs(), QgsProject.instance()
        )
        map_pt = event.mapPoint()
        pt = ct.transform(map_pt)
        pt2 = ct.transform(
            QgsPointXY(map_pt.x() + self.canvas.mapUnitsPerPixel() * SEARCH_RADIUS_PX, map_pt.y())
        )
        radius = abs(pt2.x() - pt.x())
        units_per_px = radius / SEARCH_RADIUS_PX
        tol = self.get_tolerance()
        snap_radius = max(units_per_px * SNAP_RADIUS_PX, tol)

        seed = pick_feature(layer, pt, radius)
        if seed is None:
            return
        # соседей ищем в радиусе привязки: партнёр по щели иначе не попал бы в граф
        found = collect_neighborhood(
            layer, seed, tol, depth=2, reach=max(tol * 2, snap_radius)
        )
        faces, _levels = faces_from_features(found)
        graph = build_graph(faces, tol)
        vid = nearest_vertex(graph, (pt.x(), pt.y()), radius)
        if vid is None:
            self.iface.statusBarIface().showMessage("Рядом нет вершины", 3000)
            return

        self.layer, self.graph, self.vid, self.tol, self.ct = layer, graph, vid, tol, ct
        self.snap_radius = snap_radius
        self.start_xy = graph.coords[vid]
        self.known_fids = set(found.keys())
        # Shift+клик или активная кнопка "добавить вершину" - вставка, иначе перенос
        self.insert = bool(self.insert_mode or (event.modifiers() & Qt.ShiftModifier))
        self.dragging = True
        self.rb_vertex.setToGeometry(points_to_geometry([self.start_xy]), layer)
        mode = ("Режим: добавление вершины на ребро (участки остаются общими)."
                if self.insert else "Режим: перенос вершины.")
        self.iface.statusBarIface().showMessage(mode, 5000)

    def canvasMoveEvent(self, event):
        if not self.dragging:
            return
        now = time.monotonic()
        if now - self._last_plan_time < PREVIEW_INTERVAL:
            return
        self._last_plan_time = now
        self._replan(event)

    def canvasReleaseEvent(self, event):
        if not self.dragging or event.button() != Qt.LeftButton:
            return
        self.dragging = False
        self._replan(event)  # финальный пересчёт без троттлинга
        plan = self.plan
        if plan is None or not (plan.point_moves or plan.point_inserts):
            self.clear()
            return
        if not self.insert and plan.new_xy == (self.start_xy[0], self.start_xy[1]):
            self.clear()  # при вставке точка на месте - тоже осмысленная операция
            return

        if plan.violations:
            self._show_block(plan)
            return

        self._apply(plan)
        self.clear()

    # --------------------------------------------------------------- план
    def _replan(self, event):
        pt = self.ct.transform(event.mapPoint())
        xy = (pt.x(), pt.y())
        known = self.known_fids
        layer = self.layer

        # привязка к вершине соседнего участка; Ctrl - перенос без привязки.
        # при вставке новой вершины слияние не выполняется
        snap = None
        if not self.insert and not (event.modifiers() & Qt.ControlModifier):
            snap = find_snap_target(self.graph, self.vid, xy, self.snap_radius)
        self.snap_target = snap

        plan = plan_vertex_move(
            self.graph,
            self.vid,
            xy,
            self.tol,
            lambda bbox: collect_extra_faces(layer, known, bbox),
            merge_into=snap,
            insert=self.insert,
        )
        self.plan = plan
        self._draw(plan)

    def _draw(self, plan):
        layer = self.layer
        self.rb_preview.reset(LINE)
        self.rb_bad.reset(LINE)
        self.rb_marks.reset(POINT)
        self.rb_snap.reset(POINT)
        self.rb_insert.reset(POINT)

        if plan.merge_into is not None:
            self.rb_snap.setToGeometry(
                points_to_geometry([self.graph.coords[plan.merge_into]]), layer
            )
        if plan.point_inserts:
            pts = []
            for fid, ri, idx in plan.point_inserts:
                ring = plan.new_rings.get((fid, ri))
                if ring is not None and idx < len(ring):
                    pts.append(ring[idx])
            if pts:
                self.rb_insert.setToGeometry(points_to_geometry(pts), layer)

        rings = [ring for ring in plan.new_rings.values()]
        if rings:
            self.rb_preview.setToGeometry(polylines_to_geometry(rings), layer)
        if plan.violations:
            segs = [list(s) for v in plan.violations for s in v.segments]
            if segs:
                self.rb_bad.setToGeometry(polylines_to_geometry(segs), layer)
            self.rb_marks.setToGeometry(
                points_to_geometry([v.point for v in plan.violations]), layer
            )
            kinds = sorted({KIND_TEXT.get(v.kind, v.kind) for v in plan.violations})
            self.iface.statusBarIface().showMessage(
                "Заблокировано: {} ({})".format(", ".join(kinds), len(plan.violations)), 5000
            )
        else:
            self.iface.statusBarIface().showMessage(self._area_text(plan), 5000)

    def _area_text(self, plan):
        parts = []
        for face, (old, new) in sorted(plan.areas.items(), key=lambda kv: str(kv[0])):
            if abs(new - old) > 1e-9:
                parts.append("{}: {:+.3f}".format(face[0], new - old))
        shown = ", ".join(parts[:4]) + (" …" if len(parts) > 4 else "")
        if plan.point_inserts:
            return "Добавление вершины. Участков затронуто: {}. Изменение площади (fid): {}".format(
                len({f[0] for f in plan.areas}), shown or "нет"
            )
        prefix = "Слияние с вершиной соседа. " if plan.merge_into is not None else ""
        return prefix + "Участков затронуто: {}. Изменение площади (fid): {}".format(
            len({f[0] for f in plan.areas}), shown or "нет"
        )

    def _show_block(self, plan):
        # подсветка нарушений остаётся; предпросмотр нового контура убираем
        self.rb_preview.reset(LINE)
        n = len(plan.violations)
        kinds = sorted({KIND_TEXT.get(v.kind, v.kind) for v in plan.violations})
        self.iface.messageBar().pushWarning(
            TITLE,
            "Правка заблокирована: {} ({}). Места нарушений отмечены красным; "
            "Esc убирает подсветку.".format(", ".join(kinds), n),
        )

    # ------------------------------------------------------------ применение
    def _check_layer(self, layer):
        if not isinstance(layer, QgsVectorLayer) or layer.geometryType() != POLYGON:
            return "Выберите активным полигональный слой."
        if not layer.isEditable():
            return "Включите режим редактирования слоя."
        wkb = layer.wkbType()
        if QgsWkbTypes.hasZ(wkb) or QgsWkbTypes.hasM(wkb):
            return "Слои с Z/M пока не поддерживаются."
        if QgsWkbTypes.isCurvedType(wkb):
            return "Слои с кривыми пока не поддерживаются."
        return None

    def _apply(self, plan):
        layer = self.layer
        x, y = plan.new_xy

        # собираем все изменения геометрии в памяти: для колец из point_inserts
        # строим новые кольца целиком (plan.new_rings), для point_moves -
        # точечные moveVertex. Так не нужно угадывать поведение addVertex
        # для многокольцевых/многочастичных полигонов.
        by_fid = defaultdict(list)  # fid -> [(part, ri, idx)]
        for (fid, part), ri, idx in plan.point_moves:
            by_fid[fid].append((part, ri, idx))

        layer.beginEditCommand(
            "Добавление вершины (граф рёбер)" if plan.point_inserts
            else "Перенос вершины (граф рёбер)"
        )
        try:
            touched = set(by_fid) | {fid for fid, _, _ in plan.point_inserts}
            # point_inserts/point_moves: face_id = (fid, номер части)
            inserts_by_key = {(face_id, ri): idx
                              for face_id, ri, idx in plan.point_inserts}
            for fid in touched:
                feat = layer.getFeature(fid)
                parts = geometry_parts(feat.geometry())
                keys = {(part, ri) for part, ri, _ in by_fid.get(fid, [])}
                keys |= {(f[1], ri) for (f, ri) in inserts_by_key if f[0] == fid}
                for part, ri in sorted(keys):
                    if ((fid, part), ri) in inserts_by_key:
                        # кольцо целиком из плана: в него уже вставлена новая точка
                        new_ring = plan.new_rings.get(((fid, part), ri))
                        if new_ring is None:
                            raise RuntimeError("no ring plan for fid {}".format(fid))
                        parts[part][ri] = list(new_ring)
                    else:
                        for p, r, idx in by_fid.get(fid, []):
                            if (p, r) == (part, ri):
                                parts[part][ri][idx] = (x, y)
                geom = QgsGeometry.fromPolygonXY(parts) if len(parts) == 1 \
                    else QgsGeometry.fromMultiPolygonXY(parts)
                if not layer.changeGeometry(fid, geom):
                    raise RuntimeError("changeGeometry failed for fid {}".format(fid))
        except Exception as exc:  # откат всей операции целиком
            layer.destroyEditCommand()
            self.iface.messageBar().pushCritical(TITLE, "Правка не применена: {}".format(exc))
            return
        layer.endEditCommand()
        layer.triggerRepaint()
        self.iface.statusBarIface().showMessage(self._area_text(plan), 8000)
