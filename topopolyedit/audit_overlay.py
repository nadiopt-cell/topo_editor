# -*- coding: utf-8 -*-
"""Оверлей «Аудит узлов» (v1.6.0): вершины двух групп слоёв сразу.

Режим включается кнопкой «Аудит узлов» на панели плагина и работает
ПОВЕРХ любого активного инструмента (это не QgsMapTool, а обычный
объект с резиновыми лентами на канве):

  * оранжевые квадраты — узлы редактируемого слоя (или активного
    полигонального, если правка не включена);
  * синие кружки — узлы слоёв прилипания (участники снэпа за вычетом
    редактируемых, чёрный список настроек учитывается);
  * красные крестики — узлы на чужой границе БЕЗ пары на другой
    стороне: «лишние» узлы редактируемого слоя и «недостающие» узлы
    там, где у эталонов стык разбит, а у нас — нет (см. node_audit).

Пересчёт: по включению, по изменению экстента карты (пан/зум, с
дебаунсом REFRESH_DELAY_MS) и по layerModified подключенных слоёв —
правки слоя сразу отражаются в аудите. Сбор ограничен видимой
областью и предохранителями node_audit (MAX_NODES/MAX_SEGS); при
усечении пользователь получает подсказку «приблизьте масштаб».

Геометрия собирается в CRS КАРТЫ (пороги eps_match/eps_touch заданы в
единицах карты) — так же, как это делает SnappingEngine для подсказок.

Единицы и дедупликация: смежные полигоны дают вершины-двойники —
узлы дедуплицируются в пределах eps_match (тот же подход, что у
подсказок прилипания в v1.5.0).
"""

import time

from qgis.PyQt.QtCore import QTimer
from qgis.PyQt.QtGui import QColor
from qgis.core import (
    QgsProject, QgsFeatureRequest, QgsCoordinateTransform, QgsGeometry,
    QgsPointXY, QgsWkbTypes, Qgis,
)
from qgis.gui import QgsRubberBand

from . import node_audit as na
from . import utils
from .geo_utils import norm_polys, iter_ring_segments, iter_polyline_parts
from .snapping_engine import get_snap_layers

# Контрастная пара + сигнальный цвет ошибок:
COLOR_EDIT = (255, 130, 0)    # оранжевый — узлы редактируемого слоя
COLOR_REF = (40, 110, 255)    # синий — узлы слоёв прилипания
COLOR_ERR = (230, 25, 25)     # красный — узел без пары на границе

REFRESH_DELAY_MS = 300   # дебаунс пересчёта при пан/зуме
WARN_INTERVAL = 10.0     # не чаще раза в столько секунд — подсказка об усечении
MAX_FEATURES = 20000     # предохранитель объектов на слой (как в снэпе)
DUP_EPS2 = 1e-12         # замыкающий дубликат кольца (как в geo_utils)

TITLE = u"Аудит узлов"


