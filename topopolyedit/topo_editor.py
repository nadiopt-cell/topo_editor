# -*- coding: utf-8 -*-
"""Применение топологических изменений к РЕДАКТИРУЕМОМУ слою.

Ключевой принцип: изменяется только редактируемый слой. Геометрии прочих
слоёв (и прочих фич того же слоя) используются исключительно как эталоны:

  * прилипание — см. snapping_engine.SnappingEngine;
  * автоотсечение перехлестов — QgsGeometry.difference от эталонных
    полигонов включенных слоёв, кроме перемещаемых фич.

Все изменения одной операции выполняются в едином edit-командном блоке
(beginEditCommand / endEditCommand), поэтому Ctrl+Z откатывает их разом.
"""

from qgis.core import (
    QgsProject, QgsFeatureRequest, QgsCoordinateTransform, QgsGeometry,
    QgsRectangle, QgsWkbTypes, Qgis, QgsVectorLayer,
)

from .geo_utils import (
    norm_polys, polys_to_geom, replace_vertices, insert_vertex_topo,
    polys_bbox, contains_vertex, translate_vertices,
)
from .snapping_engine import get_snap_layers

MAX_CLIP_REFS = 5000  # предохранитель на число эталонных полигонов


class TopoEditor(object):
    """Исполнитель топологических операций над редактируемым слоем."""

    def __init__(self, iface, layer):
        """
        :param iface: QgisInterface
        :param layer: редактируемый ПОЛИГОНАЛЬНЫЙ слой (в режиме правки)
        """
        self.iface = iface
        self.layer = layer

    # ------------------------------------------------------------------
    # Внутренние помощники
    # ------------------------------------------------------------------

    def _collect_moved(self, old_pt, new_pt, eps):
        """Фичи слоя, содержащие вершину old_pt, с подставленной new_pt.

        :return: [{"fid", "polys", "multi"}, ...]
        """
        rect = QgsRectangle(old_pt.x() - eps, old_pt.y() - eps,
                            old_pt.x() + eps, old_pt.y() + eps)
        eps2 = eps * eps
        entries = []
        req = QgsFeatureRequest(rect)
        req.setSubsetOfAttributes([])
        for feat in self.layer.getFeatures(req):
            nr = norm_polys(feat.geometry())
            if not nr:
                continue
            polys, was_multi = nr
            new_polys, changed = replace_vertices(polys, old_pt, new_pt, eps2)
            if changed:
                entries.append({"fid": feat.id(), "polys": new_polys,
                                "multi": was_multi})
        return entries

    def _clip_references(self, bbox, exclude_ids):
        """Эталонные полигоны для отсечения перехлестов.

        Берутся все включенные слои (и сам редактируемый слой), кроме
        перемещаемых фич. Геометрии приводятся к CRS редактируемого слоя.
        Слои только читаются — ничего в них не изменяется.
        """
        refs = []
        dest = self.layer.crs()
        for lyr in get_snap_layers(self.layer):
            if lyr.geometryType() != QgsWkbTypes.PolygonGeometry:
                continue  # отсекать можно только полигонами
            same = lyr.crs().authid() == dest.authid()
            ct = None
            if same:
                rect = bbox
            else:
                try:
                    ct_d2l = QgsCoordinateTransform(dest, lyr.crs(),
                                                    QgsProject.instance())
                    rect = ct_d2l.transformBoundingBox(bbox)
                    ct = QgsCoordinateTransform(lyr.crs(), dest,
                                                QgsProject.instance())
                except Exception:
                    continue
            try:
                req = QgsFeatureRequest(rect)
                req.setSubsetOfAttributes([])
                for feat in lyr.getFeatures(req):
                    if lyr.id() == self.layer.id() \
                            and feat.id() in exclude_ids:
                        continue
                    g = feat.geometry()
                    if g is None or g.isNull() or g.isEmpty():
                        continue
                    if ct is not None:
                        try:
                            g = QgsGeometry(g)
                            if g.transform(ct) != 0:
                                continue
                        except Exception:
                            continue
                    refs.append(g)
                    if len(refs) >= MAX_CLIP_REFS:
                        return refs, True
            except Exception:
                continue
        return refs, False

    @staticmethod
    def _clip_against_refs(g, refs):
        """difference() последовательно по всем эталонам.

        :return: (geometry, clipped_count) — может вернуть пустую геометрию,
                 если полигон полностью поглощён соседями.
        """
        clipped = 0
        for ref in refs:
            if g is None or g.isNull() or g.isEmpty():
                break
            try:
                gb = g.boundingBox()
                if not gb.intersects(ref.boundingBox()):
                    continue
                g2 = g.difference(ref)
            except Exception:
                continue
            if g2 is None or g2.isNull() or g2.isEmpty():
                g = QgsGeometry()  # полигон полностью поглощён
                break
            if g2.type() == QgsWkbTypes.PolygonGeometry:
                g = g2
                clipped += 1
            else:
                try:
                    g3 = g2.makeValid()
                    if not g3.isNull() and not g3.isEmpty() \
                            and g3.type() == QgsWkbTypes.PolygonGeometry:
                        g = g3
                        clipped += 1
                except Exception:
                    pass
        return g, clipped

    def _push(self, text, level=Qgis.Info, duration=4):
        self.iface.messageBar().pushMessage(
            u"Топологическое редактирование", text,
            level=level, duration=duration)

    def _apply_moves(self, entries, exclude_ids, command_text, eps):
        """Единый edit-командный блок: запись новых геометрий + отсечение.

        Общий конвейер операций перемещения (узла и ребра): габарит
        новых геометрий, подбор эталонов, makeValid для невалидных
        колец, difference по эталонам, changeGeometry. Одна операция
        == одна команда отмены (Ctrl+Z).

        :param entries: [{"fid", "polys", "multi"}, ...] — новые геометрии
        :param exclude_ids: {fid, ...} — перемещаемые фичи (не эталоны)
        :param command_text: название edit-команды (видно в истории отмены)
        :param eps: допуск (единицы CRS слоя) — запас габарита
        :return: {"moved", "clipped", "skipped", "refs"}
        """
        report = {"moved": 0, "clipped": 0, "skipped": 0, "refs": 0}

        # габарит всех новых геометрий + запас
        bbox = None
        for e in entries:
            r = polys_bbox(e["polys"])
            if bbox is None:
                bbox = r
            else:
                bbox.combineExtentWith(r)
        bbox.grow(eps)

        refs, truncated = self._clip_references(bbox, exclude_ids)
        report["refs"] = len(refs)
        if truncated:
            self._push(u"Эталонов для отсечения слишком много "
                       u"(>{}), часть перехлестов могла остаться. "
                       u"Уменьшите экстент.".format(MAX_CLIP_REFS),
                       level=Qgis.Warning, duration=6)

        self.layer.beginEditCommand(command_text)
        for e in entries:
            g = polys_to_geom(e["polys"], e["multi"])
            if g.isNull() or g.isEmpty():
                report["skipped"] += 1
                continue
            if not g.isGeosValid():
                try:
                    gv = g.makeValid()
                    if not gv.isNull() and not gv.isEmpty() \
                            and gv.type() == QgsWkbTypes.PolygonGeometry:
                        g = gv
                except Exception:
                    pass

            g, clipped_here = self._clip_against_refs(g, refs)

            if g.isNull() or g.isEmpty():
                # полигон полностью поглощён соседями — не меняем
                report["skipped"] += 1
                continue
            if self.layer.changeGeometry(e["fid"], g):
                report["moved"] += 1
                report["clipped"] += clipped_here
            else:
                report["skipped"] += 1
        self.layer.endEditCommand()
        self.layer.triggerRepaint()
        return report

    # ------------------------------------------------------------------
    # Операция 1: топологическое перемещение узла + автоотсечение
    # ------------------------------------------------------------------

    def move_vertex(self, old_pt, new_pt, eps):
        """Перемещает общий узел во всех полигонах редактируемого слоя
        и отсекает перехлесты, возникшие при подтягивании.

        :param old_pt: исходное положение узла (CRS слоя)
        :param new_pt: новое положение узла (CRS слоя), уже с прилипанием
        :param eps: допуск совпадения узла (единицы CRS слоя)
        :return: {"moved", "clipped", "skipped", "refs", "error"}
        """
        report = {"moved": 0, "clipped": 0, "skipped": 0, "refs": 0,
                  "error": None}
        if new_pt.sqrDist(old_pt) <= 1e-12:
            report["error"] = u"узел не смещён"
            return report
        try:
            entries = self._collect_moved(old_pt, new_pt, eps)
            if not entries:
                report["error"] = u"узел не найден в полигонах слоя"
                return report
            report.update(self._apply_moves(
                entries, {e["fid"] for e in entries},
                u"Топологическое перемещение узла", eps))
        except Exception as exc:
            try:
                self.layer.destroyEditCommand()
            except Exception:
                pass
            report["error"] = str(exc)
        return report

    # ------------------------------------------------------------------
    # Операция 1б: топологическое перемещение ребра целиком
    # ------------------------------------------------------------------

    def _collect_edge_moved(self, anchors, delta, eps):
        """Фичи слоя, содержащие хотя бы одну якорную вершину цепочки,
        с якорями, сдвинутыми на вектор delta.

        Полигон, содержащий только часть якорей (примыкание "только
        концом", как полигон C на вершине общего ребра A и B),
        смещается этими якорями — топология сохраняется.

        :param anchors: [QgsPointXY, ...] — исходные вершины цепочки
        :param delta: (dx, dy) — вектор переноса (CRS слоя)
        :param eps: допуск совпадения узлов (единицы CRS слоя)
        :return: [{"fid", "polys", "multi"}, ...]
        """
        dx, dy = delta
        pad = eps + max(abs(dx), abs(dy))
        rect = None
        for p in anchors:
            r = QgsRectangle(p.x(), p.y(), p.x(), p.y())
            rect = r if rect is None else rect.combineExtentWith(r)
        rect.grow(pad)
        eps2 = eps * eps
        entries = []
        req = QgsFeatureRequest(rect)
        req.setSubsetOfAttributes([])
        for feat in self.layer.getFeatures(req):
            nr = norm_polys(feat.geometry())
            if not nr:
                continue
            polys, was_multi = nr
            new_polys, changed = translate_vertices(polys, anchors, delta,
                                                    eps2)
            if changed:
                entries.append({"fid": feat.id(), "polys": new_polys,
                                "multi": was_multi})
        return entries

    def move_edge(self, anchors, delta, eps):
        """Перемещает ребро — прямолинейную цепочку вершин — параллельным
        переносом и отсекает перехлесты.

        Якоря — вершины цепочки (концы и промежуточные, вставленные на
        ребро). Смещаются во ВСЕХ полигонах редактируемого слоя, где
        они есть: полигоны с общим ребром двигаются целиком, полигоны,
        примыкающие только частью якорей, следуют за ними.

        :param anchors: [QgsPointXY, ...] — вершины цепочки (CRS слоя),
                        минимум две; первая и последняя — концы ребра
        :param delta: (dx, dy) — вектор переноса (CRS слоя),
                      уже с прилипанием
        :param eps: допуск совпадения узлов (единицы CRS слоя)
        :return: {"moved", "clipped", "skipped", "refs", "error"}
        """
        report = {"moved": 0, "clipped": 0, "skipped": 0, "refs": 0,
                  "error": None}
        if not anchors or len(anchors) < 2:
            report["error"] = u"ребро не задано"
            return report
        dx, dy = delta
        if dx * dx + dy * dy <= 1e-12:
            report["error"] = u"ребро не смещено"
            return report
        if anchors[0].sqrDist(anchors[-1]) <= eps * eps:
            report["error"] = u"ребро вырождено (короче допуска)"
            return report
        try:
            entries = self._collect_edge_moved(anchors, (dx, dy), eps)
            if not entries:
                report["error"] = u"ребро не найдено в полигонах слоя"
                return report
            report.update(self._apply_moves(
                entries, {e["fid"] for e in entries},
                u"Топологическое перемещение ребра", eps))
        except Exception as exc:
            try:
                self.layer.destroyEditCommand()
            except Exception:
                pass
            report["error"] = str(exc)
        return report

    # ------------------------------------------------------------------
    # Операция 2: топологическая вставка узла на ребро
    # ------------------------------------------------------------------

    def add_vertex_on_edge(self, p1, p2, new_pt, eps):
        """Вставляет узел на ребро (p1, p2) во все полигоны редактируемого
        слоя, содержащие это ребро (включая T-образные примыкания).

        :param p1, p2: концы ребра, по которому выполнен клик (CRS слоя)
        :param new_pt: точка вставки на ребре (CRS слоя)
        :param eps: допуск совпадения рёбер (единицы CRS слоя)
        :return: {"inserted", "features", "error"}
        """
        report = {"inserted": 0, "features": 0, "error": None}
        if new_pt.sqrDist(p1) <= 1e-12 or new_pt.sqrDist(p2) <= 1e-12:
            report["error"] = (u"точка вставки совпадает с вершиной ребра")
            return report
        try:
            rect = QgsRectangle(min(p1.x(), p2.x()), min(p1.y(), p2.y()),
                                max(p1.x(), p2.x()), max(p1.y(), p2.y()))
            rect.grow(eps)
            eps2 = eps * eps
            targets = []
            req = QgsFeatureRequest(rect)
            req.setSubsetOfAttributes([])
            for feat in self.layer.getFeatures(req):
                nr = norm_polys(feat.geometry())
                if not nr:
                    continue
                polys, was_multi = nr
                cnt = insert_vertex_topo(polys, p1, p2, new_pt, eps2)
                if cnt:
                    targets.append({"fid": feat.id(), "polys": polys,
                                    "multi": was_multi})
                    report["inserted"] += cnt
            if not targets:
                report["error"] = u"ребро не найдено в полигонах слоя"
                return report

            self.layer.beginEditCommand(u"Вставка узла на ребро")
            for t in targets:
                g = polys_to_geom(t["polys"], t["multi"])
                if self.layer.changeGeometry(t["fid"], g):
                    report["features"] += 1
            self.layer.endEditCommand()
            self.layer.triggerRepaint()
        except Exception as exc:
            try:
                self.layer.destroyEditCommand()
            except Exception:
                pass
            report["error"] = str(exc)
        return report
