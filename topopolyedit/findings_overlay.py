# -*- coding: utf-8 -*-
"""Оверлей ошибок аудита слоя на карте (этап 5 ТЗ, п. 5).

Маркеры находок аудита рисуются поверх канвы, как у «Аудита узлов»:
только видимая область, бюджет по числу маркеров, перестройка при
пан/зуме — отложенная (debounce), чтобы не мешать навигации. Цвет
маркера — по классу ошибки:

  красный   — не исправимо автоматически (INVALID у чужих мест,
              перехлёст без выбора, большая щель, сливер);
  оранжевый — исправимо автоматически (безопасные коды);
  жёлтый    — прочее (NEAR_NODE без безопасного слияния и т.п.).

Точки находок хранятся в CRS слоя; для карты переводятся через
QgsCoordinateTransform (слои в разных СК — этап 4).
"""

from qgis.PyQt.QtCore import QTimer
from qgis.PyQt.QtGui import QColor
from qgis.gui import QgsVertexMarker
from qgis.core import (
    QgsCoordinateTransform, QgsProject, QgsRectangle,
)

from .core.checks.base import (
    CODE_INVALID, CODE_OVERLAP, CODE_GAP, CODE_NEAR_NODE, CODE_SLIVER,
    CODE_SPIKE, CODE_DUP_NODE, CODE_T_JUNCTION,
)

# бюджет маркеров на канве (как у аудита узлов — лимит, не тормозим)
MAX_MARKERS = 2000
REFRESH_DELAY_MS = 250

RED = QColor(230, 20, 20)
ORANGE = QColor(255, 140, 0)
YELLOW = QColor(230, 210, 0)


def severity_color(finding):
    """Цвет маркера по классу и исправимости находки."""
    code = finding.code
    if code in (CODE_INVALID, CODE_OVERLAP):
        return RED
    if code == CODE_GAP:
        return ORANGE if finding.fixable else RED
    if code in (CODE_DUP_NODE, CODE_T_JUNCTION, CODE_SPIKE):
        return ORANGE
    if code == CODE_NEAR_NODE:
        return ORANGE if finding.fixable else YELLOW
    if code == CODE_SLIVER:
        return YELLOW
    return YELLOW


class FindingsOverlay(object):
    """Маркеры находок аудита на канве с бюджетом и отложенной
    перестройкой."""

    def __init__(self, iface):
        self.iface = iface
        self._canvas = None
        try:
            self._canvas = iface.mapCanvas()
        except Exception:
            self._canvas = None
        self._points = []  # [(QgsPointXY map_crs, color)]
        self._items = []
        self._timer = None
        if self._canvas is not None:
            try:
                self._timer = QTimer(self._canvas)
                self._timer.setSingleShot(True)
                self._timer.timeout.connect(self._render)
            except Exception:
                self._timer = None
            try:
                self._canvas.extentChanged.connect(self._schedule)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Публичный API
    # ------------------------------------------------------------------

    def set_findings(self, points_with_colors):
        """[(QgsPointXY map_crs, QColor), ...] — перерисовать."""
        self._points = list(points_with_colors or [])
        self._render_now()

    def clear(self):
        self._points = []
        self._render_now()

    def shutdown(self):
        try:
            if self._canvas is not None:
                self._canvas.extentChanged.disconnect(self._schedule)
        except Exception:
            pass
        self._clear_items()
        self._points = []

    # ------------------------------------------------------------------
    # Внутреннее
    # ------------------------------------------------------------------

    def _schedule(self, *_args):
        if self._timer is not None:
            self._timer.start(REFRESH_DELAY_MS)

    def _clear_items(self):
        for item in self._items:
            try:
                self._canvas.scene().removeItem(item)
            except Exception:
                pass
        self._items = []

    def _render_now(self):
        if self._canvas is None:
            return
        self._clear_items()
        if not self._points:
            return
        try:
            extent = self._canvas.extent()
        except Exception:
            extent = None
        visible = []
        for pt, color in self._points:
            if extent is not None and isinstance(extent, QgsRectangle) \
                    and not extent.contains(pt):
                continue
            visible.append((pt, color))
            if len(visible) >= MAX_MARKERS:
                break
        for pt, color in visible:
            try:
                marker = QgsVertexMarker(self._canvas)
                marker.setIconType(QgsVertexMarker.ICON_X)
                marker.setColor(color)
                marker.setPenWidth(2)
                marker.setIconSize(10)
                marker.setCenter(pt)
                marker.show()
                self._items.append(marker)
            except Exception:
                break


def transform_to_map(iface, layer_crs, pt):
    """Точка CRS слоя -> CRS карты (без преобразования при сбое)."""
    try:
        canvas = iface.mapCanvas()
        dest = canvas.mapSettings().destinationCrs()
        if dest is None or layer_crs is None or dest == layer_crs:
            return pt
        ct = QgsCoordinateTransform(layer_crs, dest,
                                    QgsProject.instance())
        return ct.transform(pt)
    except Exception:
        return pt