class NodeAuditOverlay(object):
    """Контроллер режима «Аудит узлов»: сбор, классификация, отрисовка."""

    def __init__(self, iface):
        self.iface = iface
        self.canvas = iface.mapCanvas()
        self.enabled = False
        self._pending = False
        self._sig_layers = []     # слои с подключенным layerModified
        self._warn_ts = 0.0
        self._last = None         # последний результат classify (+счётчики)

        self.band_edit = self._make_band(COLOR_EDIT, 7, "ICON_BOX", 0)
        self.band_ref = self._make_band(COLOR_REF, 7, "ICON_CIRCLE", 90)
        # крест ошибок создаётся последним — рисуется ПОВЕРХ цветных узлов
        self.band_err = self._make_band(COLOR_ERR, 11, "ICON_X", 0)

    # ------------------------------------------------------------------
    # Включение / выключение / жизненный цикл
    # ------------------------------------------------------------------

    def set_enabled(self, on):
        """Включает/выключает режим; включение сразу пересчитывает аудит."""
        on = bool(on)
        if on == self.enabled:
            if on:
                self.refresh(push_summary=True)
            return
        self.enabled = on
        if on:
            try:
                self.canvas.extentChanged.connect(self.schedule_refresh)
            except Exception:
                pass
            self.refresh(push_summary=True)
        else:
            try:
                self.canvas.extentChanged.disconnect(self.schedule_refresh)
            except Exception:
                pass
            self._disconnect_layers()
            self._clear_bands()
            self._last = None

    def shutdown(self):
        """Полный сброс при выгрузке плагина."""
        self.set_enabled(False)

    def schedule_refresh(self, *_args):
        """Отложенный пересчёт (дебаунс: не чаще раза в REFRESH_DELAY_MS)."""
        if not self.enabled or self._pending:
            return
        self._pending = True
        try:
            QTimer.singleShot(REFRESH_DELAY_MS, self._scheduled_refresh)
        except Exception:
            self._pending = False

    def _scheduled_refresh(self):
        self._pending = False
        self.refresh()

    # ------------------------------------------------------------------
    # Пересчёт
    # ------------------------------------------------------------------

    def refresh(self, push_summary=False):
        """Пересборка узлов в видимой области + классификация + отрисовка."""
        if not self.enabled:
            return
        try:
            res = self._collect_and_classify()
        except Exception as exc:
            utils.log(u"{}: сбой пересчёта — {}".format(TITLE, exc))
            self._clear_bands()
            return
        self._last = res
        self._render(res)
        if res.get("truncated"):
            self._warn_truncated()
        if push_summary:
            self._push_summary(res)

    def _collect_and_classify(self):
        """Сбор узлов/сегментов обеих групп и classify_border_nodes."""
        empty = {"plain_a": [], "error_a": [],
                 "plain_b": [], "error_b": [], "truncated": False}
        try:
            extent = self.canvas.extent()
            dest = self.canvas.mapSettings().destinationCrs()
        except Exception as exc:
            utils.log(u"{}: нет экстента/CRS карты — {}".format(TITLE, exc))
            return empty
        edit_layers = self._edit_layers()
        ref_layers = self._ref_layers(edit_layers)
        self._reconnect_layers(edit_layers + ref_layers)
        pts_e, segs_e, tr_e = self._collect_group(edit_layers, extent, dest)
        pts_r, segs_r, tr_r = self._collect_group(ref_layers, extent, dest)
        res = na.classify_border_nodes(pts_e, segs_e, pts_r, segs_r)
        res["truncated"] = bool(tr_e or tr_r)
        return res

    def _edit_layers(self):
        """Группа A: редактируемые полигональные слои; иначе активный."""
        layers = utils.resolve_edit_layers(self.iface, offer_start=False,
                                           quiet=True)
        layers = [l for l in layers if utils.is_polygon_layer(l)]
        if layers:
            return layers
        try:
            active = self.iface.activeLayer()
        except Exception:
            active = None
        return [active] if utils.is_polygon_layer(active) else []

    def _ref_layers(self, edit_layers):
        """Группа B: слои прилипания за вычетом группы A (чёрный список
        учитывается внутри get_snap_layers при каждом вызове)."""
        edit_ids = set()
        for lyr in edit_layers:
            try:
                edit_ids.add(lyr.id())
            except Exception:
                pass
        try:
            snap = get_snap_layers(edit_layers[0] if edit_layers else None)
        except Exception as exc:
            utils.log(u"{}: слои прилипания недоступны — {}".format(
                TITLE, exc))
            return []
        return [l for l in snap if l.id() not in edit_ids]

    # ------------------------------------------------------------------
    # Сбор геометрии
    # ------------------------------------------------------------------

    def _collect_group(self, layers, extent_map, dest_crs):
        """Узлы и сегменты группы слоёв в CRS карты (+флаг усечения)."""
        raw_pts = []
        segs = []
        truncated = False
        for lyr in layers:
            if len(raw_pts) >= na.MAX_RAW_POINTS or len(segs) >= na.MAX_SEGS:
                truncated = True
                break
            try:
                self._collect_layer(lyr, extent_map, dest_crs,
                                    raw_pts, segs)
            except Exception as exc:
                utils.log(u"{}: слой «{}» пропущен — {}".format(
                    TITLE, lyr.name(), exc))
        pts = na.dedup_points(raw_pts)
        if len(pts) > na.MAX_NODES:
            pts = pts[:na.MAX_NODES]
            truncated = True
        if len(segs) > na.MAX_SEGS:
            segs = segs[:na.MAX_SEGS]
            truncated = True
        return pts, segs, truncated

    def _collect_layer(self, lyr, extent_map, dest_crs, pts_out, segs_out):
        """Вершины и сегменты одного слоя (в видимой области) в CRS карты."""
        same = lyr.crs().authid() == dest_crs.authid()
        ct = None
        rect_l = extent_map
        if not same:
            ct_m2l = QgsCoordinateTransform(dest_crs, lyr.crs(),
                                            QgsProject.instance())
            rect_l = ct_m2l.transformBoundingBox(extent_map)
            ct = QgsCoordinateTransform(lyr.crs(), dest_crs,
                                        QgsProject.instance())
        req = QgsFeatureRequest(rect_l)
        req.setSubsetOfAttributes([])
        gtype = lyr.geometryType()
        cnt = 0
        for feat in lyr.getFeatures(req):
            cnt += 1
            if cnt > MAX_FEATURES:
                break
            g = feat.geometry()
            if g is None or g.isNull() or g.isEmpty():
                continue
            if ct is not None:
                g = QgsGeometry(g)
                if g.transform(ct) != 0:
                    continue
            if gtype == QgsWkbTypes.PolygonGeometry:
                nr = norm_polys(g)
                if not nr:
                    continue
                for part in nr[0]:
                    for ring in part:
                        n = len(ring)
                        if n > 1 and ring[0].sqrDist(ring[-1]) <= DUP_EPS2:
                            verts = ring[:-1]
                        else:
                            verts = ring
                        for v in verts:
                            pts_out.append((v.x(), v.y()))
                        for _si, a, b in iter_ring_segments(ring):
                            segs_out.append(((a.x(), a.y()),
                                             (b.x(), b.y())))
            elif gtype == QgsWkbTypes.LineGeometry:
                for line in iter_polyline_parts(g):
                    for v in line:
                        pts_out.append((v.x(), v.y()))
                    for i in range(len(line) - 1):
                        a, b = line[i], line[i + 1]
                        segs_out.append(((a.x(), a.y()), (b.x(), b.y())))
            else:  # точечные слои: узлы = сами точки
                if g.isMultipart():
                    pp = g.asMultiPoint()
                else:
                    p = g.asPoint()
                    pp = [p] if p is not None else []
                for v in pp:
                    pts_out.append((v.x(), v.y()))

    # ------------------------------------------------------------------
    # Отрисовка и сообщения
    # ------------------------------------------------------------------

    def _make_band(self, rgb, size, icon_name, fill_alpha):
        rb = QgsRubberBand(self.canvas, QgsWkbTypes.PointGeometry)
        rb.setStrokeColor(QColor(*rgb))
        rb.setFillColor(QColor(rgb[0], rgb[1], rgb[2], fill_alpha))
        rb.setIcon(utils.enum_member(QgsRubberBand, "IconType", icon_name))
        rb.setIconSize(size)
        rb.setWidth(2)
        return rb

    def _render(self, res):
        """Цветные узлы (весь слой) + красные крестики ошибок поверх."""
        try:
            self._set_band(self.band_edit,
                           res["plain_a"] + res["error_a"])
            self._set_band(self.band_ref,
                           res["plain_b"] + res["error_b"])
            self._set_band(self.band_err,
                           res["error_a"] + res["error_b"])
        except Exception as exc:
            utils.log(u"{}: сбой отрисовки — {}".format(TITLE, exc))

    def _set_band(self, rb, tuples_xy):
        pts = [QgsPointXY(x, y) for x, y in tuples_xy]
        if pts:
            rb.setToGeometry(QgsGeometry.fromMultiPointXY(pts), None)
        else:
            rb.reset(QgsWkbTypes.PointGeometry)

    def _clear_bands(self):
        for rb in (self.band_edit, self.band_ref, self.band_err):
            try:
                rb.reset(QgsWkbTypes.PointGeometry)
            except Exception:
                pass

    def _push_summary(self, res):
        """Сводка при включении режима: сколько узлов и сколько ошибок."""
        n_edit = len(res["plain_a"]) + len(res["error_a"])
        n_ref = len(res["plain_b"]) + len(res["error_b"])
        n_err = len(res["error_a"]) + len(res["error_b"])
        msg = (u"Узлы: редактируемый слой — {0} (оранжевые квадраты), "
               u"слои прилипания — {1} (синие кружки), без пары на "
               u"границе — {2} (красные крестики).".format(
                   n_edit, n_ref, n_err))
        try:
            self.iface.messageBar().pushMessage(
                TITLE, msg,
                level=(Qgis.Warning if n_err else Qgis.Info), duration=6)
        except Exception:
            pass

    def _warn_truncated(self):
        now = time.monotonic()
        if now - self._warn_ts < WARN_INTERVAL:
            return
        self._warn_ts = now
        try:
            self.iface.messageBar().pushMessage(
                TITLE,
                u"Узлов в видимой области слишком много — показана часть. "
                u"Приблизьте масштаб, чтобы увидеть остальные.",
                level=Qgis.Warning, duration=5)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Сигналы слоёв: правки сразу видны в аудите
    # ------------------------------------------------------------------

    def _reconnect_layers(self, layers):
        """layerModified -> schedule_refresh (правка слоя обновляет аудит).

        Подключение лучшее из возможного: у объекта без сигнала (тесты,
        нестандартные обёртки) просто пропускается — аудит обновится по
        пан/зуму.
        """
        self._disconnect_layers()
        for lyr in layers:
            try:
                lyr.layerModified.connect(self.schedule_refresh)
                self._sig_layers.append(lyr)
            except Exception:
                pass

    def _disconnect_layers(self):
        for lyr in self._sig_layers:
            try:
                lyr.layerModified.disconnect(self.schedule_refresh)
            except Exception:
                pass
        self._sig_layers = []
