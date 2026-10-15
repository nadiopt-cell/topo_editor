# -*- coding: utf-8 -*-
"""Применение топологических изменений к РЕДАКТИРУЕМОМУ слою.

Ключевой принцип: изменяется только редактируемый слой. Геометрии прочих
слоёв (и прочих фич того же слоя) используются исключительно как эталоны:

  * прилипание — см. snapping_engine.SnappingEngine;
  * автоотсечение перехлестов — QgsGeometry.difference от эталонных
    полигонов включенных слоёв, кроме перемещаемых фич;
  * перехлёст с другими объектами ТОГО ЖЕ слоя обрабатывается ПО ЗАПРОСУ
    пользователя (настройка «спрашивать / всегда обрезать / не обрезать»):
    можно обрезать перемещаемые полигоны, обрезать соседей, оставить
    перехлёст или отменить правку. Не зависит от настроек прилипания.

Все изменения одной операции выполняются в едином edit-командном блоке
(beginEditCommand / endEditCommand), поэтому Ctrl+Z откатывает их разом.
"""

from qgis.core import (
    QgsProject, QgsFeatureRequest, QgsCoordinateTransform, QgsGeometry,
    QgsRectangle, QgsWkbTypes, Qgis, QgsVectorLayer,
)

from .geo_utils import (
    norm_polys, polys_to_geom, replace_vertices, insert_vertex_topo,
    polys_bbox, points_bbox, contains_vertex, translate_edge_vertices,
    TOPO_MATCH_EPS, TOPO_MATCH_EPS2,
)
from .snapping_engine import get_snap_layers
from . import settings
from . import utils

MAX_CLIP_REFS = 5000  # предохранитель на число эталонных полигонов
MAX_OVERLAP_NEIGHBORS = 500  # соседей того же слоя для проверки перехлёста

# Перехлёст меньше этой доли площади меньшего из двух полигонов считается
# погрешностью (шов по общей границе, шум float) и не обрабатывается.
OVERLAP_MIN_RATIO = 1e-8

# Выбор пользователя при перехлёсте с объектами того же слоя
CHOICE_CLIP_MOVED = "clip_moved"          # обрезать перемещаемые по соседям
CHOICE_CLIP_NEIGHBORS = "clip_neighbors"  # обрезать соседей по перемещаемым
CHOICE_KEEP = "keep"                      # оставить перехлёст
CHOICE_CANCEL = "cancel"                  # отменить всю правку
_CHOICES = (CHOICE_CLIP_MOVED, CHOICE_CLIP_NEIGHBORS, CHOICE_KEEP,
            CHOICE_CANCEL)


def polygon_only(g):
    """Полигональная часть результата геометрической операции.

    intersection()/difference() иногда возвращают GeometryCollection
    (полигон + линия касания); такой результат НЕ является полигоном по
    QgsGeometry.type(), и его нельзя ни записать, ни измерить как
    полигон. Здесь из коллекции берутся только полигоны.

    :return: QgsGeometry-полигон/мультиполигон или None, если полигональной
             части нет (пусто, касание по линии/точке)
    """
    if g is None or g.isNull() or g.isEmpty():
        return None
    if g.type() == QgsWkbTypes.PolygonGeometry:
        return g
    try:
        parts = [p for p in g.asGeometryCollection()
                 if p.type() == QgsWkbTypes.PolygonGeometry
                 and not p.isEmpty()]
    except Exception:
        return None
    if not parts:
        return None
    if len(parts) == 1:
        return parts[0]
    try:
        u = QgsGeometry.unaryUnion(parts)
    except Exception:
        return None
    if u is None or u.isNull() or u.isEmpty():
        return None
    return u


def union_bbox(geoms):
    """Общий габарит списка QgsGeometry (QgsRectangle) или None."""
    bbox = None
    for g in geoms:
        r = QgsRectangle(g.boundingBox())
        if bbox is None:
            bbox = r
        else:
            bbox.combineExtentWith(r)  # изменяет на месте, возвращает None
    return bbox


