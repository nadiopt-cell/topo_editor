# -*- coding: utf-8 -*-
"""Правка нескольких слоёв одной операцией (этап 4 ТЗ).

Принцип ТЗ: «меняются слои в режиме правки, разрешённые в настройках».
Слои без режима правки по-прежнему не трогаются.

Ключевые части:
  * MultiLayerCommitter — атомарная запись плана в несколько слоёв
    (п. 3): beginEditCommand на каждом затронутом слое (или ОДНА команда
    на транзакционную группу QGIS — п. 5); если запись в любой слой не
    удалась — destroyEditCommand на ВСЕХ: частичной записи нет ни в
    одном слое;
  * MultiLayerEditor — координатор операции (п. 1–2, 4): собирает
    участников (в правке + не в чёрном списке + узел/ребро под точкой),
    переводит точки в СК каждого слоя, собирает планы через ядро
    TopoEditor._plan_moves (валидация ДО любых записей), пишет единым
    коммиттером и регистрирует операцию в OperationLog.

Точки передаются в CRS ПЕРВИЧНОГО слоя (как раньше) или в CRS карты
(ct_m2l/ct_l2m из инструмента); для каждого участника координатор
считает СВОИ координаты обратным преобразованием от ЕДИНОЙ точки карты
— дрейф не накапливается (п. 2).
"""

from qgis.core import (
    QgsProject, QgsCoordinateTransform, QgsPointXY, QgsWkbTypes,
    QgsVectorLayer,
)

from .geo_utils import TOPO_MATCH_EPS
from . import settings
from . import utils
from . import operation_log as oplog
from .topo_editor import TopoEditor, union_bbox


class MultiLayerCommitter(object):
    """Атомарная запись одной операции в НЕСКОЛЬКО слоёв (этап 4, п. 3).

    Обычный случай: beginEditCommand открывается на КАЖДОМ затронутом
    слое; endEditCommand — только если запись во ВСЕ слои удалась.
    Сбой любой changeGeometry (или исключение в плане записи) —
    destroyEditCommand на ВСЕХ открытых слоях: ни один слой не меняется.

    Транзакционная группа QGIS (п. 5): если ВСЕ затронутые слои в одной
    группе (обе таблицы одного GeoPackage с режимом транзакций), QGIS
    зеркалирует команды на все слои группы — открывается ОДНА команда
    на первом слое группы, запись геометрий идёт во все слои. Кастомная
    OperationLog и стеки слоёв не дают двойную отмену: в журнале
    операция помечена transaction_group=True, откат — один undo().

    Худший случай (endEditCommand одного слоя упал после завершения
    команды другого) — неполный коммит; вероятность исчезающе мала,
    состояние фиксируется исключением (отчёт покажет сбой записи).
    """

    def __init__(self, command_text):
        self.command_text = command_text
        self._open = []  # слои с открытой командой (порядок открытия)
        self.transaction_group = False

    def commit_plans(self, plans):
        """Пишет планы во все слои атомарно.

        :param plans: [(layer, plan), ...] — plan из TopoEditor._plan_moves
        :return: [(layer_id, index_after)] — индексы стеков отмены сразу
                 после коммита (для OperationLog)
        :raises RuntimeError: при любой неудаче записи (все команды
                 уничтожены, слои битово целы)
        """
        layers = [layer for layer, _plan in plans]
        self.transaction_group = oplog.same_transaction_group(layers)
        try:
            if self.transaction_group:
                # одна команда на группу (этап 4, п. 5): QGIS зеркалирует
                # begin/end на все слои группы, undo группового стека один
                layers[0].beginEditCommand(self.command_text)
                self._open.append(layers[0])
                for layer, plan in plans:
                    self._write_layer(layer, plan)
                layers[0].endEditCommand()
                self._open.remove(layers[0])
            else:
                for layer, _plan in plans:
                    layer.beginEditCommand(self.command_text)
                    self._open.append(layer)
                for layer, plan in plans:
                    self._write_layer(layer, plan)
                for layer in list(self._open):
                    layer.endEditCommand()
                    self._open.remove(layer)
        except Exception:
            self.destroy()
            raise
        entries = []
        for layer, _plan in plans:
            try:
                index_after = layer.undoStack().index()
            except Exception:
                index_after = 0
            entries.append((layer.id(), self.command_text, index_after))
        return entries

    def _write_layer(self, layer, plan):
        """changeGeometry по всем fid плана; любой сбой — RuntimeError."""
        for fid in plan["write_order"]:
            ok = False
            try:
                ok = layer.changeGeometry(fid, plan["final"][fid])
            except Exception as exc:
                raise RuntimeError(
                    u"Слой «{}»: сбой записи геометрии #{} — {}".format(
                        layer.name(), fid, exc)) from exc
            if not ok:
                raise RuntimeError(
                    u"Не удалось записать геометрию #{} слоя «{}» "
                    u"(changeGeometry)".format(fid, layer.name()))

    def destroy(self):
        """Уничтожает ВСЕ открытые команды (слои битово целы)."""
        for layer in list(self._open):
            try:
                layer.destroyEditCommand()
            except Exception:
                pass
        self._open = []


