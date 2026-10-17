# -*- coding: utf-8 -*-
"""Пакетный аудит слоя и исправление ошибок (этап 5 ТЗ).

LayerAuditTask — фоновый прогон проверок (QgsTask с прогрессом и
отменой; ТЗ этап 5 п. 1). Проверки из core/checks читают слой в
фоновом потоке (только чтение — допустимо), находки собираются в
self.findings и забираются главным потоком по taskCompleted.

FixEngine — исправления (ТЗ этап 5 п. 3): каждое исправление — это
ChangeSet {fid: QgsGeometry} из payload находки (core/checks/fixes.py),
прогнанный через валидатор этапа 1 (блокирующие нарушения отказывают
всей пачке ДО записи — слой битово цел) и записанный атомарно одной
командой отмены (MultiLayerCommitter этапа 4, транзакционные группы
поддержаны). После записи — общий журнал операций (кнопка «Отменить
правку» и один Ctrl+Z), габарит операции для индекса топологии
(этап 3), перерисовка.

Контроль площади (критерий приёмки ТЗ): суммарная площадь слоя после
пачки не должна измениться больше порога (по умолчанию 0,001 %) —
проверяется ДО записи, при превышении пачка отказывается целиком.
"""

from qgis.core import QgsRectangle, QgsTask

from . import settings
from . import utils
from . import operation_log as oplog
from .multi_editor import MultiLayerCommitter
from .topo_editor import TopoEditor, union_bbox
from .core.checks import (
    run_layer_checks, run_checks, build_ctx,
    SAFE_FIX_CODES, FIX_ORDER, Finding,
    CODE_DUP_NODE, CODE_NEAR_NODE, CODE_T_JUNCTION, CODE_GAP,
    CODE_SPIKE, CODE_OVERLAP, CODE_INVALID,
)
from .core.checks import fixes
from .core.checks.fixes import FixRefused
from .core.validator import validate_operation, blocking


class LayerAuditTask(QgsTask):
    """Аудит слоя в фоне: QgsTask с прогрессом и отменой (ТЗ этап 5).

    :ivar findings: [Finding] — заполняется по завершении run();
    :ivar error: текст сбоя или None (taskTerminated при ошибке).
    """

    def __init__(self, layer, opts=None):
        flags = None
        try:
            flags = QgsTask.CanCancel
        except Exception:
            try:
                flags = utils.enum_member(QgsTask, "TaskFlag", "CanCancel")
            except Exception:
                flags = None
        try:
            if flags is not None:
                super(LayerAuditTask, self).__init__(
                    u"TopoPolyEdit: аудит слоя", flags)
            else:
                super(LayerAuditTask, self).__init__(
                    u"TopoPolyEdit: аудит слоя")
        except TypeError:
            super(LayerAuditTask, self).__init__(u"TopoPolyEdit: аудит слоя")
        self.layer = layer
        self.opts = opts
        self.findings = []
        self.error = None
        self._phase = u""

    def _progress(self, frac, message):
        self._phase = message or self._phase
        try:
            self.setProgress(max(0.0, min(1.0, float(frac))))
        except Exception:
            pass
        try:
            self.setDescription(u"{}: {}".format(
                u"TopoPolyEdit: аудит слоя", self._phase))
        except Exception:
            pass

    def _cancelled(self):
        try:
            return self.isCanceled()
        except Exception:
            return False

    def run(self):
        try:
            self.findings = run_layer_checks(
                self.layer, self.opts, progress=self._progress,
                cancelled=self._cancelled)
            return True
        except Exception as exc:
            self.error = str(exc)
            utils.log(u"Аудит слоя: сбой фоновой задачи — {}".format(exc))
            return False

    def progress_text(self):
        return self._phase


def run_audit_sync(layer, opts=None, progress=None, cancelled=None):
    """Синхронный аудит (тесты, фоновый путь недоступен): [Finding]."""
    return run_layer_checks(layer, opts=opts, progress=progress,
                            cancelled=cancelled)