def find_overlaps(prepared, neighbors, min_ratio=OVERLAP_MIN_RATIO):
    """Перехлёсты новых геометрий перемещаемых полигонов с соседями.

    :param prepared: [(fid, QgsGeometry)] — новые геометрии (валидные)
    :param neighbors: [(fid, QgsGeometry)] — прочие объекты того же слоя
    :param min_ratio: порог: перехлёст должен быть больше min_ratio * площадь
                      меньшего из пары полигонов
    :return: [{"moved": fid, "neighbor": fid, "area": float,
               "geom": QgsGeometry (полигон перехлёста)}]
    """
    out = []
    for mfid, mg in prepared:
        if mg is None or mg.isNull() or mg.isEmpty():
            continue
        mb = mg.boundingBox()
        m_area = mg.area()
        if m_area <= 0.0:
            continue
        for nfid, ng in neighbors:
            if not mb.intersects(ng.boundingBox()):
                continue
            try:
                if not mg.intersects(ng):
                    continue
                inter = polygon_only(mg.intersection(ng))
            except Exception as exc:
                utils.log(u"Перехлёст: сбой intersection #{}/#{} — {}"
                          .format(mfid, nfid, exc))
                continue
            if inter is None:
                continue
            area = inter.area()
            if area > min_ratio * min(m_area, ng.area()):
                out.append({"moved": mfid, "neighbor": nfid,
                            "area": area, "geom": inter})
    return out