def candidate_edit_layers(primary_layer):
    """Кандидаты в участники операции (этап 4, п. 1(а)(б)).

    Режим настройки edit_scope:
      * EDIT_SCOPE_ALL (по умолчанию) — первичный слой + ВСЕ прочие
        полигональные слои в режиме правки, не в чёрном списке правки;
      * EDIT_SCOPE_ACTIVE — только первичный слой (поведение до этапа 4).

    Критерий (в) «имеют узел в той же точке или ребро, проходящее
    через неё» проверяется позже — по непустоте собранного плана
    (_collect_moved: вершина или T-ребро под точкой).

    :return: [QgsVectorLayer, ...] (первичный слой первым)
    """
    if primary_layer is None:
        return []
    out = [primary_layer]
    if settings.edit_scope() == settings.EDIT_SCOPE_ACTIVE:
        return out
    try:
        disabled = settings.edit_disabled_ids()
    except Exception:
        disabled = set()
    for lyr in QgsProject.instance().mapLayers().values():
        if lyr is primary_layer or lyr.id() == primary_layer.id():
            continue
        if not isinstance(lyr, QgsVectorLayer):
            continue
        try:
            if not lyr.isValid() or not lyr.isEditable():
                continue
            if lyr.geometryType() != QgsWkbTypes.PolygonGeometry:
                continue
            if lyr.id() in disabled:
                continue  # чёрный список правки (п. 1(б))
        except Exception:
            continue
        out.append(lyr)
    return out


def _map_crs(iface, primary_layer):
    """CRS карты (для единой точки операции).

    Из холста, если доступен; иначе — CRS первичного слоя (офлайн-
    тесты без канвы: преобразования становятся identity).
    """
    try:
        if iface is not None:
            return iface.mapCanvas().mapSettings().destinationCrs()
    except Exception:
        pass
    try:
        return primary_layer.crs()
    except Exception:
        return None


class _Transforms(object):
    """Преобразования «единая точка карты <-> CRS слоя» одного участника.

    identity, если CRS слоя совпадает с CRS карты (избегаем лишнего
    reprojection и сохраняем битовые координаты для одинарных СК).
    """

    def __init__(self, map_crs, layer):
        self.identity = False
        self._m2l = None
        self._l2m = None
        try:
            if map_crs is not None and layer.crs() == map_crs:
                self.identity = True
                return
            self._m2l = QgsCoordinateTransform(
                map_crs, layer.crs(), QgsProject.instance())
            self._l2m = QgsCoordinateTransform(
                layer.crs(), map_crs, QgsProject.instance())
        except Exception:
            # преобразование недоступно: слой не может участвовать
            # в единой точке — помечаем identity (точки пойдут как есть,
            # совпадение проверит _collect_moved)
            self.identity = True
            self._m2l = None
            self._l2m = None

    def to_layer(self, map_pt):
        if self.identity or map_pt is None:
            return map_pt
        return self._m2l.transform(QgsPointXY(map_pt))

    def to_map(self, layer_pt):
        if self.identity or layer_pt is None:
            return layer_pt
        return self._l2m.transform(QgsPointXY(layer_pt))

    def map_delta_to_layer(self, delta, ref_map_pt):
        """Вектор переноса в СК слоя: разность преобразованных точек.

        ref_map_pt — опорная точка (первый якорь); для линейных СК
        вектор одинаков во всех точках, для проективных — берём в опорной.
        """
        if self.identity or ref_map_pt is None:
            return delta
        a = self.to_layer(ref_map_pt)
        b = self.to_layer(QgsPointXY(ref_map_pt.x() + delta[0],
                                     ref_map_pt.y() + delta[1]))
        return (b.x() - a.x(), b.y() - a.y())