class FixEngine(object):
    """Исправление ошибок аудита одной команды отмены на пачку."""

    def __init__(self, iface, layer):
        self.iface = iface
        self.layer = layer

    # ------------------------------------------------------------------
    # Публичный API
    # ------------------------------------------------------------------

    def fix_findings(self, findings, command_text=None,
                     overlap_decider=None, makevalid_decider=None):
        """Исправляет пачку находок (одна команда отмены на пачку).

        :param findings: [Finding] — находки этого слоя
        :param command_text: текст команды отмены
        :param overlap_decider: callable(Finding) -> "smaller"|"larger"|None
        :param makevalid_decider: callable(Finding, new_geom, area_before,
                                  area_after) -> bool
        :return: отчёт {attempted, fixed, refused: [(Finding, текст)],
                 error, area_change_pct, violations}
        """
        report = {"attempted": 0, "fixed": 0, "refused": [],
                  "error": None, "area_change_pct": 0.0,
                  "violations": None}
        batch = [f for f in (findings or []) if f is not None]
        report["attempted"] = len(batch)
        if not batch:
            report["error"] = u"нет ошибок для исправления"
            return report
        layer = self.layer
        if not layer.isEditable():
            try:
                if not layer.startEditing():
                    report["error"] = (u"слой «{}» не переведён в режим "
                                       u"правки".format(layer.name()))
                    return report
            except Exception as exc:
                report["error"] = u"не удалось начать правку: {}".format(
                    exc)
                return report
        try:
            geoms = {}
            for f in layer.getFeatures():
                g = f.geometry()
                if g is None or g.isNull() or g.isEmpty():
                    continue
                geoms[f.id()] = g
        except Exception as exc:
            report["error"] = u"слой не прочитан: {}".format(exc)
            return report

        # ChangeSet: последовательное применение payload к актуальным
        # геометриям (порядок FIX_ORDER для безопасных кодов, потом
        # ручные коды — каждая находка по своейpayload-логике)
        order = {code: i for i, code in enumerate(FIX_ORDER)}
        batch_sorted = sorted(
            batch, key=lambda f: (order.get(f.code, 99), f.fid or 0))
        changes = {}
        work = dict(geoms)
        applied = []
        had_crash = False
        for finding in batch_sorted:
            try:
                ch = self._apply_finding(finding, work, overlap_decider,
                                         makevalid_decider)
            except FixRefused as exc:
                # осознанный отказ: находка устарела, пользователь
                # отклонил, щель уже закрыта пачкой — не ошибка
                report["refused"].append((finding, str(exc)))
                continue
            except Exception as exc:
                report["refused"].append(
                    (finding, u"сбой применения: {}".format(exc)))
                had_crash = True
                continue
            for fid, g in ch.items():
                work[fid] = g
                changes[fid] = g
            applied.append(finding)
        if not changes:
            if had_crash:
                report["error"] = (u"ни одно исправление не применимо "
                                   u"(есть сбои применения — см. "
                                   u"отказы)")
            # иначе все отказы осознанные — причина в refused
            return report

        # --- валидация плана ДО записи (слой битово цел при отказе) ---
        old_geoms = {fid: geoms[fid] for fid in changes
                     if fid in geoms}
        missing = [fid for fid in changes if fid not in geoms]
        if missing:
            report["error"] = (u"фичи {} исчезли (данные изменились)"
                               .format(u", ".join(u"#{}".format(f)
                                                  for f in missing)))
            return report
        try:
            neighbors = self._neighbors(old_geoms, changes)
        except RuntimeError as exc:
            report["error"] = str(exc)
            return report
        except Exception as exc:
            report["error"] = u"сбор соседей не удался: {}".format(exc)
            return report
        opts = settings.validator_opts()
        # Слив NEAR_NODE в центроид (ТЗ этап 5) и удаление SPIKE
        # неизбежно меняют границу на малую, ГЕОМЕТРИЧЕСКИ ОГРАНИЧЕННУЮ
        # величину: клин <= сдвиг × длина ребра; игла = треугольник
        # (prev, остриё, next). Порог «шва» для такой пачки ослабляется
        # до этой границы — inherent-клин проходит, настоящая щель
        # по-прежнему блокируется.
        bound = 0.0
        for f in applied:
            try:
                if f.code == CODE_NEAR_NODE:
                    shift = float(f.payload.get("max_shift") or 0.0)
                    edge = float(f.payload.get("max_edge_len") or 0.0)
                    # сумма: клины разных находок лежат в разных местах
                    bound += shift * edge
                elif f.code == CODE_SPIKE:
                    bound += float(f.payload.get("spike_area") or 0.0)
            except Exception:
                continue
        if bound > 0.0:
            try:
                min_area = min(g.area() for g in old_geoms.values()) \
                    if old_geoms else 0.0
                min_area = max(min_area, 1e-30)
                seam_op = 1.01 * bound / min_area
                opts["seam_min_ratio"] = max(
                    float(opts.get("seam_min_ratio",
                                   settings.DEFAULT_SEAM_RATIO)), seam_op)
            except Exception as exc:
                utils.log(u"Аудит: ослабление порога шва — {}".format(
                    exc))
        try:
            violations = validate_operation(old_geoms, changes, neighbors,
                                            **opts)
        except Exception as exc:
            report["error"] = u"сбой валидации плана: {}".format(exc)
            return report
        block = blocking(violations)
        if block:
            report["error"] = (u"валидатор отклонил исправление "
                               u"({} нарушений); слой не изменён"
                               .format(len(block)))
            report["violations"] = [v.to_dict() for v in block]
            return report

        # --- контроль суммарной площади (критерий приёмки ТЗ) ---
        # Порог применяется к пачкам БЕЗОПАСНЫХ кодов: их исправления
        # не должны заметно менять слой. Для ручных кодов (makeValid,
        # обрезка перехлёста — явный выбор пользователя с показом
        # площадей) изменение площади ожидаемо и порогом не гнится;
        # фактическое изменение всё равно попадает в отчёт.
        safe_only = all(f.code in SAFE_FIX_CODES for f in applied)
        try:
            old_total = sum(g.area() for g in geoms.values())
            delta = sum(changes[fid].area() - old_geoms[fid].area()
                        for fid in changes)
            new_total = old_total + delta
            pct = (abs(new_total - old_total) / old_total * 100.0
                   if old_total > 1e-30 else 0.0)
        except Exception:
            old_total = new_total = pct = 0.0
        limit = settings.fix_area_change_pct()
        if safe_only and pct > limit:
            report["error"] = (
                u"суммарная площадь изменилась бы на {:.4g}% — больше "
                u"порога {:.4g}%; слой не изменён".format(pct, limit))
            report["area_change_pct"] = pct
            return report

        # --- атомарная запись (одна команда отмены на пачку) ---
        plan = {"write_order": sorted(changes.keys()), "final": changes}
        committer = MultiLayerCommitter(
            command_text or u"Аудит: исправление ошибок")
        try:
            entries = committer.commit_plans([(layer, plan)])
        except Exception as exc:
            report["error"] = (u"запись отменена, слой не изменён: {}"
                               .format(exc))
            return report

        # журналы: OperationLog (кнопка «Отменить правку»), габарит
        # операции для индекса топологии, слой последней правки
        try:
            oplog.note_operation(
                entries, transaction_group=committer.transaction_group)
        except Exception as exc:
            utils.log(u"Аудит: журнал операций — {}".format(exc))
        try:
            olds = [g for g in old_geoms.values()]
            news = [g for g in changes.values()]
            bbox = union_bbox(olds + news)
            if bbox is not None:
                bbox.grow(settings.edit_match_eps(layer) * 10.0)
                utils.note_operation_bbox(layer, bbox)
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
        report["fixed"] = len(changes)
        report["area_change_pct"] = pct
        return report

    # ------------------------------------------------------------------
    # Применение одного исправления
    # ------------------------------------------------------------------

    def _apply_finding(self, finding, work, overlap_decider,
                       makevalid_decider):
        """ChangeSet одного исправления по payload; FixRefused — отказ."""
        code = finding.code
        payload = finding.payload or {}
        fid = finding.fid
        eps = settings.edit_match_eps(self.layer)
        if code == CODE_DUP_NODE:
            return fixes.clean_dup_nodes(work, fid, eps)
        if code == CODE_SPIKE:
            return fixes.remove_spike_vertex(work, payload.get("pos"),
                                             eps)
        if code == CODE_NEAR_NODE:
            if not payload.get("safe", False):
                raise FixRefused(u"сливание кластера небезопасно")
            return fixes.merge_near_cluster(work,
                                            payload.get("positions"),
                                            payload.get("centroid"), eps)
        if code == CODE_T_JUNCTION:
            return fixes.insert_t_vertex(work, payload.get("pos"),
                                         payload.get("edges") or [], eps)
        if code == CODE_GAP:
            return fixes.glue_gap(work, payload.get("gap_geom"),
                                  payload.get("neighbor_fid"),
                                  changed_fids=set(changes.keys()))
        if code == CODE_OVERLAP:
            choice = None
            if overlap_decider is not None:
                choice = overlap_decider(finding)
            if choice not in ("smaller", "larger"):
                raise FixRefused(u"обрезаемый полигон не выбран")
            return fixes.trim_overlap(work, payload, choice)
        if code == CODE_INVALID:
            if fid not in work:
                raise FixRefused(u"фича #{} не найдена".format(fid))
            g = work[fid]
            new_g = fixes.make_valid_geom(g)
            if makevalid_decider is not None:
                try:
                    area_before = float(g.area())
                    area_after = float(new_g.area())
                except Exception:
                    area_before = area_after = 0.0
                if not makevalid_decider(finding, new_g, area_before,
                                         area_after):
                    raise FixRefused(u"makeValid не подтверждён")
            return {fid: new_g}
        raise FixRefused(u"исправление кода {} не поддерживается"
                         .format(code))

    # ------------------------------------------------------------------
    # Соседи для валидации
    # ------------------------------------------------------------------

    def _neighbors(self, old_geoms, changes):
        """[(fid, geom)] вокруг bbox пачки (кроме изменённых).

        Переиспользует _same_layer_neighbors TopoEditor: индекс
        топологии + лимит соседей (RuntimeError лимита — явный отказ
        пачки, как в этапе 3)."""
        editor = TopoEditor(self.iface, self.layer)
        olds = [g for g in old_geoms.values()]
        news = [g for g in changes.values()]
        bbox = union_bbox(olds + news)
        if bbox is None:
            bbox = QgsRectangle()
        bbox.grow(settings.edit_match_eps(self.layer) * 10.0)
        exclude = set(changes.keys())
        return editor._same_layer_neighbors(bbox, exclude)


__all__ = ["LayerAuditTask", "FixEngine", "run_audit_sync", "Finding",
           "SAFE_FIX_CODES"]