class TopoEditor(object):
    """Исполнитель топологических операций над редактируемым слоем."""

    def __init__(self, iface, layer, overlap_decider=None):
        """
        :param iface: QgisInterface
        :param layer: редактируемый ПОЛИГОНАЛЬНЫЙ слой (в режиме правки)
        :param overlap_decider: callable(info) -> CHOICE_*; вызывается, когда
            перемещение перекрывает объекты ЭТОГО ЖЕ слоя, а в настройках
            выбран режим «спрашивать». info = {"neighbors": число соседей,
            "moved": число перемещаемых, "area": суммарная площадь,
            "overlaps": [...find_overlaps...]}. Без decider в режиме
            «спрашивать» перехлёст остаётся как есть.
        """
        self.iface = iface
        self.layer = layer
        self.overlap_decider = overlap_decider

    # ------------------------------------------------------------------
    # Внутренние помощники
    # ------------------------------------------------------------------

    def _collect_moved(self, old_pt, new_pt, match_eps):
        """Фичи слоя, содержащие вершину old_pt, с подставленной new_pt.

        match_eps — топологическое совпадение (TOPO_MATCH_EPS), НЕ snap.

        :return: [{"fid", "polys", "multi"}, ...]
        """
        rect = QgsRectangle(old_pt.x() - match_eps, old_pt.y() - match_eps,
                            old_pt.x() + match_eps, old_pt.y() + match_eps)
        rect.grow(match_eps)
        eps2 = match_eps * match_eps
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

    def _clip_references(self, bbox):
        """Эталонные полигоны ДРУГИХ слоёв для автоотсечения перехлестов.

        Берутся все включенные слои, кроме редактируемого: перехлёст с
        объектами редактируемого слоя обрабатывается отдельно и по запросу
        пользователя (см. _same_layer_neighbors / find_overlaps) — и не
        зависит от того, снят ли этот слой в настройках прилипания.
        Геометрии приводятся к CRS редактируемого слоя. Слои только
        читаются — ничего в них не изменяется.
        """
        refs = []
        dest = self.layer.crs()
        for lyr in get_snap_layers(self.layer):
            if lyr.id() == self.layer.id():
                continue  # свой слой — отдельный сценарий (по запросу)
            if lyr.geometryType() != QgsWkbTypes.PolygonGeometry:
                continue  # отсекать можно только полигонами
            same = lyr.crs() == dest
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
                    g = feat.geometry()
                    if g is None or g.isNull() or g.isEmpty():
                        continue
                    if ct is not None:
                        try:
                            g = QgsGeometry(g)
                            if g.transform(ct) != 0:
                                continue
                        except Exception as exc:
                            utils.log(u"Эталон «{}» #{}: сбой преобразования "
                                      u"CRS — пропущен ({})"
                                      .format(lyr.name(), feat.id(), exc))
                            continue
                    refs.append(g)
                    if len(refs) >= MAX_CLIP_REFS:
                        return refs, True
            except Exception as exc:
                utils.log(u"Слой «{}»: сбой чтения эталонов — {}"
                          .format(lyr.name(), exc))
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
            except Exception as exc:
                raise RuntimeError(
                    u"Отсечение: сбой difference — {}".format(exc)) from exc
            if g2 is None or g2.isNull() or g2.isEmpty():
                g = QgsGeometry()  # полигон полностью поглощён
                break
            g3 = polygon_only(g2)
            if g3 is None:
                try:
                    g3 = polygon_only(g2.makeValid())
                except Exception as exc:
                    utils.log(u"Отсечение: сбой makeValid — {}".format(exc))
            if g3 is None or not g3.isGeosValid():
                raise RuntimeError(
                    u"Отсечение: результат не является корректным полигоном")
            g = g3
            clipped += 1
        return g, clipped

    def _push(self, text, level=Qgis.Info, duration=4):
        self.iface.messageBar().pushMessage(
            u"Топологическое редактирование", text,
            level=level, duration=duration)

    # ------------------------------------------------------------------
    # Перехлёст с объектами ТОГО ЖЕ слоя (по запросу пользователя)
    # ------------------------------------------------------------------

    def _same_layer_neighbors(self, bbox, exclude_ids):
        """Прочие полигоны редактируемого слоя в габарите bbox.

        :param exclude_ids: {fid, ...} — перемещаемые фичи (не соседи)
        :return: [(fid, QgsGeometry)] в CRS слоя (не больше
                 MAX_OVERLAP_NEIGHBORS)
        """
        out = []
        req = QgsFeatureRequest(bbox)
        req.setSubsetOfAttributes([])
        for feat in self.layer.getFeatures(req):
            if feat.id() in exclude_ids:
                continue
            g = feat.geometry()
            if g is None or g.isNull() or g.isEmpty():
                continue
            if g.type() != QgsWkbTypes.PolygonGeometry:
                continue
            out.append((feat.id(), g))
            if len(out) > MAX_OVERLAP_NEIGHBORS:
                raise RuntimeError(
                    u"Соседей в области больше {} — операция отменена, "
                    u"чтобы не выполнить частичное отсечение; "
                    u"уменьшите перемещаемый участок".format(
                        MAX_OVERLAP_NEIGHBORS))
        return out

    def preview_overlaps(self, prepared, exclude_ids):
        """Перехлёсты для превью при перетаскивании (ничего не пишет).

        :param prepared: [(fid, QgsGeometry)] — новые геометрии (CRS слоя)
        :param exclude_ids: {fid, ...} — перемещаемые фичи
        :return: результат find_overlaps()
        """
        valid = []
        for fid, g in prepared:
            if g is None or g.isNull() or g.isEmpty():
                continue
            if not g.isGeosValid():
                # при перетаскивании контур легко становится самопересекающимся
                try:
                    g = polygon_only(g.makeValid())
                except Exception:
                    g = None
                if g is None:
                    continue
            valid.append((fid, g))
        prepared = valid
        bbox = union_bbox([g for _fid, g in prepared])
        if bbox is None:
            return []
        w, h = max(bbox.width(), 0.0), max(bbox.height(), 0.0)
        bbox.grow(max((w + h) * 0.001, 1e-9))
        neighbors = self._same_layer_neighbors(bbox, set(exclude_ids))
        return find_overlaps(prepared, neighbors)

    def _decide_overlap(self, overlaps):
        """Что делать с перехлёстом: настройка либо вопрос пользователю.

        :return: CHOICE_* (при сбое диалога — CHOICE_CANCEL: правку не
                 применяем «вслепую»)
        """
        mode = settings.overlap_mode()
        if mode == settings.OVERLAP_CLIP:
            return CHOICE_CLIP_MOVED
        if mode == settings.OVERLAP_KEEP:
            return CHOICE_KEEP
        if self.overlap_decider is None:
            return CHOICE_KEEP
        info = {
            "neighbors": len({o["neighbor"] for o in overlaps}),
            "moved": len({o["moved"] for o in overlaps}),
            "area": sum(o["area"] for o in overlaps),
            "overlaps": overlaps,
        }
        try:
            choice = self.overlap_decider(info)
        except Exception as exc:
            utils.log(u"Перехлёст: сбой запроса к пользователю — {}"
                      .format(exc))
            return CHOICE_CANCEL
        return choice if choice in _CHOICES else CHOICE_CANCEL

    def _fit_to_layer(self, g):
        """Геометрия в виде, допустимом для слоя (одиночная/мульти).

        Нужна для соседей, которых мы режем: разрез может превратить
        полигон в мультиполигон, а слой с одиночными полигонами его не
        примет. Если без потери частей привести нельзя — None.
        """
        try:
            multi_layer = QgsWkbTypes.isMultiType(self.layer.wkbType())
        except Exception:
            return g
        if g.isMultipart() == multi_layer:
            return g
        if multi_layer:
            try:
                g.convertToMultiType()
            except Exception:
                pass
            return g
        parts = [p for p in g.asMultiPolygon() if p]
        if len(parts) == 1:
            return QgsGeometry.fromPolygonXY(parts[0])
        return None

    # ------------------------------------------------------------------
    # Общий конвейер перемещения
    # ------------------------------------------------------------------

    def _apply_moves(self, entries, exclude_ids, command_text, eps):
        """Единый edit-командный блок: запись новых геометрий + отсечение.

        Общий конвейер операций перемещения (узла и ребра):
          1. новые геометрии: makeValid для невалидных колец, автоотсечение
             по эталонам ДРУГИХ слоёв (как раньше, без вопросов);
          2. перехлёст с объектами ЭТОГО ЖЕ слоя: обнаружение и — по
             запросу пользователя — обрезка перемещаемых полигонов, обрезка
             соседей, «оставить» или отмена всей правки (до записи);
          3. запись: changeGeometry в одной команде отмены (Ctrl+Z).

        :param entries: [{"fid", "polys", "multi"}, ...] — новые геометрии
        :param exclude_ids: {fid, ...} — перемещаемые фичи (не эталоны)
        :param command_text: название edit-команды (видно в истории отмены)
        :param eps: допуск (единицы CRS слоя) — запас габарита
        :return: {"moved", "clipped", "skipped", "refs", "overlaps",
                  "neighbors_clipped", "overlap_choice", "cancelled"}
        """
        report = {"moved": 0, "clipped": 0, "skipped": 0, "refs": 0,
                  "overlaps": 0, "neighbors_clipped": 0,
                  "overlap_choice": None, "cancelled": False}

        # габарит всех новых геометрий + запас
        # (combineExtentWith меняет bbox на месте и возвращает None —
        # возвращаемое значение использовать нельзя)
        bbox = None
        for e in entries:
            r = polys_bbox(e["polys"])
            if bbox is None:
                bbox = r
            else:
                bbox.combineExtentWith(r)
        if bbox is None:
            return report  # пустой список — нечего двигать и отсекать
        # запас поиска эталонов: match_eps слишком мал; берём долю bbox
        w = max(bbox.width(), 0.0)
        h = max(bbox.height(), 0.0)
        pad = max(eps * 10.0, (w + h) * 0.001, 1e-6)
        bbox.grow(pad)

        refs, truncated = self._clip_references(bbox)
        report["refs"] = len(refs)
        if truncated:
            report["error"] = (
                u"Эталонов для отсечения слишком много (>{}) — "
                u"уменьшите экстент или список слоёв".format(MAX_CLIP_REFS))
            return report

        # --- 1. новые геометрии (запись ещё не началась) -----------------
        prepared = []  # [(entry, geometry, clipped_count)]
        for e in entries:
            g = polys_to_geom(e["polys"], e["multi"])
            if g.isNull() or g.isEmpty():
                report["error"] = u"Перемещаемый полигон без геометрии"
                return report
            if not g.isGeosValid():
                try:
                    gv = g.makeValid()
                    if not gv.isNull() and not gv.isEmpty() \
                            and gv.type() == QgsWkbTypes.PolygonGeometry:
                        g = gv
                except Exception as exc:
                    report["error"] = u"Некорректная геометрия #{}: {}".format(
                        e["fid"], exc)
                    return report
                if not g.isGeosValid():
                    report["error"] = u"Некорректная геометрия #{}".format(
                        e["fid"])
                    return report

            try:
                g, clipped_here = self._clip_against_refs(g, refs)
            except Exception as exc:
                report["error"] = u"Отсечение #{}: сбой — {}".format(
                    e["fid"], exc)
                return report

            if g.isNull() or g.isEmpty():
                report["error"] = (
                    u"Отсечение полностью поглотило полигон #{} — "
                    u"операция отменена".format(e["fid"]))
                return report
            fitted = self._fit_to_layer(g)
            if fitted is None:
                report["error"] = (
                    u"Отсечение полигона #{} дало мультиполигон, слой не "
                    u"допускает мультигеометрию — операция отменена".format(
                        e["fid"]))
                return report
            prepared.append((e, fitted, clipped_here))

        # --- 2. перехлёст с объектами того же слоя ----------------------
        overlaps = []
        neighbors = []
        if prepared and settings.overlap_mode() != settings.OVERLAP_KEEP:
            neighbors = self._same_layer_neighbors(bbox, set(exclude_ids))
            if neighbors:
                overlaps = find_overlaps(
                    [(e["fid"], g) for e, g, _c in prepared], neighbors)
        choice = CHOICE_KEEP
        if overlaps:
            report["overlaps"] = len({o["neighbor"] for o in overlaps})
            choice = self._decide_overlap(overlaps)
            report["overlap_choice"] = choice
            if choice == CHOICE_CANCEL:
                report["cancelled"] = True
                return report  # до beginEditCommand — слой не тронут

        by_moved = {}     # fid перемещаемого -> [fid соседей-перехлёстов]
        by_neighbor = {}  # fid соседа -> [fid перемещаемых]
        for o in overlaps:
            by_moved.setdefault(o["moved"], []).append(o["neighbor"])
            by_neighbor.setdefault(o["neighbor"], []).append(o["moved"])
        neighbor_geom = dict(neighbors)

        # Вся правка одной командой: частичная запись или оставшийся
        # после сбоя перехлёст недопустимы — при ошибке откатывается всё.
        command_started = False
        try:
            self.layer.beginEditCommand(command_text)
            command_started = True
            final = {}
            for e, g, clipped_here in prepared:
                if choice == CHOICE_CLIP_MOVED and e["fid"] in by_moved:
                    g, more = self._clip_against_refs(
                        g, [neighbor_geom[n] for n in by_moved[e["fid"]]])
                    clipped_here += more
                    if g.isNull() or g.isEmpty():
                        raise RuntimeError(
                            u"Отсечение полностью поглотило полигон #{}".format(
                                e["fid"]))
                fitted = self._fit_to_layer(g)
                if fitted is None or not fitted.isGeosValid():
                    raise RuntimeError(
                        u"Неподдерживаемая или некорректная геометрия "
                        u"полигона #{}".format(e["fid"]))
                if not self.layer.changeGeometry(e["fid"], fitted):
                    raise RuntimeError(
                        u"Не удалось записать геометрию #{} (changeGeometry)"
                        .format(e["fid"]))
                report["moved"] += 1
                report["clipped"] += clipped_here
                final[e["fid"]] = fitted

            if choice == CHOICE_CLIP_NEIGHBORS:
                for nfid, mfids in by_neighbor.items():
                    ng = neighbor_geom[nfid]
                    cut = False
                    for mfid in mfids:
                        if mfid not in final:
                            continue
                        cut = True
                        ng = polygon_only(ng.difference(final[mfid]))
                        if ng is None:
                            raise RuntimeError(
                                u"Отсечение полностью поглотило соседа #{}"
                                .format(nfid))
                    if not cut:
                        continue
                    fitted = self._fit_to_layer(ng)
                    if fitted is None or not fitted.isGeosValid():
                        raise RuntimeError(
                            u"Неподдерживаемая или некорректная геометрия "
                            u"соседа #{}".format(nfid))
                    if not self.layer.changeGeometry(nfid, fitted):
                        raise RuntimeError(
                            u"Не удалось записать геометрию соседа #{} "
                            u"(changeGeometry)".format(nfid))
                    report["neighbors_clipped"] += 1
            self.layer.endEditCommand()
            command_started = False
            # слой запоминается для кнопки «Отменить» (штатная отмена QGIS
            # привязана к активному слою и может быть недоступна)
            utils.note_edited_layer(self.layer)
        except Exception as exc:
            if command_started:
                self.layer.destroyEditCommand()
            report.update({"moved": 0, "clipped": 0,
                           "neighbors_clipped": 0,
                           "error": str(exc)})
            return report
        self.layer.triggerRepaint()
        return report

    # ------------------------------------------------------------------
    # Операция 1: топологическое перемещение узла + автоотсечение
    # ------------------------------------------------------------------

    def move_vertex(self, old_pt, new_pt, match_eps=TOPO_MATCH_EPS):
        """Перемещает общий узел во всех полигонах редактируемого слоя
        и отсекает перехлесты, возникшие при подтягивании.

        :param old_pt: исходное положение узла (CRS слоя)
        :param new_pt: новое положение узла (CRS слоя), уже с прилипанием
        :param match_eps: допуск топологического совпадения узла
                          (TOPO_MATCH_EPS; НЕ snap-допуск)
        :return: {"moved", "clipped", "skipped", "refs", "error"}
        """
        report = {"moved": 0, "clipped": 0, "skipped": 0, "refs": 0,
                  "error": None}
        if new_pt.sqrDist(old_pt) <= TOPO_MATCH_EPS2:
            report["error"] = u"узел не смещён"
            return report
        try:
            entries = self._collect_moved(old_pt, new_pt, match_eps)
            if not entries:
                report["error"] = u"узел не найден в полигонах слоя"
                return report
            report.update(self._apply_moves(
                entries, {e["fid"] for e in entries},
                u"Топологическое перемещение узла", match_eps))
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

    def _collect_edge_moved(self, anchors, delta, match_eps=TOPO_MATCH_EPS):
        """Фичи слоя, содержащие хотя бы одну якорную вершину цепочки,
        с якорями, сдвинутыми на вектор delta.

        Полигон, содержащий только часть якорей (примыкание "только
        концом", как полигон C на вершине общего ребра A и B),
        смещается этими якорями — топология сохраняется.

        :param anchors: [QgsPointXY, ...] — исходные вершины цепочки
        :param delta: (dx, dy) — вектор переноса (CRS слоя)
        :param match_eps: допуск топологического совпадения (не snap)
        :return: [{"fid", "polys", "multi"}, ...]
        """
        dx, dy = delta
        pad = match_eps + max(abs(dx), abs(dy))
        rect = points_bbox(anchors)
        if rect is None:
            return []  # пустые якоря — искать нечего
        rect.grow(pad)
        eps2 = match_eps * match_eps
        entries = []
        req = QgsFeatureRequest(rect)
        req.setSubsetOfAttributes([])
        for feat in self.layer.getFeatures(req):
            nr = norm_polys(feat.geometry())
            if not nr:
                continue
            polys, was_multi = nr
            new_polys, changed = translate_edge_vertices(
                polys, anchors, delta, eps2)
            if changed:
                entries.append({"fid": feat.id(), "polys": new_polys,
                                "multi": was_multi})
        return entries

    def move_edge(self, anchors, delta, match_eps=TOPO_MATCH_EPS):
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
        :param match_eps: допуск топологического совпадения (не snap)
        :return: {"moved", "clipped", "skipped", "refs", "error"}
        """
        report = {"moved": 0, "clipped": 0, "skipped": 0, "refs": 0,
                  "error": None}
        if not anchors or len(anchors) < 2:
            report["error"] = u"ребро не задано"
            return report
        dx, dy = delta
        if dx * dx + dy * dy <= TOPO_MATCH_EPS2:
            report["error"] = u"ребро не смещено"
            return report
        if anchors[0].sqrDist(anchors[-1]) <= TOPO_MATCH_EPS2:
            report["error"] = u"ребро вырождено (короче допуска)"
            return report
        try:
            entries = self._collect_edge_moved(anchors, (dx, dy), match_eps)
            if not entries:
                report["error"] = u"ребро не найдено в полигонах слоя"
                return report
            report.update(self._apply_moves(
                entries, {e["fid"] for e in entries},
                u"Топологическое перемещение ребра", match_eps))
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

    def add_vertex_on_edge(self, p1, p2, new_pt, match_eps=TOPO_MATCH_EPS):
        """Вставляет узел на ребро (p1, p2) во все полигоны редактируемого
        слоя, содержащие это ребро (включая T-образные примыкания).

        :param p1, p2: концы ребра, по которому выполнен клик (CRS слоя)
        :param new_pt: точка вставки на ребре (CRS слоя)
        :param match_eps: допуск топологического совпадения рёбер (не snap)
        :return: {"inserted", "features", "error"}
        """
        report = {"inserted": 0, "features": 0, "error": None}
        if (new_pt.sqrDist(p1) <= TOPO_MATCH_EPS2 or
                new_pt.sqrDist(p2) <= TOPO_MATCH_EPS2):
            report["error"] = (u"точка вставки совпадает с вершиной ребра")
            return report
        try:
            rect = QgsRectangle(min(p1.x(), p2.x()), min(p1.y(), p2.y()),
                                max(p1.x(), p2.x()), max(p1.y(), p2.y()))
            # поиск фич — небольшой запас вокруг сегмента
            w = abs(p1.x() - p2.x())
            h = abs(p1.y() - p2.y())
            rect.grow(max(match_eps * 10.0, (w + h) * 0.001, 1e-6))
            eps2 = match_eps * match_eps
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
                if not self.layer.changeGeometry(t["fid"], g):
                    raise RuntimeError(
                        u"Не удалось записать геометрию #{} (changeGeometry)"
                        .format(t["fid"]))
                report["features"] += 1
            self.layer.endEditCommand()
            utils.note_edited_layer(self.layer)  # для кнопки «Отменить»
            self.layer.triggerRepaint()
        except Exception as exc:
            try:
                self.layer.destroyEditCommand()
            except Exception:
                pass
            report["inserted"] = 0
            report["features"] = 0
            report["error"] = str(exc)
        return report
