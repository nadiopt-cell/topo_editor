# -*- coding: utf-8 -*-
"""Инструмент карты: топологическое перемещение ребра целиком.

Захват ребра (прямолинейной цепочки вершин) редактируемого слоя +
перетаскивание:
  * ребро — ВСЯ цепочка вершин между углами: промежуточные вершины,
    вставленные ранее на ребро, едут вместе с ним и граница остаётся
    прямой;
  * все якоря цепочки смещаются во ВСЕХ полигонах редактируемого слоя,
    где они есть: полигоны с общим ребром двигаются вместе, полигоны,
    примыкающие только концом, следуют за этим концом;
  * прилипание нового положения концов цепочки к узлам и рёбрам всех
    включенных слоёв (перемещаемые фичи исключены);
  * перехлесты отсекаются автоматически (TopoEditor.move_edge);
  * Esc — отмена перетаскивания / выход из инструмента.
"""

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QColor
from qgis.gui import QgsVertexMarker, QgsRubberBand
from qgis.core import (QgsPointXY, QgsRectangle, QgsGeometry, QgsWkbTypes,
                       Qgis, QgsFeatureRequest)

from .geo_utils import (norm_polys, polys_to_geom, collinear_run,
                        translate_vertices, find_segment, contains_vertex,
                        points_bbox)
from .snapping_engine import get_snap_layers
from .topo_editor import TopoEditor
from .topo_tool_base import BaseTopoTool
from . import utils


