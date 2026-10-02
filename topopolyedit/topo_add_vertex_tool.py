# -*- coding: utf-8 -*-
"""Инструмент карты: топологическая вставка узла на ребро.

Клик по ребру полигонального слоя:
  * узел вставляется во ВСЕ полигоны редактируемого слоя, содержащие
    это ребро — в том числе при T-образных примыканиях;
  * точка вставки учитывает прилипание ко всем включенным слоям;
  * Esc — выход из инструмента.
"""

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QColor
from qgis.gui import QgsMapTool, QgsVertexMarker, QgsRubberBand
from qgis.core import (QgsPointXY, QgsRectangle, QgsGeometry, QgsWkbTypes,
                       Qgis, QgsFeatureRequest)

from .geo_utils import norm_polys, find_segment, point_to_segment
from .snapping_engine import SnappingEngine
from .topo_editor import TopoEditor
from . import utils


class TopoAddVertexTool(QgsMapTool):
    """Вставка узла на ребро сразу во все смежные полигоны слоя."""

    def __init__(self, iface, action):
        super(TopoAddVertexTool, self).__init__(iface.mapCanvas())
        self.iface = iface
        self.action = action
        self.engine = SnappingEngine(iface.mapCanvas())

        # подсветка ребра, на которое можно вставить узел
        self.edge_rb = QgsRubberBand(self.canvas(), QgsWkbTypes.LineGeometry)
        self.edge_rb.setStrokeColor(QColor(255, 150, 0))
        self.edge_rb.setWidth(3)

        # маркер точки вставки
        self.marker = QgsVertexMarker(self.canvas())
        self.marker.setIconType(QgsVertexMarker.ICON_CIRCLE)
        self.marker.setColor(QColor(255, 255, 255))
        self.marker.setPenWidth(2)
        self.marker.hide()

    # ------------------------------------------------------------------
    def _cleanup(self):
        try:
            self.edge_rb.reset(QgsWkbTypes.LineGeometry)
        except Exception:
            pass
        self.marker.hide()

    def activate(self):
        self.setCursor(Qt.CursorShape.CrossCursor)
        super(TopoAddVertexTool, self).activate()

    def deactivate(self):
        self._cleanup()
        if self.action is not None and self.action.isChecked():
            self.action.setChecked(False)
        super(TopoAddVertexTool, self).deactivate()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key.Key_Escape:
            self.iface.actionPan().trigger()
            return
        super(TopoAddVertexTool, self).keyPressEvent(e)

    # ------------------------------------------------------------------
    def _find_segment(self, layer, pt_layer, eps):
        """Ближайшее ребро слоя к точке (CRS слоя) в пределах eps."""
        rect = QgsRectangle(pt_layer.x() - eps, pt_layer.y() - eps,
                            pt_layer.x() + eps, pt_layer.y() + eps)
        best = None
        try:
            req = QgsFeatureRequest(rect)
            req.setSubsetOfAttributes([])
            for feat in layer.getFeatures(req):
                nr = norm_polys(feat.geometry())
                if not nr:
                    continue
                r = find_segment(nr[0], pt_layer, eps * eps)
                if r is not None and (best is None or r[6] < best[6]):
                    best = r
        except Exception:
            return None
        return best

    def canvasMoveEvent(self, e):
        self._cleanup()
        layer = utils.resolve_edit_layer(self.iface, quiet=True)
        if layer is None:
            return
        try:
            pt_layer, eps, _ct_m2l, ct_l2m = utils.layer_tolerance(
                self.canvas(), layer, e.mapPoint())
        except Exception:
            return
        seg = self._find_segment(layer, pt_layer, eps)
        if seg is None:
            return
        _pi, _ri, _si, a, b, proj, _d2 = seg
        try:
            am = ct_l2m.transform(QgsPointXY(a))
            bm = ct_l2m.transform(QgsPointXY(b))
            pm = ct_l2m.transform(QgsPointXY(proj))
        except Exception:
            return
        self.edge_rb.setToGeometry(QgsGeometry.fromPolylineXY([am, bm]), None)
        self.marker.setCenter(pm)
        self.marker.show()

    def canvasPressEvent(self, e):
        if e.button() != Qt.MouseButton.LeftButton:
            return
        try:
            layer = utils.resolve_edit_layer(self.iface, offer_start=True)
            if layer is None:
                return
            map_pt = e.mapPoint()
            pt_layer, eps, ct_m2l, _ct_l2m = utils.layer_tolerance(
                self.canvas(), layer, map_pt)
            seg = self._find_segment(layer, pt_layer, eps)
            if seg is None:
                self.iface.messageBar().pushMessage(
                    u"Топологическое редактирование",
                    u"Рядом с курсором нет ребра редактируемого слоя.",
                    level=Qgis.Info, duration=2)
                return
            _pi, _ri, _si, a, b, _proj, _d2 = seg

            # прилипание ко всем включенным слоям (если есть)
            snap = self.engine.snap(map_pt, exclude=None, editable_layer=layer)
            src_map = snap.point if snap is not None else map_pt
            try:
                src_layer = ct_m2l.transform(src_map)
            except Exception:
                src_layer = pt_layer
            proj, _d2, _t = point_to_segment(src_layer, a, b)

            editor = TopoEditor(self.iface, layer)
            report = editor.add_vertex_on_edge(a, b, proj, eps)
            self._report(report)
        except Exception as exc:
            self.iface.messageBar().pushMessage(
                u"Топологическое редактирование", u"Ошибка: {}".format(exc),
                level=Qgis.Critical, duration=5)
        finally:
            self._cleanup()

    # ------------------------------------------------------------------
    def _report(self, report):
        bar = self.iface.messageBar()
        if report.get("error"):
            bar.pushMessage(u"Топологическое редактирование",
                            u"Вставка не выполнена: {}".format(report["error"]),
                            level=Qgis.Warning, duration=5)
            return
        if report.get("features"):
            msg = u"Узел добавлен; полигонов обновлено: {}".format(
                report["features"])
            if report.get("inserted", 0) > report["features"]:
                msg += u"; вставок (с примыканиями): {}".format(
                    report["inserted"])
            bar.pushMessage(u"Топологическое редактирование", msg,
                            level=Qgis.Success, duration=4)
        else:
            bar.pushMessage(u"Топологическое редактирование",
                            u"Изменений не выполнено",
                            level=Qgis.Info, duration=3)
