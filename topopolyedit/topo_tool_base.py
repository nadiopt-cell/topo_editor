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

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import QMessageBox
from qgis.gui import QgsMapTool, QgsVertexMarker, QgsRubberBand
from qgis.core import QgsPointXY, QgsGeometry, QgsWkbTypes, Qgis

from .snapping_engine import SnappingEngine
from .topo_editor import (TopoEditor, CHOICE_CLIP_MOVED,
                          CHOICE_CLIP_NEIGHBORS, CHOICE_KEEP, CHOICE_CANCEL)
from . import settings
from . import utils

# Не чаще раза в столько секунд пересчитывается красная область перехлёста
# при перетаскивании (пересечения геометрий дороже привязки).
OVERLAP_PREVIEW_INTERVAL = 0.08


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
        """Оранжевая резиновая лента одного перемещаемого полигона."""
        rb = QgsRubberBand(self.canvas(), QgsWkbTypes.PolygonGeometry)
        rb.setStrokeColor(QColor(230, 120, 0))
        rb.setFillColor(QColor(255, 170, 0, 90))
        rb.setWidth(2)
        self.rubbers.append(rb)
        return rb

    def _reset_poly_rubbers(self):
        """Сброс всех лент перемещаемых полигонов."""
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