class MultiLayerEditor(object):
    """Координатор многослойных топологических операций (этап 4 ТЗ).

    Использует ядро TopoEditor для каждого участника: план собирается
    и валидируется ДО любых записей; затем единый MultiLayerCommitter
    пишет все планы атомарно. Слои-участники не являются эталонами
    отсечения друг друга (двигаются синхронно).
    """

    def __init__(self, iface, primary_layer, overlap_decider=None,
                 makevalid_decider=None, clip_decider=None):
        self.iface = iface
        self.primary = primary_layer
        self.overlap_decider = overlap_decider
        self.makevalid_decider = makevalid_decider
        self.clip_decider = clip_decider

    # ------------------------------------------------------------------
    # Участники и преобразования
    # ------------------------------------------------------------------

    def _editor_for(self, layer):
        return TopoEditor(self.iface, layer,
                          overlap_decider=self.overlap_decider,
                          makevalid_decider=self.makevalid_decider,
                          clip_decider=self.clip_decider)

    def _participants(self, old_map_pt, new_map_pt, collect):
        """Собирает участников с непустыми планами.

        :param old_map_pt, new_map_pt: единые точки в CRS карты
        :param collect: callable(editor, tf, eps) -> entries | None
                        (сбор фич слоя под операцию в его CRS)
        :return: (participants, report) — participants: список словарей
                 {"layer", "editor", "tf", "entries", "new_pt"(для edge:
                 ещё "new_anchors")}; report — накопленный отчёт ошибок
        """
        report = {"moved": 0, "clipped": 0, "skipped": 0, "refs": 0,
                  "overlaps": 0, "neighbors_clipped": 0,
                  "overlap_choice": None, "cancelled": False,
                  "t_inserted": 0, "violations": None, "warnings": None,
                  "makevalid_repaired": 0, "error": None,
                  "participants": [], "skipped_layers": []}
        map_crs = _map_crs(self.iface, self.primary)
        participants = []
        for lyr in candidate_edit_layers(self.primary):
            tf = _Transforms(map_crs, lyr)
            try:
                old_pt = tf.to_layer(old_map_pt)
                eps = settings.edit_match_eps(lyr)
            except Exception as exc:
                report["skipped_layers"].append(lyr.name())
                utils.log(u"Этап 4: слой «{}» пропущен (СК) — {}"
                          .format(lyr.name(), exc))
                continue
            editor = self._editor_for(lyr)
            try:
                entries = collect(editor, tf, eps, old_pt)
            except Exception as exc:
                report["skipped_layers"].append(lyr.name())
                utils.log(u"Этап 4: слой «{}» пропущен (сбор) — {}"
                          .format(lyr.name(), exc))
                continue
            if not entries:
                continue  # нет узла/ребра под точкой (критерий (в))
            participants.append({"layer": lyr, "editor": editor, "tf": tf,
                                 "entries": entries})
        return participants, report

    # ------------------------------------------------------------------
    # Общий механизм: планы -> атомарный коммит
    # ------------------------------------------------------------------

    def _run_multi(self, participants, report, command_text, collect_plan):
        """Планы всех участников -> атомарная запись -> OperationLog.

        :param participants: список от _participants
        :param collect_plan: callable(editor, part) -> (entries, t_points)
        :return: report (дополнен счётчиками; error при любой неудаче —
                 ВСЕ слои не изменены)
        """
        moving_ids = {part["layer"].id() for part in participants}
        plans = []
        for part in participants:
            entries, t_points = collect_plan(part["editor"], part)
            part["t_points"] = t_points
            try:
                rep, plan = part["editor"]._plan_moves(
                    entries, {e["fid"] for e in entries}, part["eps"],
                    t_points=t_points, moving_layer_ids=moving_ids)
            except Exception as exc:
                report["error"] = (u"Слой «{}»: сбой планирования — {}"
                                   .format(part["layer"].name(), exc))
                return report
            if plan is None:
                # валидация/перехлёст/план не прошли — вся операция
                # отменяется ДО любых записей (слои не рассинхронизируются)
                rep_layer = part["layer"].name()
                if rep.get("error"):
                    report["error"] = (u"Слой «{}»: {}".format(
                        rep_layer, rep["error"]))
                if rep.get("cancelled"):
                    report["cancelled"] = True
                report["violations"] = rep.get("violations")
                return report
            if rep.get("warnings"):
                report.setdefault("all_warnings", []).extend(
                    rep.get("warnings"))
                report["warnings"] = rep.get("warnings")
            plans.append((part["layer"], part["editor"], plan))

        committer = MultiLayerCommitter(command_text)
        try:
            entries = committer.commit_plans(
                [(layer, plan) for layer, _ed, plan in plans])
        except Exception as exc:
            report["error"] = (u"Запись отменена, все слои не изменены: {}"
                               .format(exc))
            return report
        # единая СК участников (для отображения точек нарушений в UI)
        try:
            report["crs_uniform"] = (
                len({part["layer"].crs().authid()
                     for part in participants}) == 1)
        except Exception:
            report["crs_uniform"] = False

        # счётчики отчёта по планам (шаг записи уже прошёл успешно)
        for layer, editor, plan in plans:
            fc = plan["final_clipped"]
            t_fids = plan["t_fids"]
            for fid in plan["write_order"]:
                if fid in fc:
                    report["moved"] += 1
                    report["clipped"] += fc[fid]
                elif fid in t_fids:
                    report["t_inserted"] += 1
                else:
                    report["neighbors_clipped"] += 1
            # журнал габарита операции для afterRollBack индекса (этап 3)
            try:
                old_g = [g for g in plan["old_geoms"].values()
                         if g is not None and not g.isNull()]
                new_g = [g for g in plan["final"].values()
                         if g is not None and not g.isNull()]
                op_bbox = union_bbox(new_g + old_g)
                if op_bbox is not None:
                    op_bbox.grow(plan["eps"] * 10.0)
                    utils.note_operation_bbox(layer, op_bbox)
            except Exception:
                pass
            try:
                utils.note_edited_layer(layer)
            except Exception:
                pass
            try:
                layer.triggerRepaint()
            except Exception:
                pass

        # OperationLog (этап 4, п. 4–5): запись ПОСЛЕ успешного коммита
        try:
            oplog.note_operation(entries,
                                 transaction_group=committer.transaction_group)
        except Exception as exc:
            utils.log(u"Журнал операций: сбой регистрации — {}".format(exc))
        report["participants"] = [part["layer"].name()
                                  for part in participants]
        return report

    # ------------------------------------------------------------------
    # Операция 1: перемещение узла во всех участниках
    # ------------------------------------------------------------------

    def move_vertex(self, old_pt, new_pt, match_eps=TOPO_MATCH_EPS,
                    ct_m2l=None, ct_l2m=None):
        """Перемещает узел во ВСЕХ слоях-участниках (этап 4 ТЗ).

        :param old_pt: исходное положение узла (CRS первичного слоя)
        :param new_pt: новое положение (CRS первичного слоя, с прилипанием)
        :param match_eps: допуск топологического совпадения (CRS первичного)
        :param ct_m2l: карта -> CRS первичного слоя (из инструмента);
                       None — точки уже в CRS карты
        :param ct_l2m: CRS первичного слоя -> карта
        :return: отчёт {moved, clipped, t_inserted, neighbors_clipped,
                 warnings, error, cancelled, participants, ...}
        """
        report = {"moved": 0, "clipped": 0, "skipped": 0, "refs": 0,
                  "error": None}
        try:
            if new_pt.sqrDist(old_pt) <= TOPO_MATCH_EPS * TOPO_MATCH_EPS:
                report["error"] = u"узел не смещён"
                return report
        except Exception:
            pass
        old_map = ct_l2m.transform(QgsPointXY(old_pt)) if ct_l2m is not None \
            else QgsPointXY(old_pt)
        new_map = ct_l2m.transform(QgsPointXY(new_pt)) if ct_l2m is not None \
            else QgsPointXY(new_pt)

        def collect(editor, tf, eps, old_layer_pt):
            new_layer_pt = tf.to_layer(new_map)
            return editor._collect_moved(old_layer_pt, new_layer_pt, eps)

        participants, m_report = self._participants(
            old_map, new_map, collect)
        if not participants:
            m_report["error"] = report["error"] or \
                u"узел не найден в полигонах слоёв"
            return m_report
        for part in participants:
            part["eps"] = settings.edit_match_eps(part["layer"])

        def collect_plan(editor, part):
            return (part["entries"], [part["tf"].to_layer(new_map)])

        m_report = self._run_multi(participants, m_report,
                                   u"Топологическое перемещение узла",
                                   collect_plan)
        report.update(m_report)
        return report

    # ------------------------------------------------------------------
    # Операция 1б: перемещение ребра во всех участниках
    # ------------------------------------------------------------------

    def move_edge(self, anchors, delta, match_eps=TOPO_MATCH_EPS,
                  ct_m2l=None, ct_l2m=None):
        """Перемещает ребро (цепочку якорей) во ВСЕХ слоях-участниках.

        :param anchors: вершины цепочки (CRS первичного слоя)
        :param delta: (dx, dy) вектор переноса (CRS первичного слоя)
        :return: отчёт (как move_vertex)
        """
        report = {"moved": 0, "clipped": 0, "skipped": 0, "refs": 0,
                  "error": None}
        dx, dy = delta
        if not anchors or len(anchors) < 2:
            report["error"] = u"ребро не задано"
            return report
        if dx * dx + dy * dy <= TOPO_MATCH_EPS * TOPO_MATCH_EPS:
            report["error"] = u"ребро не смещено"
            return report
        if anchors[0].sqrDist(anchors[-1]) <= TOPO_MATCH_EPS * TOPO_MATCH_EPS:
            report["error"] = u"ребро вырождено (короче допуска)"
            return report

        def to_map(p):
            return ct_l2m.transform(QgsPointXY(p)) if ct_l2m is not None \
                else QgsPointXY(p)

        anchors_map = [to_map(a) for a in anchors]
        delta_map = None
        if ct_l2m is not None:
            # вектор переноса в СК карты: разность преобразованных точек
            a0 = ct_l2m.transform(QgsPointXY(anchors[0]))
            a1 = ct_l2m.transform(QgsPointXY(anchors[0].x() + dx,
                                              anchors[0].y() + dy))
            delta_map = (a1.x() - a0.x(), a1.y() - a0.y())
        else:
            delta_map = (dx, dy)

        def new_anchors_map():
            return [QgsPointXY(a.x() + delta_map[0], a.y() + delta_map[1])
                    for a in anchors_map]

        def collect(editor, tf, eps, old_first):
            # якоря в CRS слоя; слой участвует, если содержит хотя бы
            # один якорь (вершиной или ребром)
            anchors_layer = [tf.to_layer(a) for a in anchors_map]
            delta_layer = tf.map_delta_to_layer(delta_map, anchors_map[0])
            entries = editor._collect_edge_moved(anchors_layer, delta_layer,
                                                 eps)
            return entries

        participants, m_report = self._participants(
            anchors_map[0], new_anchors_map()[0], collect)
        if not participants:
            m_report["error"] = report["error"] or \
                u"ребро не найдено в полигонах слоёв"
            return m_report
        for part in participants:
            part["eps"] = settings.edit_match_eps(part["layer"])
            part["delta_layer"] = part["tf"].map_delta_to_layer(
                delta_map, anchors_map[0])
            part["anchors_layer"] = [part["tf"].to_layer(a)
                                     for a in anchors_map]

        def collect_plan(editor, part):
            return (part["entries"],
                    [QgsPointXY(a.x() + part["delta_layer"][0],
                                a.y() + part["delta_layer"][1])
                     for a in part["anchors_layer"]])

        m_report = self._run_multi(participants, m_report,
                                   u"Топологическое перемещение ребра",
                                   collect_plan)
        report.update(m_report)
        return report
