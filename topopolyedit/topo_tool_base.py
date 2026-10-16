# -*- coding: utf-8 -*-
"""Общая база инструментов карты TopoPolyEdit (BaseTopoTool).

Выделяет общий код трёх QgsMapTool-инструментов (v1.4.0 — рефакторинг
без изменения поведения):

  * движок прилипания SnappingEngine;
  * маркер точки прилипания (фиолетовый круг) и подсветка ребра
    прилипания (фиолетовая линия);
  * подсветка вершин слоёв прилипания: бирюзовые квадраты — вершины
    эталонов рядом с курсором, фиолетовые кружки — все вершины объекта,
    к которому идёт прилипание (данные — SnappingEngine.hints);
  * красная область перехлёста перемещаемых полигонов с объектами ТОГО ЖЕ
    слоя и диалог «что делать с перехлёстом» (запрос пользователю);
  * оранжевые резиновые ленты перемещаемых полигонов;
  * активация (крест-курсор) и деактивация (сброс состояния + снятие
    галочки кнопки инструмента);
  * Esc: отмена перетаскивания (если оно идёт) либо выход в панорамирование;
  * единое сообщение об ошибке операции и вывод отчёта в message bar.

Подклассы:
  * создают СВОИ маркеры/ленты в __init__ ПОСЛЕ super().__init__();
  * сбрасывают их в переопределении _cleanup_extra() — базовый
    _cleanup() вызовет этот хук после общего сброса;
  * сообщения «нет узла/ребра» и текст отчёта — в подклассах (тексты
    различаются).
"""

import time

from qgis.PyQt.QtCore import Qt, QTimer
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import QMessageBox, QPushButton
from qgis.gui import QgsMapTool, QgsVertexMarker, QgsRubberBand
from qgis.core import (QgsPointXY, QgsGeometry, QgsWkbTypes, Qgis,
                       QgsRectangle)

from .snapping_engine import SnappingEngine
from .topo_editor import (TopoEditor, CHOICE_CLIP_MOVED,
                          CHOICE_CLIP_NEIGHBORS, CHOICE_KEEP, CHOICE_CANCEL,
                          union_bbox)
from .core.validator import (
    GeosValidity, NoSpikes, NoNewGaps, blocking,
    CODE_NEW_GAP,
)
from . import settings
from . import utils

# Не чаще раза в столько секунд пересчитывается красная область перехлёста
# и маркеры нарушений при перетаскивании (пересечения геометрий дороже
# привязки).
OVERLAP_PREVIEW_INTERVAL = 0.08

# Превью щелей при перетаскивании считается только при небольшом числе
# соседей в области: объединение сотен полигонов каждые 0,08 с подвиснет.
# На тяжёлых слоях щели проверяются при отпускании кнопки (полная
# валидация операции).
GAP_PREVIEW_MAX_NEIGHBORS = 64