class TopoEdgeMoveTool(BaseTopoTool):
    """Перетаскивание целого ребра: все вершины цепочки сразу, во всех
    смежных полигонах редактируемого слоя."""

    def __init__(self, iface, action):
        super(TopoEdgeMoveTool, self).__init__(iface, action)

        # маркеры концов захватываемого ребра (зелёные квадраты)
        self.hover_markers = []
        for _i in (0, 1):
            m = QgsVertexMarker(self.canvas())
            m.setIconType(QgsVertexMarker.ICON_BOX)
            m.setColor(QColor(70, 190, 90))
            m.setPenWidth(2)
            m.hide()
            self.hover_markers.append(m)

        # подсветка захватываемого ребра (оранжевая линия)
        self.hover_edge = QgsRubberBand(self.canvas(),
                                        QgsWkbTypes.LineGeometry)
        self.hover_edge.setStrokeColor(QColor(255, 150, 0))
        self.hover_edge.setWidth(3)

        # перемещаемое ребро во время перетаскивания (красная линия)
        self.drag_edge = QgsRubberBand(self.canvas(), QgsWkbTypes.LineGeometry)
        self.drag_edge.setStrokeColor(QColor(220, 40, 40))
        self.drag_edge.setWidth(3)

    # ------------------------------------------------------------------
    # Служебные
    # ------------------------------------------------------------------

    def _hide_hover(self):
        for m in self.hover_markers:
            m.hide()
        try:
            self.hover_edge.reset(QgsWkbTypes.LineGeometry)
        except Exception:
            pass

    def _cleanup_extra(self):
        self._hide_hover()
        try:
            self.drag_edge.reset(QgsWkbTypes.LineGeometry)
        except Exception:
            pass

    def _find_edge(self, layer, pt_layer, eps):
        """Ближайшее невырожденное ребро слоя к точке (CRS слоя).

        Ребро разворачивается в полную прямолинейную цепочку вершин
        (промежуточные вершины на прямой включаются).

        Ошибки чтения данных НЕ проглатываются — они поднимаются выше,
        чтобы инструмент показал настоящую причину, а не «нет ребра».

        :return: (anchors, nr) или None; anchors — [QgsPointXY, ...]
        """
        rect = QgsRectangle(pt_layer.x() - eps, pt_layer.y() - eps,
                            pt_layer.x() + eps, pt_layer.y() + eps)
        best = None
        best_nr = None
        req = QgsFeatureRequest(rect)
        req.setSubsetOfAttributes([])
        for feat in layer.getFeatures(req):
            nr = norm_polys(feat.geometry())
            if not nr:
                continue
            r = find_segment(nr[0], pt_layer, eps * eps)
            if r is None:
                continue
            _pi, _ri, _si, a, b, _proj, d2 = r
            if a.sqrDist(b) <= eps * eps:
                continue  # вырожденный сегмент — работа инструмента узлов
            if best is None or d2 < best[6]:
                best = r
                best_nr = nr
        if best is None:
            return None
        pi, ri, si, _a, _b, _proj, _d2 = best
        anchors = collinear_run(best_nr[0][pi][ri], si, eps * eps)
        if len(anchors) < 2 or \
                anchors[0].sqrDist(anchors[-1]) <= eps * eps:
            return None
        return anchors, best_nr

    def _collect_edge_set(self, layer, anchors, eps):
        """Все фичи слоя, содержащие хотя бы одну якорную вершину."""
        rect = points_bbox(anchors)
        if rect is None:
            return []  # пустые якоря — искать нечего
        rect.grow(eps)
        eps2 = eps * eps
        entries = []
        req = QgsFeatureRequest(rect)
        req.setSubsetOfAttributes([])
        for feat in layer.getFeatures(req):
            nr = norm_polys(feat.geometry())
            if not nr:
                continue
            polys, was_multi = nr
            hit = False
            for a in anchors:
                if contains_vertex(polys, a, eps2):
                    hit = True
                    break
            if hit:
                entries.append({"fid": feat.id(), "polys": polys,
                                "multi": was_multi})
        return entries

    def _translation_delta(self, map_pt):
        """Вектор переноса в CRS карты с прилипанием концов цепочки.

        Пробует привязать новое положение каждого конца (узел > ребро,
        как в SnappingEngine); из найденных поправок берётся та, что
        требует наименьшего сдвига. Перенос остаётся параллельным:
        форма ребра не искажается.

        :return: (SnapResult или None, (dx, dy) — итоговый вектор)
        """
        drag = self.drag
        dx = map_pt.x() - drag["press_map"].x()
        dy = map_pt.y() - drag["press_map"].y()
        best = None  # (dist2, snap, (vx, vy))
        for src in (drag["anchors_map"][0], drag["anchors_map"][-1]):
            cand = QgsPointXY(src.x() + dx, src.y() + dy)
            snap = self.engine.snap(cand, exclude=drag["exclude"],
                                    editable_layer=drag["layer"])
            if snap is None:
                continue
            vx = snap.point.x() - cand.x()
            vy = snap.point.y() - cand.y()
            dist2 = vx * vx + vy * vy
            if best is None or dist2 < best[0]:
                best = (dist2, snap, (vx, vy))
        if best is None:
            return None, (dx, dy)
        _d2, snap, (vx, vy) = best
        return snap, (dx + vx, dy + vy)

    def _layer_delta(self, d_map):
        """Вектор переноса из CRS карты в CRS слоя (по нажатию)."""
        drag = self.drag
        try:
            cur = drag["ct_m2l"].transform(QgsPointXY(
                drag["press_map"].x() + d_map[0],
                drag["press_map"].y() + d_map[1]))
            return (cur.x() - drag["press_layer"].x(),
                    cur.y() - drag["press_layer"].y())
        except Exception:
            return d_map

    # ------------------------------------------------------------------
    # События карты
    # ------------------------------------------------------------------

    def _no_edge_message(self, candidates, map_pt):
        """Понятное сообщение: в каких слоях искали и что делать.

        Если рядом с курсором есть ребро полигонального слоя, НЕ
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
                    self.canvas(), lyr, map_pt, kind="edge")
                found = self._find_edge(lyr, pt_layer, eps)
            except Exception:
                continue
            if found is not None:
                bar.pushMessage(
                    u"Топологическое редактирование",
                    u"Ребро есть в слое «{}», но этот слой не находится "
                    u"в режиме редактирования. Включите правку "
                    u"(карандаш) и повторите.".format(lyr.name()),
                    level=Qgis.Warning, duration=6)
                return
        names = u", ".join(u"«{}»".format(l.name()) for l in candidates) \
            if candidates else u"—"
        bar.pushMessage(
            u"Топологическое редактирование",
            u"Рядом с курсором нет ребра редактируемого слоя "
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
            found = None
            layer = None
            eps = ct_m2l = ct_l2m = pt_layer = None
            # первый кандидат — активный слой; далее остальные
            # редактируемые: побеждает слой, под курсором которого
            # найдено ребро
            for lyr in candidates:
                pt_layer, eps, ct_m2l, ct_l2m = utils.layer_tolerance(
                    self.canvas(), lyr, map_pt, kind="edge")
                found = self._find_edge(lyr, pt_layer, eps)
                if found is not None:
                    layer = lyr
                    break
            if found is None:
                self._no_edge_message(candidates, map_pt)
                return
            anchors, _nr = found
            entries = self._collect_edge_set(layer, anchors, eps)
            if not entries:
                return
            self.drag = {
                "layer": layer,
                "anchors": [QgsPointXY(p) for p in anchors],
                "anchors_map": [ct_l2m.transform(QgsPointXY(p))
                                for p in anchors],
                "press_map": QgsPointXY(map_pt),
                "press_layer": QgsPointXY(pt_layer),
                "eps": eps,
                "ct_m2l": ct_m2l,
                "ct_l2m": ct_l2m,
                "entries": entries,
                "exclude": {(layer.id(), en["fid"]) for en in entries},
            }
            self._hide_hover()
            for _ in entries:
                self._make_polygon_rubber()
            self._update_preview(*self._translation_delta(map_pt)[1])
        except Exception as exc:
            self._cleanup()
            self._push_error(exc)

    def canvasMoveEvent(self, e):
        map_pt = e.mapPoint()
        if self.drag is None:
            self._update_hover(map_pt)
            return
        try:
            snap, d_map = self._translation_delta(map_pt)
            self._show_snap_feedback(snap)
            self._update_preview(d_map)
        except Exception as exc:
            utils.log(u"Перетаскивание ребра: сбой превью — {}".format(exc))

    def canvasReleaseEvent(self, e):
        if e.button() != Qt.MouseButton.LeftButton or self.drag is None:
            return
        drag = self.drag
        try:
            _snap, d_map = self._translation_delta(e.mapPoint())
            delta_l = self._layer_delta(d_map)
            if delta_l[0] * delta_l[0] + delta_l[1] * delta_l[1] <= 1e-12:
                self._cleanup()
                return
            editor = TopoEditor(self.iface, drag["layer"])
            report = editor.move_edge(drag["anchors"], delta_l, drag["eps"])
            self._report(report)
        except Exception as exc:
            self._push_error(exc)
        finally:
            self._cleanup()

    # ------------------------------------------------------------------
    # Визуализация
    # ------------------------------------------------------------------

    def _update_hover(self, map_pt):
        """Подсветка ребра, которое будет перемещено (любой слой в правке)."""
        self._hide_hover()
        candidates = utils.resolve_edit_layers(self.iface, quiet=True)
        for layer in candidates:
            try:
                pt_layer, eps, _ct_m2l, ct_l2m = utils.layer_tolerance(
                    self.canvas(), layer, map_pt, kind="edge")
                found = self._find_edge(layer, pt_layer, eps)
            except Exception:
                continue
            if found is None:
                continue
            anchors, _nr = found
            try:
                pts_map = [ct_l2m.transform(QgsPointXY(p)) for p in anchors]
            except Exception:
                continue
            self.hover_edge.setToGeometry(
                QgsGeometry.fromPolylineXY(pts_map), None)
            self.hover_markers[0].setCenter(pts_map[0])
            self.hover_markers[1].setCenter(pts_map[-1])
            for m in self.hover_markers:
                m.show()
            return

    def _update_preview(self, d_map):
        """Резиновые ленты всех перемещаемых полигонов + ребра."""
        drag = self.drag
        if drag is None:
            return
        delta_l = self._layer_delta(d_map)
        eps2 = drag["eps"] * drag["eps"]
        for rb, en in zip(self.rubbers, drag["entries"]):
            new_polys, _ch = translate_vertices(en["polys"], drag["anchors"],
                                                delta_l, eps2)
            g = polys_to_geom(new_polys, en["multi"])
            try:
                g.transform(drag["ct_l2m"])
            except Exception:
                pass
            rb.setToGeometry(g, None)
        try:
            line = [QgsPointXY(p.x() + d_map[0], p.y() + d_map[1])
                    for p in drag["anchors_map"]]
            self.drag_edge.setToGeometry(
                QgsGeometry.fromPolylineXY(line), None)
        except Exception:
            pass

    def _report(self, report):
        bar = self.iface.messageBar()
        if report.get("error"):
            bar.pushMessage(u"Топологическое редактирование",
                            u"Отмена: {}".format(report["error"]),
                            level=Qgis.Warning, duration=5)
            return
        if report.get("moved"):
            msg = u"Ребро перемещено; полигонов обновлено: {}".format(
                report["moved"])
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
