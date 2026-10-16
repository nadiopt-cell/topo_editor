# -*- coding: utf-8 -*-
"""Инструмент карты: топологическое перемещение узлов.

ЛКМ по узлу + перетаскивание:
  * узел смещается во ВСЕХ полигонах редактируемого слоя, где он есть
    (совпадение по TOPO_MATCH_EPS, НЕ по snap-допуску);
  * при hover/drag подсвечиваются ВСЕ связанные вершины (тот же узел);
  * прилипание к узлам и рёбрам всех включенных слоёв в экстенте;
    вершины слоёв прилипания подсвечиваются (кандидаты рядом с курсором и
    все вершины объекта-цели);
  * перехлёсты с полигонами других слоёв отсекаются автоматически, а с
    объектами ЭТОГО ЖЕ слоя — по запросу пользователя (красная область в
    превью, диалог при отпускании; TopoEditor.move_vertex);
  * Esc — отмена перетаскивания / выход из инструмента.
"""

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QColor
from qgis.gui import QgsVertexMarker
from qgis.core import (QgsPointXY, QgsRectangle, QgsGeometry, Qgis,
                       QgsFeatureRequest)

from .geo_utils import (norm_polys, polys_to_geom, replace_vertices,
                        find_vertex, contains_vertex, collect_matching_points,
                        TOPO_MATCH_EPS, TOPO_MATCH_EPS2)
from .snapping_engine import get_snap_layers
from .topo_editor import TopoEditor
from .topo_tool_base import BaseTopoTool
from . import utils

# Максимум маркеров связанных узлов (защита UI на очень плотных данных)
_MAX_RELATED_MARKERS = 64


