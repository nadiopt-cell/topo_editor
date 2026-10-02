# -*- coding: utf-8 -*-
"""Вспомогательные функции: выбор редактируемого слоя, допуски, сообщения."""

from qgis.core import (
    QgsProject, QgsVectorLayer, QgsWkbTypes, QgsPointXY,
    QgsCoordinateTransform, QgsUnitTypes, Qgis,
)
from qgis.PyQt.QtWidgets import QMessageBox

TOLERANCE_PX = 10.0  # допуск поиска узлов/рёбер, пикселей


def is_polygon_layer(layer):
    """True — это корректный полигональный векторный слой."""
    return (isinstance(layer, QgsVectorLayer) and layer.isValid()
            and layer.geometryType() == QgsWkbTypes.PolygonGeometry)


def resolve_edit_layers(iface, offer_start=True, quiet=False):
    """Список редактируемых полигональных слоёв в порядке приоритета.

    Первый элемент — активный слой, если он в правке. Если активный слой
    не в правке, а редактируемые полигональные слои есть — возвращаются
    ВСЕ они: инструмент выберет тот, под курсором которого найден узел
    или ребро (иначе при нескольких слоях в правке инструмент искал
    не в том слое и ошибочно сообщал «нет узла»). Если редактируемых
    слоёв нет — предлагается включить правку активного полигонального
    слоя (кроме quiet-режима).

    :param offer_start: предлагать ли включить редактирование диалогом
    :param quiet: не показывать никаких диалогов и сообщений (для hover)
    :return: [QgsVectorLayer, ...] — может быть пустым
    """
    layer = iface.activeLayer()
    if is_polygon_layer(layer) and layer.isEditable():
        return [layer]

    edited = [l for l in QgsProject.instance().mapLayers().values()
              if is_polygon_layer(l) and l.isEditable()]
    if edited:
        return edited

    if quiet:
        return []

    if is_polygon_layer(layer) and offer_start:
        ans = QMessageBox.question(
            iface.mainWindow(),
            u"Топологическое редактирование",
            u"Слой «{}» не находится в режиме редактирования.\n"
            u"Включить редактирование?".format(layer.name()),
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
        if ans == QMessageBox.Yes:
            layer.startEditing()
            return [layer]
        return []

    iface.messageBar().pushMessage(
        u"Топологическое редактирование",
        u"Включите режим редактирования полигонального слоя "
        u"и сделайте его активным.",
        level=Qgis.Warning, duration=5)
    return []


def resolve_edit_layer(iface, offer_start=True, quiet=False):
    """Первый редактируемый полигональный слой (см. resolve_edit_layers).

    :return: QgsVectorLayer или None
    """
    layers = resolve_edit_layers(iface, offer_start=offer_start,
                                 quiet=quiet)
    return layers[0] if layers else None


def layer_tolerance(canvas, layer, map_pt):
    """Переводит пиксельный допуск в единицы CRS слоя.

    :param canvas: QgsMapCanvas
    :param layer: редактируемый слой
    :param map_pt: точка в CRS карты
    :return: (pt_layer, eps_layer, ct_map_to_layer, ct_layer_to_map)
    """
    ms = canvas.mapSettings()
    dest = ms.destinationCrs()
    try:
        tol_map = ms.convertToMapUnits(TOLERANCE_PX, QgsUnitTypes.RenderPixels)
    except Exception:
        tol_map = TOLERANCE_PX
    ct_m2l = QgsCoordinateTransform(dest, layer.crs(), QgsProject.instance())
    ct_l2m = QgsCoordinateTransform(layer.crs(), dest, QgsProject.instance())
    pt_layer = ct_m2l.transform(map_pt)
    try:
        shifted = ct_m2l.transform(
            QgsPointXY(map_pt.x() + tol_map, map_pt.y()))
        eps = pt_layer.distance(shifted)
    except Exception:
        eps = tol_map
    if eps <= 0.0:
        eps = tol_map
    return pt_layer, eps, ct_m2l, ct_l2m
