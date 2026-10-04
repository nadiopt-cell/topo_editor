# -*- coding: utf-8 -*-
"""Инструмент карты: топологическое перемещение узлов.

ЛКМ по узлу + перетаскивание:
  * узел смещается во ВСЕХ полигонах редактируемого слоя, где он есть;
  * прилипание к узлам и рёбрам всех включенных слоёв в экстенте;
  * перехлесты отсекаются автоматически (TopoEditor.move_vertex);
  * Esc — отмена перетаскивания / выход из инструмента.
"""

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QColor
from qgis.gui import QgsVertexMarker
from qgis.core import (QgsPointXY, QgsRectangle, Qgis, QgsFeatureRequest)

from .geo_utils import (norm_polys, polys_to_geom, replace_vertices,
                        find_vertex, contains_vertex)
from .snapping_engine import get_snap_layers
from .topo_editor import TopoEditor
from .topo_tool_base import BaseTopoTool
from . import utils


class TopoMoveTool(BaseTopoTool):
    """Перетаскивание общего узла всех смежных полигонов редактируемого слоя."""

    def __init__(self, iface, action):
        super(TopoMoveTool, self).__init__(iface, action)

        # маркер узла под курсором (зелёный квадрат)
        self.hover_marker = QgsVertexMarker(self.canvas())
        self.hover_marker.setIconType(QgsVertexMarker.ICON_BOX)
        self.hover_marker.setColor(QColor(70, 190, 90))
        self.hover_marker.setPenWidth(2)
        self.hover_marker.hide()

    # ------------------------------------------------------------------
    # Служебные
    # ------------------------------------------------------------------

    def _cleanup_extra(self):
        self.hover_marker.hide()

    def _find_vertex(self, layer, pt_layer, eps):
        """Ближайший узел слоя к точке (CRS слоя) в пределах eps.

        Ошибки чтения данных НЕ проглатываются — они поднимаются выше,
        чтобы инструмент показал настоящую причину, а не «нет узла».
        """
        rect = QgsRectangle(pt_layer.x() - eps, pt_layer.y() - eps,
                            pt_layer.x() + eps, pt_layer.y() + eps)
        best = None
        req = QgsFeatureRequest(rect)
        req.setSubsetOfAttributes([])
        for feat in layer.getFeatures(req):
            nr = norm_polys(feat.geometry())
            if not nr:
                continue
            r = find_vertex(nr[0], pt_layer, eps * eps)
            if r is not None and (best is None or r[1] < best[1]):
                best = (r[0], r[1])
        return best[0] if best is not None else None

    def _collect_move_set(self, layer, picked, eps):
        """Все фичи слоя, содержащие узел picked в пределах eps."""
        rect = QgsRectangle(picked.x() - eps, picked.y() - eps,
                            picked.x() + eps, picked.y() + eps)
        entries = []
        req = QgsFeatureRequest(rect)
        req.setSubsetOfAttributes([])
        for feat in layer.getFeatures(req):
            nr = norm_polys(feat.geometry())
            if not nr:
                continue
            polys, was_multi = nr
            if contains_vertex(polys, picked, eps * eps):
                entries.append({"fid": feat.id(), "polys": polys,
                                "multi": was_multi})
        return entries

    # ------------------------------------------------------------------
    # События карты
    # ------------------------------------------------------------------

    def _no_vertex_message(self, candidates, map_pt):
        """Понятное сообщение: в каких слоях искали и что делать.

        Если рядом с курсором есть узел полигонального слоя, НЕ
        находящегося в правке, — подсказка включить редактирование.
        """
        bar = self.iface.messageBar()
        edit_ids = {l.id() for l in candidates}
        for lyr in get_snap_layers(None):
            if not utils.is_polygon_layer(lyr) or lyr.isEditable() \
                    or lyr.id() in edit_ids:
                continue
            try:
                pt_layer, eps, _m2l, _l2m = utils.layer_tolerance(
                    self.canvas(), lyr, map_pt)
                found = self._find_vertex(lyr, pt_layer, eps)
            except Exception:
                continue
            if found is not None:
                bar.pushMessage(
                    u"Топологическое редактирование",
                    u"Узел есть в слое «{}», но этот слой не находится "
                    u"в режиме редактирования. Включите правку "
                    u"(карандаш) и повторите.".format(lyr.name()),
                    level=Qgis.Warning, duration=6)
                return
        names = u", ".join(u"«{}»".format(l.name()) for l in candidates) \
            if candidates else u"—"
        bar.pushMessage(
            u"Топологическое редактирование",
            u"Рядом с курсором нет узла редактируемого слоя "
            u"(поиск по: {}).".format(names),
            level=Qgis.Info, duration=4)

    def canvasPressEvent(self, e):
        if e.button() != Qt.MouseButton.LeftButton:
            return
        try:
            candidates = utils.resolve_edit_layers(self.iface,
                                                   offer_start=True)
            if not candidates:
                return
            map_pt = e.mapPoint()
            picked = None
            layer = None
            pt_layer = eps = ct_m2l = ct_l2m = None
            # первый кандидат — активный слой; далее остальные
            # редактируемые: побеждает слой, под курсором которого есть узел
            for lyr in candidates:
                pt_layer, eps, ct_m2l, ct_l2m = utils.layer_tolerance(
                    self.canvas(), lyr, map_pt)
                v = self._find_vertex(lyr, pt_layer, eps)
                if v is not None:
                    picked = v
                    layer = lyr
                    break
            if picked is None:
                self._no_vertex_message(candidates, map_pt)
                return
            entries = self._collect_move_set(layer, picked, eps)
            if not entries:
                return
            self.drag = {
                "layer": layer,
                "picked": QgsPointXY(picked),
                "eps": eps,
                "ct_m2l": ct_m2l,
                "ct_l2m": ct_l2m,
                "entries": entries,
                "exclude": {(layer.id(), en["fid"]) for en in entries},
            }
            for _ in entries:
                self._make_polygon_rubber()
            self._update_preview(map_pt, None)
        except Exception as exc:
            self._cleanup()
            self._push_error(exc)

    def canvasMoveEvent(self, e):
        map_pt = e.mapPoint()
        if self.drag is None:
            self._update_hover(map_pt)
            return
        try:
            snap = self.engine.snap(map_pt, exclude=self.drag["exclude"],
                                    editable_layer=self.drag["layer"])
            self._show_snap_feedback(snap)
            self._update_preview(map_pt, snap)
        except Exception as exc:
            utils.log(u"Перетаскивание узла: сбой превью — {}".format(exc))

    def canvasReleaseEvent(self, e):
        if e.button() != Qt.MouseButton.LeftButton or self.drag is None:
            return
        drag = self.drag
        try:
            map_pt = e.mapPoint()
            snap = self.engine.snap(map_pt, exclude=drag["exclude"],
                                    editable_layer=drag["layer"])
            new_map_pt = snap.point if snap is not None else map_pt
            try:
                new_pt = drag["ct_m2l"].transform(new_map_pt)
            except Exception:
                new_pt = drag["ct_m2l"].transform(map_pt)
            if new_pt.sqrDist(drag["picked"]) <= 1e-12:
                self._cleanup()
                return
            editor = TopoEditor(self.iface, drag["layer"])
            report = editor.move_vertex(drag["picked"], new_pt, drag["eps"])
            self._report(report)
        except Exception as exc:
            self._push_error(exc)
        finally:
            self._cleanup()

    # ------------------------------------------------------------------
    # Визуализация
    # ------------------------------------------------------------------

    def _update_hover(self, map_pt):
        """Показ узла, который будет перемещён (любой редактируемый слой)."""
        self.hover_marker.hide()
        candidates = utils.resolve_edit_layers(self.iface, quiet=True)
        for layer in candidates:
            try:
                pt_layer, eps, _ct_m2l, ct_l2m = utils.layer_tolerance(
                    self.canvas(), layer, map_pt)
                picked = self._find_vertex(layer, pt_layer, eps)
            except Exception:
                continue
            if picked is None:
                continue
            self.hover_marker.setCenter(ct_l2m.transform(picked))
            self.hover_marker.show()
            return

    def _update_preview(self, map_pt, snap):
        """Резиновые ленты всех перемещаемых полигонов."""
        drag = self.drag
        if drag is None:
            return
        new_map_pt = snap.point if snap is not None else map_pt
        try:
            new_pt = drag["ct_m2l"].transform(new_map_pt)
        except Exception:
            return
        eps2 = drag["eps"] * drag["eps"]
        for rb, en in zip(self.rubbers, drag["entries"]):
            new_polys, _ch = replace_vertices(en["polys"], drag["picked"],
                                              new_pt, eps2)
            g = polys_to_geom(new_polys, en["multi"])
            try:
                g.transform(drag["ct_l2m"])
            except Exception:
                pass
            rb.setToGeometry(g, None)

    def _report(self, report):
        bar = self.iface.messageBar()
        if report.get("error"):
            bar.pushMessage(u"Топологическое редактирование",
                            u"Отмена: {}".format(report["error"]),
                            level=Qgis.Warning, duration=5)
            return
        if report.get("moved"):
            msg = u"Полигонов обновлено: {}".format(report["moved"])
            if report.get("clipped"):
                msg += u"; перехлестов отсечено: {}".format(report["clipped"])
            if report.get("skipped"):
                msg += u"; пропущено: {}".format(report["skipped"])
            bar.pushMessage(u"Топологическое редактирование", msg,
                            level=Qgis.Success, duration=4)
        else:
            bar.pushMessage(u"Топологическое редактирование",
                            u"Изменений не выполнено",
                            level=Qgis.Info, duration=3)