class TopoMoveTool(BaseTopoTool):
    """Перетаскивание общего узла всех смежных полигонов редактируемого слоя."""

    def __init__(self, iface, action):
        super(TopoMoveTool, self).__init__(iface, action)

        # маркер ближайшего узла под курсором (зелёный квадрат)
        self.hover_marker = QgsVertexMarker(self.canvas())
        self.hover_marker.setIconType(QgsVertexMarker.ICON_BOX)
        self.hover_marker.setColor(QColor(70, 190, 90))
        self.hover_marker.setPenWidth(2)
        self.hover_marker.setIconSize(12)
        self.hover_marker.hide()

        # маркеры ВСЕХ связанных вершин (тот же топологический узел)
        self.related_markers = []

    # ------------------------------------------------------------------
    # Служебные
    # ------------------------------------------------------------------

    def _cleanup_extra(self):
        self.hover_marker.hide()
        self._hide_related()

    def _hide_related(self):
        for m in self.related_markers:
            m.hide()

    def _ensure_related_markers(self, n):
        """Пул маркеров связанных узлов (не больше _MAX_RELATED_MARKERS)."""
        n = min(n, _MAX_RELATED_MARKERS)
        while len(self.related_markers) < n:
            m = QgsVertexMarker(self.canvas())
            m.setIconType(QgsVertexMarker.ICON_BOX)
            # чуть бледнее основного hover — «связанные»
            m.setColor(QColor(40, 160, 70))
            m.setPenWidth(1)
            m.setIconSize(9)
            m.hide()
            self.related_markers.append(m)
        return n

    def _show_related(self, pts_map):
        """Подсветка связанных вершин (координаты уже в CRS карты)."""
        if not pts_map:
            self._hide_related()
            return
        n = self._ensure_related_markers(len(pts_map))
        for i, m in enumerate(self.related_markers):
            if i < n and i < len(pts_map):
                m.setCenter(pts_map[i])
                m.show()
            else:
                m.hide()

    def _find_vertex(self, layer, pt_layer, snap_eps):
        """Ближайший узел слоя к точке (CRS слоя) в пределах snap_eps.

        Ошибки чтения данных НЕ проглатываются — они поднимаются выше,
        чтобы инструмент показал настоящую причину, а не «нет узла».

        :return: (point QgsPointXY, dist2) или None
        """
        rect = QgsRectangle(pt_layer.x() - snap_eps, pt_layer.y() - snap_eps,
                            pt_layer.x() + snap_eps, pt_layer.y() + snap_eps)
        best = None
        req = QgsFeatureRequest(rect)
        req.setSubsetOfAttributes([])
        for feat in layer.getFeatures(req):
            nr = norm_polys(feat.geometry())
            if not nr:
                continue
            r = find_vertex(nr[0], pt_layer, snap_eps * snap_eps)
            if r is not None and (best is None or r[1] < best[1]):
                best = (r[0], r[1])
        return best

    def _collect_move_set(self, layer, picked, match_eps):
        """Все фичи слоя, содержащие узел picked (топологическое совпадение).

        match_eps — TOPO_MATCH_EPS, НЕ snap-допуск. Иначе соседние узлы
        в радиусе прилипания ошибочно «склеиваются» и едут вместе.
        """
        rect = QgsRectangle(picked.x() - match_eps, picked.y() - match_eps,
                            picked.x() + match_eps, picked.y() + match_eps)
        # небольшой запас на пространственный индекс / float
        rect.grow(match_eps)
        match2 = match_eps * match_eps
        entries = []
        related = []
        req = QgsFeatureRequest(rect)
        req.setSubsetOfAttributes([])
        for feat in layer.getFeatures(req):
            nr = norm_polys(feat.geometry())
            if not nr:
                continue
            polys, was_multi = nr
            if contains_vertex(polys, picked, match2):
                entries.append({"fid": feat.id(), "polys": polys,
                                "multi": was_multi})
                for p in collect_matching_points(polys, picked, match2):
                    related.append(p)
        return entries, related

    def _pick_best_vertex(self, candidates, map_pt):
        """Среди всех editable-слоёв — ближайший узел (не first-hit).

        :return: (layer, picked, dist2, snap_eps, ct_m2l, ct_l2m) или None
        """
        best = None
        for lyr in candidates:
            pt_layer, snap_eps, ct_m2l, ct_l2m = utils.layer_tolerance(
                self.canvas(), lyr, map_pt, kind="vertex")
            found = self._find_vertex(lyr, pt_layer, snap_eps)
            if found is None:
                continue
            picked, dist2 = found
            if best is None or dist2 < best[2]:
                best = (lyr, picked, dist2, snap_eps, ct_m2l, ct_l2m)
        return best

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
                pt_layer, snap_eps, _m2l, _l2m = utils.layer_tolerance(
                    self.canvas(), lyr, map_pt, kind="vertex")
                found = self._find_vertex(lyr, pt_layer, snap_eps)
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
            best = self._pick_best_vertex(candidates, map_pt)
            if best is None:
                self._no_vertex_message(candidates, map_pt)
                return
            layer, picked, _d2, snap_eps, ct_m2l, ct_l2m = best
            entries, related = self._collect_move_set(
                layer, picked, TOPO_MATCH_EPS)
            if not entries:
                return
            self.drag = {
                "layer": layer,
                "picked": QgsPointXY(picked),
                "match_eps": TOPO_MATCH_EPS,
                "snap_eps": snap_eps,
                "ct_m2l": ct_m2l,
                "ct_l2m": ct_l2m,
                "entries": entries,
                "exclude": {(layer.id(), en["fid"]) for en in entries},
            }
            for _ in entries:
                self._make_polygon_rubber()
            # подсветка всех связанных вершин сразу после захвата
            try:
                pts_map = [ct_l2m.transform(QgsPointXY(p)) for p in related]
            except Exception:
                pts_map = []
            self._show_related(pts_map)
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
            if new_pt.sqrDist(drag["picked"]) <= TOPO_MATCH_EPS2:
                self._cleanup()
                return
            editor = TopoEditor(self.iface, drag["layer"], self._ask_overlap,
                                self._ask_makevalid)
            report = editor.move_vertex(
                drag["picked"], new_pt, drag["match_eps"])
            self._report(report, drag["ct_l2m"])
        except Exception as exc:
            self._push_error(exc)
        finally:
            self._cleanup()

    # ------------------------------------------------------------------
    # Визуализация
    # ------------------------------------------------------------------

    def _update_hover(self, map_pt):
        """Ближайший узел + все связанные вершины (любой слой в правке)."""
        self.hover_marker.hide()
        self._hide_related()
        candidates = utils.resolve_edit_layers(self.iface, quiet=True)
        if not candidates:
            return
        try:
            best = self._pick_best_vertex(candidates, map_pt)
        except Exception:
            return
        if best is None:
            return
        layer, picked, _d2, _snap_eps, _ct_m2l, ct_l2m = best
        try:
            self.hover_marker.setCenter(ct_l2m.transform(QgsPointXY(picked)))
            self.hover_marker.show()
        except Exception:
            return
        # связанные узлы на hover (тот же topo-match)
        try:
            _entries, related = self._collect_move_set(
                layer, picked, TOPO_MATCH_EPS)
            pts_map = [ct_l2m.transform(QgsPointXY(p)) for p in related]
            self._show_related(pts_map)
        except Exception:
            pass

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
        match2 = drag["match_eps"] * drag["match_eps"]
        prepared = []  # [(fid, геометрия в CRS слоя)] — для красной области
        for rb, en in zip(self.rubbers, drag["entries"]):
            new_polys, _ch = replace_vertices(en["polys"], drag["picked"],
                                              new_pt, match2)
            g = polys_to_geom(new_polys, en["multi"])
            prepared.append((en["fid"], QgsGeometry(g)))
            try:
                g.transform(drag["ct_l2m"])
            except Exception:
                pass
            rb.setToGeometry(g, None)
        self._update_validator_preview(drag["layer"], drag["ct_l2m"], prepared)

    def _report(self, report, ct_l2m=None):
        bar = self.iface.messageBar()
        if report.get("error"):
            if report.get("violations"):
                self._push_violation_error(report, ct_l2m)
                return
            bar.pushMessage(u"Топологическое редактирование",
                            report["error"], level=Qgis.Warning, duration=4)
            return
        if report.get("cancelled"):
            self._push_cancelled()
            return
        parts = [u"изменено полигонов: {}".format(report.get("moved", 0))]
        if report.get("clipped"):
            parts.append(u"отсечено перехлестов: {}".format(
                report["clipped"]))
        parts.extend(self._overlap_notes(report))
        if report.get("skipped"):
            parts.append(u"пропущено: {}".format(report["skipped"]))
        bar.pushMessage(u"Топологическое редактирование",
                        u"; ".join(parts), level=Qgis.Success, duration=4)
