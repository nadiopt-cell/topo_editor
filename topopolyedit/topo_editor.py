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
    QgsRectangle, QgsWkbTypes, Qgis, QgsVectorLayer, QgsPointXY,
)

from .geo_utils import (
    norm_polys, polys_to_geom, replace_vertices, insert_vertex_topo,
    polys_bbox, points_bbox, contains_vertex, translate_edge_vertices,
    find_t_edges, chain_riders,
    TOPO_MATCH_EPS, TOPO_MATCH_EPS2,
)
from .core.validator import (
    validate_operation, GeosValidity, NoSpikes, Violation, blocking,
    SEV_WARNING,
)
from .snapping_engine import get_snap_layers
from . import topology_index as tindex
from . import settings
from . import utils
from . import operation_log as oplog

MAX_CLIP_REFS = 5000  # предохранитель на число эталонных полигонов
# Лимит соседей того же слоя для проверки перехлёста (этап 3 ТЗ, п. 7:
# прежний жёсткий лимит 500 заменён на MAX_OVERLAP_NEIGHBORS 5000,
# значение читается из настроек при каждой операции — см.
# _same_layer_neighbors; константа оставлена для совместимости/тестов).
MAX_OVERLAP_NEIGHBORS = settings.DEFAULT_MAX_NEIGHBORS

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


def _copy_polys(polys):
    """Глубокая копия нормализованной структуры.

    Снимки индекса топологии (этап 3) разделяются между запросами;
    операции мутируют структуру (insert_vertex_topo вставляет вершину
    на месте) — копия изолирует кэш от правки."""
    return [[[QgsPointXY(v) for v in ring] for ring in part]
            for part in polys]


