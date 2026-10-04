# -*- coding: utf-8 -*-
"""Общая база инструментов карты TopoPolyEdit (BaseTopoTool).

Выделяет общий код трёх QgsMapTool-инструментов (v1.4.0 — рефакторинг
без изменения поведения):

  * движок прилипания SnappingEngine;
  * маркер точки прилипания (фиолетовый круг) и подсветка ребра
    прилипания (фиолетовая линия);
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

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QColor
from qgis.gui import QgsMapTool, QgsVertexMarker, QgsRubberBand
from qgis.core import QgsPointXY, QgsGeometry, QgsWkbTypes, Qgis

from .snapping_engine import SnappingEngine


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
        self._cleanup_extra()

    def _push_error(self, exc):
        """Единое сообщение об ошибке операции (message bar)."""
        self.iface.messageBar().pushMessage(
            self.TITLE, u"Ошибка: {}".format(exc),
            level=Qgis.Critical, duration=5)

    def _show_snap_feedback(self, snap):
        """Маркер найденной привязки; для ребра — линия сегмента."""
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