class BaseTopoTool(QgsMapTool):
    """Базовый класс инструментов карты плагина."""

    TITLE = u"Топологическое редактирование"

    def __init__(self, iface, action):
        super(BaseTopoTool, self).__init__(iface.mapCanvas())
        self.iface = iface
        self.action = action
        self.engine = SnappingEngine(iface.mapCanvas())
        self.drag = None
        self.rubbers = []
        self._rubber_pool = []

        # маркер точки прилипания (фиолетовый круг)
        self.snap_marker = QgsVertexMarker(self.canvas())
        self.snap_marker.setIconType(QgsVertexMarker.ICON_CIRCLE)
        self.snap_marker.setColor(QColor(200, 80, 230))
        self.snap_marker.setPenWidth(2)
        self.snap_marker.hide()

        # подсветка ребра прилипания (фиолетовая линия)
        self.snap_seg = QgsRubberBand(self.canvas(), QgsWkbTypes.LineGeometry)
        self.snap_seg.setStrokeColor(QColor(200, 80, 230))
        self.snap_seg.setWidth(2)

        # вершины-кандидаты слоёв прилипания рядом с курсором
        # (бирюзовые полые квадраты) и вершины объекта-цели
        # (фиолетовые кружки)
        self.hint_near = self._make_point_band(
            QColor(0, 170, 190), 7, "ICON_BOX", fill_alpha=0)
        self.hint_target = self._make_point_band(
            QColor(200, 80, 230), 9, "ICON_CIRCLE", fill_alpha=110)

        # область перехлёста с объектами редактируемого слоя (красная)
        self.overlap_rb = QgsRubberBand(self.canvas(),
                                        QgsWkbTypes.PolygonGeometry)
        self.overlap_rb.setStrokeColor(QColor(220, 30, 30))
        self.overlap_rb.setFillColor(QColor(220, 30, 30, 110))
        self.overlap_rb.setWidth(2)
        self._overlap_ts = 0.0

        # валидатор (этап 1): красные маркеры точек самопересечений и
        # красная заливка НОВЫХ щелей при перетаскивании
        self.violation_markers = []
        self.gap_rb = QgsRubberBand(self.canvas(),
                                    QgsWkbTypes.PolygonGeometry)
        self.gap_rb.setStrokeColor(QColor(220, 30, 30))
        self.gap_rb.setFillColor(QColor(255, 60, 60, 130))
        self.gap_rb.setWidth(2)
        self._violation_ts = 0.0
        self._forbidden_cursor = False

    # ------------------------------------------------------------------
    # Хук подклассов
    # ------------------------------------------------------------------

    def _cleanup_extra(self):
        """Сброс собственных маркеров/лент подкласса.

        Вызывается из базового _cleanup() после общего сброса. Подкласс
        НЕ должен сбрасывать здесь self.rubbers — ими ведёт база.
        """
        pass

    # ------------------------------------------------------------------
    # Общие служебные методы
    # ------------------------------------------------------------------

    def _make_point_band(self, color, size, icon_name, fill_alpha=0):
        """Резиновая лента точек: значок в каждой вершине мультиточки."""
        rb = QgsRubberBand(self.canvas(), QgsWkbTypes.PointGeometry)
        rb.setStrokeColor(color)
        rb.setFillColor(QColor(color.red(), color.green(), color.blue(),
                               fill_alpha))
        rb.setIcon(utils.enum_member(QgsRubberBand, "IconType", icon_name))
        rb.setIconSize(size)
        rb.setWidth(2)
        return rb

    def _set_points(self, rb, pts):
        """Показать список точек (CRS карты) на ленте или очистить её."""
        try:
            if pts:
                rb.setToGeometry(QgsGeometry.fromMultiPointXY(
                    [QgsPointXY(p) for p in pts]), None)
            else:
                rb.reset(QgsWkbTypes.PointGeometry)
        except Exception as exc:
            utils.log(u"Подсветка вершин: сбой отрисовки — {}".format(exc))

    def _show_hints(self):
        """Подсветка вершин слоёв прилипания по данным движка."""
        hints = self.engine.hints
        self._set_points(self.hint_near, hints.get("near"))
        self._set_points(self.hint_target, hints.get("target"))

    def _reset_hints(self):
        """Сброс подсветки вершин (и подсказок движка)."""
        self.engine.reset_hints()
        for rb in (self.hint_near, self.hint_target):
            try:
                rb.reset(QgsWkbTypes.PointGeometry)
            except Exception:
                pass

    def _reset_overlap(self):
        """Сброс красной области перехлёста."""
        try:
            self.overlap_rb.reset(QgsWkbTypes.PolygonGeometry)
        except Exception:
            pass

    def _make_polygon_rubber(self):
        """Лента перемещаемого полигона — переиспользуется из пула."""
        index = len(self.rubbers)
        if index < len(self._rubber_pool):
            rb = self._rubber_pool[index]
        else:
            rb = QgsRubberBand(self.canvas(), QgsWkbTypes.PolygonGeometry)
            rb.setStrokeColor(QColor(230, 120, 0))
            rb.setFillColor(QColor(255, 170, 0, 90))
            rb.setWidth(2)
            self._rubber_pool.append(rb)
        self.rubbers.append(rb)
        return rb

    def _reset_poly_rubbers(self):
        """Сброс активных лент (пул сохраняется для переиспользования)."""
        for rb in self.rubbers:
            try:
                rb.reset(QgsWkbTypes.PolygonGeometry)
            except Exception:
                pass
        self.rubbers = []

    def _reset_snap_seg(self):
        """Сброс подсветки ребра прилипания."""
        try:
            self.snap_seg.reset(QgsWkbTypes.LineGeometry)
        except Exception:
            pass

    def _cleanup(self):
        """Полный сброс состояния перетаскивания и визуализации."""
        self.drag = None
        self._reset_poly_rubbers()
        self.snap_marker.hide()
        self._reset_snap_seg()
        self._reset_hints()
        self._reset_overlap()
        self._reset_violations()
        self._cleanup_extra()

    def _push_error(self, exc):
        """Единое сообщение об ошибке операции (message bar)."""
        self.iface.messageBar().pushMessage(
            self.TITLE, u"Ошибка: {}".format(exc),
            level=Qgis.Critical, duration=5)

    def _show_snap_feedback(self, snap):
        """Маркер найденной привязки; для ребра — линия сегмента.

        Вершины слоёв прилипания показываются и тогда, когда привязки нет
        (чтобы было видно, куда целиться).
        """
        self._show_hints()
        if snap is None:
            self.snap_marker.hide()
            self._reset_snap_seg()
            return
        self.snap_marker.setCenter(snap.point)
        self.snap_marker.show()
        if snap.snap_type == "edge" and snap.segment is not None:
            a, b = snap.segment
            self.snap_seg.setToGeometry(
                QgsGeometry.fromPolylineXY([QgsPointXY(a), QgsPointXY(b)]),
                None)
        else:
            self._reset_snap_seg()

    # ------------------------------------------------------------------
    # Перехлёст с объектами редактируемого слоя
    # ------------------------------------------------------------------

    def _update_overlap_preview(self, layer, ct_l2m, prepared):
        """Красная область: где перемещаемые полигоны наезжают на другие
        объекты ТОГО ЖЕ слоя (то, о чём спросим при отпускании кнопки).

        :param layer: редактируемый слой
        :param ct_l2m: преобразование CRS слоя -> CRS карты
        :param prepared: [(fid, QgsGeometry в CRS слоя)] — новые геометрии
        """
        now = time.monotonic()
        if now - self._overlap_ts < OVERLAP_PREVIEW_INTERVAL:
            return
        self._overlap_ts = now
        try:
            if settings.overlap_mode() == settings.OVERLAP_KEEP:
                self._reset_overlap()
                return
            overlaps = TopoEditor(self.iface, layer).preview_overlaps(
                prepared, {fid for fid, _g in prepared})
            if not overlaps:
                self._reset_overlap()
                return
            geoms = [o["geom"] for o in overlaps]
            g = (QgsGeometry.unaryUnion(geoms) if len(geoms) > 1
                 else geoms[0])
            g = QgsGeometry(g)
            g.transform(ct_l2m)
            self.overlap_rb.setToGeometry(g, None)
        except Exception as exc:
            utils.log(u"Превью перехлёста: сбой — {}".format(exc))

    # ------------------------------------------------------------------
    # Превью валидатора (этап 1 ТЗ): маркеры самопересечений, щели
    # ------------------------------------------------------------------

    def _ensure_violation_markers(self, n):
        """Пул красных маркеров (X) точек нарушений."""
        while len(self.violation_markers) < n:
            m = QgsVertexMarker(self.canvas())
            m.setIconType(QgsVertexMarker.ICON_X)
            m.setColor(QColor(230, 20, 20))
            m.setPenWidth(3)
            m.setIconSize(11)
            m.hide()
            self.violation_markers.append(m)

    def _show_violation_markers(self, pts_map):
        """Показать маркеры нарушений (координаты уже в CRS карты)."""
        self._ensure_violation_markers(len(pts_map))
        for i, m in enumerate(self.violation_markers):
            if i < len(pts_map):
                m.setCenter(pts_map[i])
                m.show()
            else:
                m.hide()

    def _reset_violations(self):
        """Сброс маркеров нарушений, заливки щелей, статуса и курсора."""
        for m in self.violation_markers:
            try:
                m.hide()
            except Exception:
                pass
        try:
            self.gap_rb.reset(QgsWkbTypes.PolygonGeometry)
        except Exception:
            pass
        self._set_status_message(u"")
        if self._forbidden_cursor:
            self._forbidden_cursor = False
            try:
                self.canvas().setCursor(Qt.CursorShape.CrossCursor)
            except Exception:
                pass

    def _set_status_message(self, text):
        """Текст причины нарушения в строке состояния (устойчиво к сбоям)."""
        try:
            self.iface.mainWindow().statusBar().showMessage(text)
        except Exception:
            pass

    def _set_forbidden_cursor(self, blocked):
        """Запрещающий курсор при блокирующих нарушениях перетаскивания."""
        if blocked == self._forbidden_cursor:
            return
        self._forbidden_cursor = blocked
        try:
            self.canvas().setCursor(
                Qt.CursorShape.ForbiddenCursor if blocked
                else Qt.CursorShape.CrossCursor)
        except Exception:
            self._forbidden_cursor = False

    def _update_validator_preview(self, layer, ct_l2m, prepared):
        """Превью валидатора при перетаскивании (троттлинг 0,08 с).

        Всегда: дешёвые правила по кандидатам (самопересечения — красные
        маркеры, шипы) + заливка щелей при небольшом числе соседей.
        Перехлёсты рисуются, как раньше, _update_overlap_preview. Текст
        причины — в строке состояния, курсор — запрещающий при блоке.

        :param layer: редактируемый слой
        :param ct_l2m: преобразование CRS слоя -> CRS карты
        :param prepared: [(fid, QgsGeometry в CRS слоя)] — новые геометрии
        """
        now = time.monotonic()
        if now - self._violation_ts < OVERLAP_PREVIEW_INTERVAL:
            return
        self._violation_ts = now
        try:
            opts = settings.validator_opts()
            violations = []
            for fid, g in prepared:
                violations.extend(GeosValidity().check(fid, g))
                violations.extend(NoSpikes().check(fid, g, opts))

            # щели: объединение соседей каждый тик допустимо только при
            # небольшом их числе (см. GAP_PREVIEW_MAX_NEIGHBORS)
            valid_prepared = [(f, g) for f, g in prepared
                              if g is not None and not g.isNull()
                              and not g.isEmpty() and g.isGeosValid()]
            if valid_prepared:
                bbox = union_bbox([g for _f, g in valid_prepared])
                if bbox is not None:
                    w = max(bbox.width(), 0.0)
                    h = max(bbox.height(), 0.0)
                    bbox.grow(max((w + h) * 0.001, 1e-9))
                    ed = TopoEditor(self.iface, layer)
                    exclude = {f for f, _g in valid_prepared}
                    try:
                        neighbors = ed._same_layer_neighbors(bbox, exclude)
                    except Exception:
                        neighbors = None  # лимит соседей — щели на отпускании
                    if neighbors is not None and \
                            len(neighbors) <= GAP_PREVIEW_MAX_NEIGHBORS:
                        old = ed._old_geoms(exclude)
                        violations.extend(NoNewGaps().check(
                            {"old_geoms": old,
                             "new_geoms": dict(valid_prepared),
                             "neighbors": neighbors,
                             "seam_min_ratio": opts.get("seam_min_ratio")}))

            errors = blocking(violations)
            # маркеры точек (самопересечения, шипы, потеря узлов)
            pts_map = []
            for v in violations:
                if v.point is None:
                    continue
                try:
                    pts_map.append(ct_l2m.transform(QgsPointXY(v.point)))
                except Exception:
                    continue
            self._show_violation_markers(pts_map[:32])  # защита UI от толпы
            # заливка новых щелей
            gap_geoms = [v.geom for v in violations
                         if v.code == CODE_NEW_GAP and v.geom is not None]
            if gap_geoms:
                g = (QgsGeometry.unaryUnion(gap_geoms)
                     if len(gap_geoms) > 1 else gap_geoms[0])
                g = QgsGeometry(g)
                try:
                    g.transform(ct_l2m)
                    self.gap_rb.setToGeometry(g, None)
                except Exception:
                    pass
            else:
                try:
                    self.gap_rb.reset(QgsWkbTypes.PolygonGeometry)
                except Exception:
                    pass
            # строка состояния + курсор
            if errors:
                self._set_status_message(
                    u"Топологическое редактирование: " +
                    u"; ".join(v.message for v in errors[:2]))
            else:
                self._set_status_message(u"")
            self._set_forbidden_cursor(bool(errors))
        except Exception as exc:
            utils.log(u"Превью валидатора: сбой — {}".format(exc))

    def _zoom_to_violation(self, map_pt):
        """Центрировать карту на нарушении и мигнуть маркером 2 с."""
        try:
            extent = self.canvas().extent()
            w = max(extent.width(), 1e-9)
            h = max(extent.height(), 1e-9)
            rect = QgsRectangle(map_pt.x() - w * 0.02,
                                map_pt.y() - h * 0.02,
                                map_pt.x() + w * 0.02,
                                map_pt.y() + h * 0.02)
            self.canvas().setExtent(rect)
            self.canvas().refresh()
        except Exception:
            pass
        marker = QgsVertexMarker(self.canvas())
        marker.setIconType(QgsVertexMarker.ICON_X)
        marker.setColor(QColor(230, 20, 20))
        marker.setPenWidth(3)
        marker.setIconSize(14)
        marker.setCenter(map_pt)
        marker.show()
        QTimer.singleShot(2000, marker.hide)

    def _push_violation_error(self, report, ct_l2m=None):
        """Сообщение о блокировке с кнопкой «Показать место» (этап 1 ТЗ).

        Точки нарушений в report["violations"] — в CRS слоя; при переданном
        преобразовании переводятся в CRS карты для кнопки.
        """
        text = report.get("error") or u"Операция заблокирована"
        vlist = report.get("violations") or []
        bar = self.iface.messageBar()
        try:
            widget = bar.createMessage(u"Топологическое редактирование",
                                       text)
            pts_map = []
            for v in vlist:
                pt = v.get("point")
                if not pt:
                    continue
                try:
                    if ct_l2m is not None:
                        pt = ct_l2m.transform(QgsPointXY(pt[0], pt[1]))
                    else:
                        pt = QgsPointXY(pt[0], pt[1])
                    pts_map.append(QgsPointXY(pt))
                except Exception:
                    continue
            if pts_map:
                btn = QPushButton(u"Показать место", widget)
                target = pts_map[0]
                btn.clicked.connect(
                    lambda: self._zoom_to_violation(QgsPointXY(target)))
                widget.layout().addWidget(btn)
            bar.pushWidget(widget, Qgis.Critical, duration=10)
        except Exception:
            # запасной путь: обычное сообщение без кнопки
            bar.pushMessage(u"Топологическое редактирование", text,
                            level=Qgis.Critical, duration=6)

    def _ask_makevalid(self, info):
        """Диалог «предлагать исправить через makeValid» (по умолчанию
        настройка выключена — диалог не вызывается)."""
        box = QMessageBox(self.iface.mainWindow())
        box.setIcon(utils.enum_member(QMessageBox, "Icon", "Warning"))
        box.setWindowTitle(u"Невалидная геометрия")
        box.setText(u"Полигон #{} имеет самопересечение или иную "
                    u"невалидную геометрию.".format(info.get("fid")))
        box.setInformativeText(
            u"Исправить через makeValid? Форма изменится автоматически "
            u"(площадь до: {:g}). После исправления операция будет "
            u"продолжена.".format(info.get("area_before", 0.0)))
        role_accept = utils.enum_member(QMessageBox, "ButtonRole",
                                        "AcceptRole")
        role_reject = utils.enum_member(QMessageBox, "ButtonRole",
                                        "RejectRole")
        yes = box.addButton(u"Исправить (makeValid)", role_accept)
        box.addButton(u"Отменить операцию", role_reject)
        box.setDefaultButton(
            box.addButton(utils.enum_member(QMessageBox, "StandardButton",
                                            "No")))
        box.setEscapeButton(
            box.buttons()[-1] if box.buttons() else None)
        run = getattr(box, "exec_", None) or getattr(box, "exec")
        run()
        return box.clickedButton() is yes

    def _ask_overlap(self, info):
        """Диалог: что делать с перехлёстом с объектами этого же слоя.

        :param info: словарь TopoEditor ("neighbors", "moved", "area", ...)
        :return: CHOICE_CLIP_MOVED | CHOICE_CLIP_NEIGHBORS | CHOICE_KEEP |
                 CHOICE_CANCEL
        """
        name = u""
        try:
            name = u" «{}»".format(self.drag["layer"].name())
        except Exception:
            pass
        box = QMessageBox(self.iface.mainWindow())
        box.setIcon(utils.enum_member(QMessageBox, "Icon", "Question"))
        box.setWindowTitle(u"Перехлёст с объектами слоя")
        box.setText(u"Перемещение накладывает полигоны на другие объекты "
                    u"слоя{}.".format(name))
        box.setInformativeText(
            u"Затронуто соседних объектов: {}. Площадь перехлёста: {:.6g} "
            u"(ед. слоя в квадрате).\n\n"
            u"«Обрезать перемещаемые» — перемещаемые полигоны уступят "
            u"место соседям.\n"
            u"«Обрезать соседей» — соседи уступят место перемещаемым "
            u"полигонам.\n"
            u"«Оставить» — перехлёст сохранится.".format(
                info.get("neighbors", 0), info.get("area", 0.0)))
        role_accept = utils.enum_member(QMessageBox, "ButtonRole",
                                        "AcceptRole")
        role_destr = utils.enum_member(QMessageBox, "ButtonRole",
                                       "DestructiveRole")
        b_moved = box.addButton(u"Обрезать перемещаемые", role_accept)
        b_neigh = box.addButton(u"Обрезать соседей", role_accept)
        b_keep = box.addButton(u"Оставить перехлёст", role_destr)
        b_cancel = box.addButton(
            utils.enum_member(QMessageBox, "StandardButton", "Cancel"))
        box.setDefaultButton(b_moved)
        box.setEscapeButton(b_cancel)
        run = getattr(box, "exec_", None) or getattr(box, "exec")
        run()
        clicked = box.clickedButton()
        if clicked is b_moved:
            return CHOICE_CLIP_MOVED
        if clicked is b_neigh:
            return CHOICE_CLIP_NEIGHBORS
        if clicked is b_keep:
            return CHOICE_KEEP
        return CHOICE_CANCEL

    def _overlap_notes(self, report):
        """Фразы для отчёта: что случилось с перехлёстом (список строк)."""
        parts = []
        if report.get("neighbors_clipped"):
            parts.append(u"обрезано соседей: {}".format(
                report["neighbors_clipped"]))
        if report.get("overlaps") and \
                report.get("overlap_choice") == CHOICE_KEEP:
            parts.append(u"перехлёст с {} объектами оставлен".format(
                report["overlaps"]))
        return parts

    def _push_cancelled(self):
        """Сообщение: правка отменена пользователем при запросе о перехлёсте."""
        self.iface.messageBar().pushMessage(
            self.TITLE,
            u"Правка отменена: перехлёст с объектами слоя не принят. "
            u"Слой не изменён.", level=Qgis.Info, duration=4)

    # ------------------------------------------------------------------
    # События карты (общие)
    # ------------------------------------------------------------------

    def activate(self):
        self.setCursor(Qt.CursorShape.CrossCursor)
        super(BaseTopoTool, self).activate()

    def deactivate(self):
        self._cleanup()
        if self.action is not None and self.action.isChecked():
            self.action.setChecked(False)
        super(BaseTopoTool, self).deactivate()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key.Key_Escape:
            if self.drag is not None:
                self._cleanup()
            else:
                self.iface.actionPan().trigger()
            return
        super(BaseTopoTool, self).keyPressEvent(e)