class _IndexUnserved(Exception):
    """Область не обслуживается индексом топологии (плитка не собрана
    из-за бюджета/слишком широка) — ТИХИЙ переход к прямому запросу
    к слою (не ошибка, в журнал не пишется)."""


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

    def __init__(self, iface, layer, overlap_decider=None,
                 makevalid_decider=None, clip_decider=None):
        """
        :param iface: QgisInterface
        :param layer: редактируемый ПОЛИГОНАЛЬНЫЙ слой (в режиме правки)
        :param overlap_decider: callable(info) -> CHOICE_*; вызывается, когда
            перемещение перекрывает объекты ЭТОГО ЖЕ слоя, а в настройках
            выбран режим «спрашивать». info = {"neighbors": число соседей,
            "moved": число перемещаемых, "area": суммарная площадь,
            "overlaps": [...find_overlaps...]}. Без decider в режиме
            «спрашивать» перехлёст остаётся как есть.
        :param makevalid_decider: callable(info) -> bool; вызывается при
            невалидной геометрии, если в настройках включено «предлагать
            исправить через makeValid» (по умолчанию выключено). info =
            {"fid", "area_before"}. True — применить makeValid и продолжить.
        :param clip_decider: callable(info) -> CLIP_CHOICE_*; вызывается
            при перехлёсте со слоем вне правки, у которого в настройках
            (этап 4, п. 7) выбран режим «спрашивать». info = {"area",
            "count"}. Без decider перехлёст обрезается (как до этапа 4).
        """
        self.iface = iface
        self.layer = layer
        self.overlap_decider = overlap_decider
        self.makevalid_decider = makevalid_decider
        self.clip_decider = clip_decider

    # ------------------------------------------------------------------
    # Индекс топологии (этап 3 ТЗ)
    # ------------------------------------------------------------------

    def _index(self):
        """Индекс топологии слоя или None (выключен настройкой,
        недоступен). При первом обращении задаёт размер плиток по
        видимому экстенту карты (ТЗ этап 3, п. 2)."""
        try:
            idx = tindex.index_for_layer(self.layer)
        except Exception:
            return None
        if idx is not None and self.iface is not None:
            try:
                idx.seed_extent_from_iface(self.iface)
            except Exception:
                pass
        return idx

    def _features_in_rect(self, rect):
        """[(fid, polys, was_multi), ...] — снимки фич под rect.

        Индекс топологии (этап 3): снимки norm_polys из кэша плиток
        (мгновенно после первой сборки); копируются — операции мутируют
        структуру. Fallback — прямой запрос getFeatures(rect), как до
        этапа 3 (индекс выключен / не обслужил / сбой)."""
        idx = self._index()
        if idx is not None:
            try:
                rows = idx.rect_features(rect)
                if rows is not None:
                    return [(fid, _copy_polys(p), m)
                            for fid, p, m in rows]
            except Exception as exc:
                utils.log(u"Индекс: запрос по области не удался — {} "
                          u"(прямой запрос)".format(exc))
        out = []
        req = QgsFeatureRequest(rect)
        req.setSubsetOfAttributes([])
        for feat in self.layer.getFeatures(req):
            nr = norm_polys(feat.geometry())
            if nr:
                out.append((feat.id(), nr[0], nr[1]))
        return out

    # ------------------------------------------------------------------
    # Внутренние помощники
    # ------------------------------------------------------------------

    def _collect_moved(self, old_pt, new_pt, match_eps):
        """Фичи слоя, задетые перемещением узла old_pt -> new_pt.

        Два случая (этап 2 ТЗ — «залатывание»):
          * фича содержит ВЕРШИНУ old_pt — вершина заменяется на new_pt;
          * фича содержит old_pt НА РЕБРЕ без вершины (T-примыкание) —
            в ребро вставляется вершина в точке old_pt (insert_vertex_topo),
            и она же заменяется на new_pt: сосед следует за узлом, щель
            не возникает (приёмка этапа 2: «оба полигона двигаются»).
        Замена выполняется ОДНИМ проходом replace_vertices после всех
        вставок: вставленная вершина совпадает с old_pt и уезжает вместе
        с остальными копиями. Работает для дыр и частей мультиполигонов
        (insert_vertex_topo обходит все кольца).

        match_eps — топологическое совпадение (TOPO_MATCH_EPS), НЕ snap.

        :return: [{"fid", "polys", "multi"}, ...]
        """
        eps2 = match_eps * match_eps
        idx = self._index()
        if idx is not None:
            try:
                fids = idx.features_near_points([old_pt], match_eps)
                if fids is None:
                    # область не обслуживается (плитка не собрана) —
                    # падаем в прямой запрос ниже
                    raise _IndexUnserved()
                entries = []
                for fid in sorted(fids):
                    snap = idx.snapshot_geometry(self.layer, fid)
                    if not snap:
                        continue
                    polys = _copy_polys(snap[0])  # кэш не мутируется
                    was_multi = snap[1]
                    # T-залатывание ДО замены: рёбра, на которых лежит old_pt
                    for a, b in find_t_edges(polys, old_pt, eps2):
                        insert_vertex_topo(polys, a, b, old_pt, eps2)
                    new_polys, changed = replace_vertices(
                        polys, old_pt, new_pt, eps2)
                    if changed:
                        entries.append({"fid": fid, "polys": new_polys,
                                        "multi": was_multi})
                return entries
            except _IndexUnserved:
                pass
            except Exception as exc:
                utils.log(u"Индекс: поиск задетых узлом фич не удался — "
                          u"{} (прямой запрос)".format(exc))
        rect = QgsRectangle(old_pt.x() - match_eps, old_pt.y() - match_eps,
                            old_pt.x() + match_eps, old_pt.y() + match_eps)
        rect.grow(match_eps)
        entries = []
        req = QgsFeatureRequest(rect)
        req.setSubsetOfAttributes([])
        for feat in self.layer.getFeatures(req):
            nr = norm_polys(feat.geometry())
            if not nr:
                continue
            polys, was_multi = nr
            # T-залатывание ДО замены: рёбра, на которых лежит old_pt
            for a, b in find_t_edges(polys, old_pt, eps2):
                insert_vertex_topo(polys, a, b, old_pt, eps2)
            new_polys, changed = replace_vertices(polys, old_pt, new_pt, eps2)
            if changed:
                entries.append({"fid": feat.id(), "polys": new_polys,
                                "multi": was_multi})
        return entries

    def _clip_references(self, bbox, moving_layer_ids=None):
        """Эталонные полигоны ДРУГИХ слоёв для автоотсечения перехлестов.

        Берутся все включенные слои, кроме редактируемого: перехлёст с
        объектами редактируемого слоя обрабатывается отдельно и по запросу
        пользователя (см. _same_layer_neighbors / find_overlaps) — и не
        зависит от того, снят ли этот слой в настройках прилипания.
        Геометрии приводятся к CRS редактируемого слоя. Слои только
        читаются — ничего в них не изменяется.

        Этап 4 ТЗ:
          * слои-участники операции (moving_layer_ids — их геометрии
            двигаются ЭТОЙ ЖЕ операцией) НЕ являются эталонами: обе
            стороны двигаются синхронно, отсечение по их СТАРОМУ
            положению разрушило бы совмещённые границы;
          * per-layer настройка (п. 7): «отсекать» — эталон, как раньше;
            «не трогать» — слой пропускается; «спрашивать» — геометрии
            возвращаются отдельно (ask_refs): при перехлёсте с ними
            спрашивается пользователь (см. _apply_moves);

        :return: (refs, truncated, ask_refs)
        """
        refs = []
        ask_refs = []
        moving = set(moving_layer_ids or ())
        dest = self.layer.crs()
        for lyr in get_snap_layers(self.layer):
            if lyr.id() == self.layer.id():
                continue  # свой слой — отдельный сценарий (по запросу)
            if lyr.id() in moving:
                continue  # участник операции — двигается вместе (этап 4)
            if lyr.geometryType() != QgsWkbTypes.PolygonGeometry:
                continue  # отсекать можно только полигонами
            try:
                mode = settings.clip_mode_for(lyr.id())
            except Exception:
                mode = settings.CLIP_CLIP
            if mode == settings.CLIP_IGNORE:
                continue  # «не трогать»: слой вне отсечения (этап 4, п. 7)
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
                    if mode == settings.CLIP_ASK:
                        ask_refs.append((g, lyr.name()))
                    else:
                        refs.append(g)
                    if len(refs) >= MAX_CLIP_REFS:
                        return refs, True, ask_refs
            except Exception as exc:
                utils.log(u"Слой «{}»: сбой чтения эталонов — {}"
                          .format(lyr.name(), exc))
                continue
        return refs, False, ask_refs

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

        Индекс топологии (этап 3): fids из QgsSpatialIndex плиток +
        геометрии из снимков (мгновенно, без запроса к слою). Fallback —
        прямой запрос getFeatures(bbox), как до этапа 3.

        :param exclude_ids: {fid, ...} — перемещаемые фичи (не соседи)
        :return: [(fid, QgsGeometry)] в CRS слоя (не больше лимита
                 settings.max_overlap_neighbors() — ТЗ этап 3, п. 7:
                 5000 по умолчанию)
        """
        limit = settings.max_overlap_neighbors()
        idx = self._index()
        if idx is not None:
            try:
                mapping = idx.bbox_features(bbox)
                if mapping is not None:
                    out = []
                    for fid in mapping.get(idx.layer_id, ()):
                        if fid in exclude_ids:
                            continue
                        snap = idx.snapshot_geometry(self.layer, fid)
                        if not snap:
                            continue
                        g = polys_to_geom(snap[0], snap[1])
                        if g is None or g.isNull() or g.isEmpty():
                            continue
                        if g.type() != QgsWkbTypes.PolygonGeometry:
                            continue
                        out.append((fid, g))
                        if len(out) > limit:
                            raise RuntimeError(
                                u"Соседей в области больше {} — операция "
                                u"отменена, чтобы не выполнить частичное "
                                u"отсечение; уменьшите перемещаемый "
                                u"участок".format(limit))
                    return out
            except RuntimeError:
                raise  # явный лимит соседей — не деградировать в прямой запрос
            except Exception as exc:
                utils.log(u"Индекс: соседи не получены — {} "
                          u"(прямой запрос)".format(exc))
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
            if len(out) > limit:
                raise RuntimeError(
                    u"Соседей в области больше {} — операция отменена, "
                    u"чтобы не выполнить частичное отсечение; "
                    u"уменьшите перемещаемый участок".format(limit))
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

    def _old_geoms(self, fids):
        """Текущие (ДО записи) геометрии фич по списку fid.

        Для валидатора: старое состояние затронутых полигонов (щели и
        общие узлы сравниваются «до/после»).

        :return: {fid: QgsGeometry} (фичи без геометрии пропущены)
        """
        out = {}
        fids = [f for f in fids if f is not None]
        if not fids:
            return out
        req = QgsFeatureRequest()
        req.setFilterFids(fids)
        req.setSubsetOfAttributes([])
        for feat in self.layer.getFeatures(req):
            g = feat.geometry()
            if g is None or g.isNull() or g.isEmpty():
                continue
            out[feat.id()] = g
        return out

    def _handle_invalid_geometry(self, g, fid):
        """Ремонт makeValid по настройке (этап 1: по умолчанию выключен).

        Невалидная геометрия перемещаемого полигона: по умолчанию —
        блокировка операции (None); при включённой настройке
        «предлагать исправить через makeValid» и согласии пользователя —
        отремонтированная геометрия (форма меняется ОТКРЫТО, с диалогом).

        :return: (QgsGeometry | None, repaired: bool)
        """
        if not settings.offer_makevalid() or self.makevalid_decider is None:
            return None, False
        info = {"fid": fid, "area_before": g.area()}
        try:
            approved = self.makevalid_decider(info)
        except Exception as exc:
            utils.log(u"makeValid: сбой запроса к пользователю — {}"
                      .format(exc))
            return None, False
        if not approved:
            return None, False
        try:
            gv = g.makeValid()
        except Exception as exc:
            utils.log(u"makeValid: сбой — {}".format(exc))
            return None, False
        gv = polygon_only(gv)
        if gv is None or gv.isNull() or gv.isEmpty() or not gv.isGeosValid():
            return None, False
        return gv, True

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
    # Общий конвейер перемещения (этапы 1–2 ТЗ; многослойная запись —
    # этап 4: план и запись разделены, см. _plan_moves / _write_plan)
    # ------------------------------------------------------------------

    def _apply_moves(self, entries, exclude_ids, command_text, eps,
                     t_points=None, moving_layer_ids=None):
        """Полный конвейер одного слоя: план → валидация → запись.

        Однослойный путь (совместимость): _plan_moves + _write_plan.
        Многослойные операции (этап 4) используют планы напрямую:
        все слои сначала дают планы (валидация ДО любых записей),
        затем единый коммиттер пишет их атомарно.

        :return: отчёт (см. _plan_moves)
        """
        report, plan = self._plan_moves(entries, exclude_ids, eps,
                                        t_points=t_points,
                                        moving_layer_ids=moving_layer_ids)
        if plan is None:
            return report
        return self._write_plan(plan, command_text, report)

    def _plan_moves(self, entries, exclude_ids, eps, t_points=None,
                    moving_layer_ids=None):
        """Шаги 1–4 конвейера перемещения: план БЕЗ записи (этапы 1–2 ТЗ;
        разделение ради этапа 4 — атомарная запись нескольких слоёв).

        Общий конвейер операций перемещения (узла и ребра):
          1. новые геометрии: автоотсечение по эталонам ДРУГИХ слоёв
             (как раньше, без вопросов); невалидная геометрия — БЛОКИРОВКА
             с точками нарушений (makeValid из конвейера убран; ремонт —
             только по настройке и с подтверждением);
          2. перехлёст с объектами ЭТОГО ЖЕ слоя: обнаружение и — по
             запросу пользователя — обрезка перемещаемых полигонов,
             обрезка соседей, «оставить» или отмена всей правки;
          3. план: все геометрии к записи (перемещаемые + обрезанные
             соседи) считаются ДО открытия edit-команды;
          3б. T-залатывание по НОВЫМ позициям (этап 2, п. 3): узел,
             прилипший к ребру своего слоя, получает в этом ребре
             вершину — в той же операции;
          4. валидация плана (все правила этапов 1–2 по ВСЕМ затронутым
             полигонам) — при ошибке слой вообще не трогается.

        Запись — отдельный шаг _write_plan (шаг 5); для нескольких
        слоёв коммиттер (этап 4, п. 3) открывает команды на ВСЕХ
        затронутых слоях и уничтожает их ВСЕ при любой неудаче.

        :param entries: [{"fid", "polys", "multi"}, ...] — новые геометрии
        :param exclude_ids: {fid, ...} — перемещаемые фичи (не эталоны)
        :param eps: допуск (единицы CRS слоя) — запас габарита
        :param t_points: [QgsPointXY, ...] новые позиции перемещённых
                         узлов/якорей — для T-залатывания (этап 2) и
                         правила NoTJunction
        :param moving_layer_ids: [layer_id, ...] — слои, чьи геометрии
                         двигаются ЭТОЙ ЖЕ операцией (этап 4): они не
                         являются эталонами отсечения
        :return: (report, plan) — plan None при ошибке/отмене; план
                 содержит {"final", "write_order", "prepared",
                 "final_clipped", "t_fids", "old_geoms", "eps", "bbox"}
        """
        report = {"moved": 0, "clipped": 0, "skipped": 0, "refs": 0,
                  "overlaps": 0, "neighbors_clipped": 0,
                  "overlap_choice": None, "cancelled": False,
                  "t_inserted": 0, "violations": None, "warnings": None,
                  "makevalid_repaired": 0}

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
            return report, None  # пустой список — нечего двигать и отсекать
        # запас поиска эталонов: match_eps слишком мал; берём долю bbox
        w = max(bbox.width(), 0.0)
        h = max(bbox.height(), 0.0)
        pad = max(eps * 10.0, (w + h) * 0.001, 1e-6)
        bbox.grow(pad)

        moving_ids = set(moving_layer_ids or ())
        moving_ids.add(self.layer.id())
        refs, truncated, ask_refs = self._clip_references(
            bbox, moving_layer_ids=moving_ids)
        report["refs"] = len(refs)
        report["ask_refs"] = len(ask_refs)
        if truncated:
            report["error"] = (
                u"Эталонов для отсечения слишком много (>{}) — "
                u"уменьшите экстент или список слоёв".format(MAX_CLIP_REFS))
            return report, None

        # --- 1. новые геометрии (запись ещё не началась) -----------------
        prepared = []  # [(entry, geometry, clipped_count)]
        for e in entries:
            g = polys_to_geom(e["polys"], e["multi"])
            if g.isNull() or g.isEmpty():
                report["error"] = u"Перемещаемый полигон без геометрии"
                return report, None
            if not g.isGeosValid():
                # Этап 1 ТЗ: makeValid из конвейера убран. Невалидная
                # геометрия — блокировка с точкой нарушения; ремонт —
                # только по настройке «предлагать makeValid» и с
                # подтверждением пользователя (по умолчанию выключено).
                gv, repaired = self._handle_invalid_geometry(g, e["fid"])
                if gv is None:
                    violations = GeosValidity().check(e["fid"], g)
                    report["error"] = (
                        u"Полигон #{}: самопересечение или иная невалидная "
                        u"геометрия — операция отменена, слой не изменён"
                        .format(e["fid"]))
                    report["violations"] = [v.to_dict() for v in violations]
                    return report, None
                g = gv
                report["makevalid_repaired"] += 1

            try:
                g, clipped_here = self._clip_against_refs(g, refs)
            except Exception as exc:
                report["error"] = u"Отсечение #{}: сбой — {}".format(
                    e["fid"], exc)
                return report, None

            if g.isNull() or g.isEmpty():
                report["error"] = (
                    u"Отсечение полностью поглотило полигон #{} — "
                    u"операция отменена".format(e["fid"]))
                return report, None
            fitted = self._fit_to_layer(g)
            if fitted is None:
                report["error"] = (
                    u"Полигон #{} состоит из нескольких частей, а слой "
                    u"объявлен с типом «одиночный полигон» — записать "
                    u"результат нельзя. Используйте слой типа "
                    u"MultiPolygon или разберите фичу на части.".format(
                        e["fid"]))
                return report, None
            if not fitted.isGeosValid():
                violations = GeosValidity().check(e["fid"], fitted)
                report["error"] = (
                    u"Отсечение полигона #{} дало невалидную геометрию — "
                    u"операция отменена, слой не изменён".format(e["fid"]))
                report["violations"] = [v.to_dict() for v in violations]
                return report, None
            prepared.append((e, fitted, clipped_here))

        # --- 1б. перехлёст со слоями «спрашивать» (этап 4, п. 7) --------
        # Слои вне правки с per-layer настройкой «спрашивать»: при наличии
        # перехлёста запрашивается решение (clip_decider); без decider —
        # обрезка (поведение до этапа 4). «Не трогать» отсеяно ещё в
        # _clip_references, «отсекать» — обычные эталоны refs.
        if ask_refs and prepared:
            pairs = [(e["fid"], g) for e, g, _c in prepared]
            ask_pairs = [(i, g) for i, (g, _n) in enumerate(ask_refs)]
            try:
                ask_overlaps = find_overlaps(pairs, ask_pairs)
            except Exception as exc:
                ask_overlaps = []
                utils.log(u"Спрашивать-эталоны: сбой проверки — {}"
                          .format(exc))
            if ask_overlaps:
                info = {"area": sum(o["area"] for o in ask_overlaps),
                        "count": len(ask_overlaps),
                        "layer_names": sorted(
                            {n for _g, n in ask_refs})}
                decision = settings.CLIP_CHOICE_CLIP
                if self.clip_decider is not None:
                    try:
                        decision = self.clip_decider(info) \
                            or settings.CLIP_CHOICE_CLIP
                    except Exception as exc:
                        utils.log(u"Спрашивать-эталоны: сбой запроса — {}"
                                  .format(exc))
                        decision = settings.CLIP_CHOICE_CLIP
                if decision not in (settings.CLIP_CHOICE_CLIP,
                                    settings.CLIP_CHOICE_KEEP,
                                    settings.CLIP_CHOICE_CANCEL):
                    decision = settings.CLIP_CHOICE_CLIP
                if decision == settings.CLIP_CHOICE_KEEP:
                    pass  # оставить перехлёст — не отсекаем
                elif decision == settings.CLIP_CHOICE_CANCEL:
                    report["cancelled"] = True
                    return report, None
                else:
                    # «отсекать»: обрезаем перемещаемые по ask-эталонам
                    ask_geoms = [g for g, _n in ask_refs]
                    new_prepared = []
                    for e, g, clipped_here in prepared:
                        try:
                            g2, more = self._clip_against_refs(g, ask_geoms)
                        except Exception as exc:
                            report["error"] = (
                                u"Отсечение по слою «спрашивать» #{}: "
                                u"сбой — {}".format(e["fid"], exc))
                            return report, None
                        if g2.isNull() or g2.isEmpty():
                            report["error"] = (
                                u"Отсечение по слою «спрашивать» полностью "
                                u"поглотило полигон #{} — операция отменена"
                                .format(e["fid"]))
                            return report, None
                        fitted = self._fit_to_layer(g2)
                        if fitted is None:
                            report["error"] = (
                                u"Полигон #{} после отсечения состоит из "
                                u"нескольких частей, а слой объявлен с "
                                u"типом «одиночный полигон»"
                                .format(e["fid"]))
                            return report, None
                        new_prepared.append(
                            (e, fitted, clipped_here + more))
                    prepared = new_prepared

        # --- 2. перехлёст с объектами того же слоя ----------------------
        overlaps = []
        neighbors = []
        prepared_geom = {}  # fid -> подготовленная геометрия перемещаемой
        if prepared and settings.overlap_mode() != settings.OVERLAP_KEEP:
            neighbors = self._same_layer_neighbors(bbox, set(exclude_ids))
            prepared_pairs = [(e["fid"], g) for e, g, _c in prepared]
            prepared_geom = dict(prepared_pairs)
            if neighbors:
                overlaps = find_overlaps(prepared_pairs, neighbors)
            # Перехлёсты МЕЖДУ перемещаемыми (этап 2): T-залатывание может
            # наклонить ребро одного перемещаемого полигона внутрь другого
            # (клин у общего края). До этапа 2 перемещаемые не могли
            # наехлеститься друг на друга. Каждая пара учитывается один раз.
            if len(prepared_pairs) > 1:
                seen_pairs = set()
                for o in find_overlaps(prepared_pairs, prepared_pairs):
                    if o["moved"] == o["neighbor"]:
                        continue
                    key = (min(o["moved"], o["neighbor"]),
                           max(o["moved"], o["neighbor"]))
                    if key not in seen_pairs:
                        seen_pairs.add(key)
                        overlaps.append(o)
        choice = CHOICE_KEEP
        if overlaps:
            report["overlaps"] = len({o["neighbor"] for o in overlaps})
            choice = self._decide_overlap(overlaps)
            report["overlap_choice"] = choice
            if choice == CHOICE_CANCEL:
                report["cancelled"] = True
                return report, None  # до beginEditCommand — слой не тронут

        by_moved = {}     # fid перемещаемого -> [fid соседей-перехлёстов]
        by_neighbor = {}  # fid соседа -> [fid перемещаемых]
        for o in overlaps:
            by_moved.setdefault(o["moved"], []).append(o["neighbor"])
            by_neighbor.setdefault(o["neighbor"], []).append(o["moved"])
        neighbor_geom = dict(neighbors)
        # партнёр по перехлёсту может быть сам перемещаемым (T-клин):
        # геометрия для отсечения — его ПОДГОТОВЛЕННАЯ геометрия
        for fid, g in prepared_geom.items():
            if fid in by_neighbor and fid not in neighbor_geom:
                neighbor_geom[fid] = g

        # --- 3. план: все геометрии к записи (ДО открытия команды) ------
        # План считается до beginEditCommand: валидатор проверяет
        # КОНЕЧНОЕ состояние слоя, при блокировке слой не трогается.
        final = {}          # fid -> QgsGeometry к записи
        final_clipped = {}  # fid -> счётчик отсечений (для отчёта)
        try:
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
                if fitted is None:
                    raise RuntimeError(
                        u"Полигон #{} состоит из нескольких частей, а слой "
                        u"объявлен с типом «одиночный полигон»".format(
                            e["fid"]))
                final[e["fid"]] = fitted
                final_clipped[e["fid"]] = clipped_here
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
                    if fitted is None:
                        raise RuntimeError(
                            u"Сосед #{} состоит из нескольких частей, а "
                            u"слой объявлен с типом «одиночный полигон»"
                            .format(nfid))
                    final[nfid] = fitted
        except RuntimeError as exc:
            report["error"] = str(exc)
            return report, None

        # --- 3б. T-залатывание по НОВЫМ позициям (этап 2 ТЗ, п. 3) ------
        # Узел прилип к ребру СВОЕГО слоя: в это ребро вставляется
        # вершина в той же операции. Выполняется ПОСЛЕ решения о
        # перехлёсте: обрезанные соседи уже в плане и не дублируются.
        t_fids = set()
        if t_points:
            try:
                t_entries = self._t_patch_at_points(t_points,
                                                    set(final.keys()), eps)
            except Exception as exc:
                report["error"] = u"T-залатывание: сбой — {}".format(exc)
                return report, None
            for t in t_entries:
                final[t["fid"]] = polys_to_geom(t["polys"], t["multi"])
                t_fids.add(t["fid"])

        # --- 4. валидация плана: все правила по всем затронутым ---------
        # (этап 1 ТЗ; при ошибке слой не трогается вовсе)
        if not neighbors:
            # KEEP-режим: перехлёстам соседи не понадобились, но щели и
            # общие узлы проверяются против всего окружения
            try:
                neighbors = self._same_layer_neighbors(bbox,
                                                       set(exclude_ids))
            except RuntimeError as exc:
                # Лимит соседей (ТЗ этап 3, п. 7): явное сообщение,
                # операция не выполняется — слой не тронут. Проглатывать
                # нельзя: валидация без соседей пропустила бы потерю
                # общих узлов и новые щели.
                report["error"] = str(exc)
                return report, None
            except Exception:
                neighbors = []  # индекс/слой не ответили — проверка по затронутым
        old_geoms = self._old_geoms(set(final.keys()))
        neighbors_for_validation = [
            (f, g) for f, g in neighbors if f not in final]
        violations = validate_operation(
            old_geoms, final, neighbors_for_validation,
            overlap_allowed=(choice == CHOICE_KEEP and bool(overlaps)),
            moved_points=list(t_points or []),
            **settings.validator_opts())
        errors = blocking(violations)
        if errors:
            report["error"] = u"Операция заблокирована валидатором: " + \
                u"; ".join(v.message for v in errors[:3])
            if len(errors) > 3:
                report["error"] += u" (и ещё {})".format(len(errors) - 3)
            report["violations"] = [v.to_dict() for v in violations]
            return report, None
        warnings = [v for v in violations if v.severity == SEV_WARNING]
        if warnings:
            report["warnings"] = [v.to_dict() for v in warnings]
            utils.log(u"Валидатор: предупреждения — {}".format(
                u"; ".join(v.message for v in warnings[:5])))

        # --- 5. ПЛАН ГОТОВ: запись — отдельный шаг _write_plan ----------
        # (разделение ради этапа 4: многослойная операция сначала
        # собирает планы ВСЕХ слоёв и только потом пишет их атомарно)
        write_order = [e["fid"] for e, _g, _c in prepared]
        write_order += [f for f in sorted(final.keys())
                        if f not in final_clipped]
        plan = {"final": final, "write_order": write_order,
                "final_clipped": final_clipped, "t_fids": t_fids,
                "old_geoms": old_geoms, "eps": eps, "bbox": bbox}
        return report, plan

    def _write_plan(self, plan, command_text, report):
        """Шаг 5: запись плана одной командой отмены (Ctrl+Z).

        Однослойный путь. Многослойные операции (этап 4) пишут планы
        нескольких слоёв через MultiLayerCommitter (multi_editor.py) —
        командные блоки там согласованы с транзакционными группами.

        :param plan: план из _plan_moves
        :param command_text: название edit-команды (в истории отмены)
        :param report: отчёт _plan_moves (дополняется счётчиками)
        :return: report
        """
        final = plan["final"]
        final_clipped = plan["final_clipped"]
        t_fids = plan["t_fids"]
        old_geoms = plan["old_geoms"]
        eps = plan["eps"]
        command_started = False
        try:
            self.layer.beginEditCommand(command_text)
            command_started = True
            for fid in plan["write_order"]:
                if not self.layer.changeGeometry(fid, final[fid]):
                    raise RuntimeError(
                        u"Не удалось записать геометрию #{} (changeGeometry)"
                        .format(fid))
                if fid in final_clipped:
                    report["moved"] += 1
                    report["clipped"] += final_clipped[fid]
                elif fid in t_fids:
                    report["t_inserted"] += 1  # T-залатывание (этап 2)
                else:
                    report["neighbors_clipped"] += 1
            self.layer.endEditCommand()
            command_started = False
            # слой запоминается для кнопки «Отменить» (штатная отмена QGIS
            # привязана к активному слою и может быть недоступна)
            utils.note_edited_layer(self.layer)
            # журнал операций плагина (этап 4, п. 4): однослойная операция
            # регистрируется так же, как многослойные, — кнопка «Отменить»
            # работает по одному и тому же журналу
            try:
                oplog.note_operation(
                    [(self.layer.id(), command_text,
                      self.layer.undoStack().index())],
                    transaction_group=False)
            except Exception:
                pass
            # журнал габарита операции для afterRollBack индекса (этап 3,
            # п. 3): НОВЫЕ и СТАРЫЕ положения затронутых фич + запас
            try:
                old_g = [g for g in old_geoms.values()
                         if g is not None and not g.isNull()]
                new_g = [g for g in final.values()
                         if g is not None and not g.isNull()]
                op_bbox = union_bbox(new_g + old_g)
                if op_bbox is not None:
                    op_bbox.grow(eps * 10.0)
                    utils.note_operation_bbox(self.layer, op_bbox)
            except Exception:
                pass
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
        :return: {"moved", "clipped", "skipped", "refs", "t_inserted",
                  "warnings", "error"}
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
                u"Топологическое перемещение узла", match_eps,
                t_points=[QgsPointXY(new_pt)]))
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
        """Фичи слоя, задетые переносом цепочки якорей на вектор delta.

        Три случая (этап 2 ТЗ):
          * фича содержит якорную вершину — якоря и коллинеарные
            попутчики сдвигаются (translate_edge_vertices);
          * фича содержит якорь НА СВОЁМ ребре без вершины (T-конец
            цепочки, п. 1–2) — в ребро вставляется вершина в точке
            якоря; она совпадает с якорем и уезжает вместе с цепочкой;
          * фича содержит вершину-пассажира НА цепочке (чужая T-вершина
            на перемещаемом ребре, п. 6) — она сдвигается на delta
            (при параллельном переносе проекция на новое ребро — это
            и есть сдвиг на delta) и остаётся на новом ребре.
        Полигон, содержащий только часть якорей (примыкание «только
        концом»), смещается этими якорями — топология сохраняется.

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
        for fid, polys, was_multi in self._features_in_rect(rect):
            # 1) T-залатывание ДО переноса: якорь на ребре фичи без узла
            for anchor in anchors:
                for a, b in find_t_edges(polys, anchor, eps2):
                    insert_vertex_topo(polys, a, b, anchor, eps2)
            # 2) якоря + коллинеарные попутчики общей границы
            new_polys, changed = translate_edge_vertices(
                polys, anchors, delta, eps2)
            # 3) вершины-пассажиры: чужие T-вершины на цепочке
            for old_r in chain_riders(polys, anchors, eps2):
                new_polys, ch2 = replace_vertices(
                    new_polys, old_r,
                    QgsPointXY(old_r.x() + dx, old_r.y() + dy), eps2)
                changed += ch2
            if changed:
                entries.append({"fid": fid, "polys": new_polys,
                                "multi": was_multi})
        return entries

    def _t_patch_at_points(self, pts, exclude_ids,
                           match_eps=TOPO_MATCH_EPS):
        """Вставка вершин в рёбра СВОЕГО слоя под точками pts (п. 3).

        Новая позиция перемещённого узла легла на ребро соседа без
        вершины (прилипание к ребру своего слоя): в это ребро вставляется
        вершина В ТОЙ ЖЕ операции — иначе узел остаётся T-примыканием
        без общего узла (И4). Фичи из exclude_ids (уже в плане) не
        дублируются: их геометрия перезаписывается целиком, а узел на
        ребре САМОЙ перемещаемой фичи — самокасание, его отсекает
        валидатор (GeosValidity).

        :param pts: [QgsPointXY, ...] — новые позиции узлов/якорей
        :param exclude_ids: {fid, ...} — фичи, уже вошедшие в план
        :param match_eps: допуск топологического совпадения (не snap)
        :return: [{"fid", "polys", "multi"}, ...] — патчи к плану
        """
        eps2 = match_eps * match_eps
        idx = self._index()
        if idx is not None:
            try:
                fids = idx.features_near_points(pts, match_eps)
                if fids is None:
                    # область не обслуживается — прямой запрос ниже
                    raise _IndexUnserved()
                entries = []
                for fid in sorted(fids):
                    if fid in exclude_ids:
                        continue
                    snap = idx.snapshot_geometry(self.layer, fid)
                    if not snap:
                        continue
                    polys = _copy_polys(snap[0])  # кэш не мутируется
                    was_multi = snap[1]
                    changed = 0
                    for p in pts:
                        for a, b in find_t_edges(polys, p, eps2):
                            changed += insert_vertex_topo(polys, a, b,
                                                          p, eps2)
                    if changed:
                        entries.append({"fid": fid, "polys": polys,
                                        "multi": was_multi})
                return entries
            except _IndexUnserved:
                pass
            except Exception as exc:
                utils.log(u"Индекс: T-залатывание не удалось — {} "
                          u"(прямой запрос)".format(exc))
        rect = points_bbox(pts)
        if rect is None:
            return []
        rect.grow(match_eps * 2.0)
        entries = []
        req = QgsFeatureRequest(rect)
        req.setSubsetOfAttributes([])
        for feat in self.layer.getFeatures(req):
            if feat.id() in exclude_ids:
                continue
            nr = norm_polys(feat.geometry())
            if not nr:
                continue
            polys, was_multi = nr
            changed = 0
            for pt in pts:
                for a, b in find_t_edges(polys, pt, eps2):
                    changed += insert_vertex_topo(polys, a, b, pt, eps2)
            if changed:
                entries.append({"fid": feat.id(), "polys": polys,
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
        :return: {"moved", "clipped", "skipped", "refs", "t_inserted",
                  "warnings", "error"}
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
                u"Топологическое перемещение ребра", match_eps,
                t_points=[QgsPointXY(a.x() + dx, a.y() + dy)
                          for a in anchors]))
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
        :return: {"inserted", "features", "violations", "error"}
        """
        report = {"inserted": 0, "features": 0, "violations": None,
                  "error": None}
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
            for fid, polys, was_multi in self._features_in_rect(rect):
                cnt = insert_vertex_topo(polys, p1, p2, new_pt, eps2)
                if cnt:
                    targets.append({"fid": fid, "polys": polys,
                                    "multi": was_multi})
                    report["inserted"] += cnt
            if not targets:
                report["error"] = u"ребро не найдено в полигонах слоя"
                return report

            # валидация результата (этап 1): геометрия с новым узлом
            # обязана оставаться валидной (правила по одной геометрии;
            # вставка коллинеарного узла не создаёт щелей и перехлёстов)
            new_geoms = {t["fid"]: polys_to_geom(t["polys"], t["multi"])
                         for t in targets}
            violations = validate_operation(
                {}, new_geoms, [], rules=(GeosValidity(), NoSpikes()),
                **settings.validator_opts())
            errors = blocking(violations)
            if errors:
                report["error"] = (
                    u"Вставка заблокирована валидатором: " +
                    u"; ".join(v.message for v in errors[:3]))
                report["violations"] = [v.to_dict() for v in violations]
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
            # журнал габарита для afterRollBack индекса (этап 3, п. 3)
            try:
                utils.note_operation_bbox(self.layer, rect)
            except Exception:
                pass
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
