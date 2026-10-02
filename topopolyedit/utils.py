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


def resolve_edit_layer(iface, offer_start=True, quiet=False):
    """Определяет редактируемый полигональный слой.

    Порядок: активный слой в режиме редактирования -> единственный слой
    проекта в режиме редактирования -> предложение включить редактирование
    активного слоя.

    :param offer_start: предлагать ли включить редактирование диалогом
    :param quiet: не показывать никаких диалогов и сообщений (для hover)
    :return: QgsVectorLayer или None
    """
    layer = iface.activeLayer()
    if is_polygon_layer(layer) and layer.isEditable():
        return layer

    edited = [l for l in QgsProject.instance().mapLayers().values()
              if is_polygon_layer(l) and l.isEditable()]
    if len(edited) == 1:
        return edited[0]

    if quiet:
        return None

    if is_polygon_layer(layer) and not layer.isEditable() and offer_start:
        ans = QMessageBox.question(
            iface.mainWindow(),
            u"Топологическое редактирование",
            u"Слой «{}» не находится в режиме редактирования.\n"
            u"Включить редактирование?".format(layer.name()),
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
        if ans == QMessageBox.Yes:
            layer.startEditing()
            return layer
        return None

    iface.messageBar().pushMessage(
        u"Топологическое редактирование",
        u"Включите режим редактирования полигонального слоя "
        u"и сделайте его активным.",
        level=Qgis.Warning, duration=5)
    return None


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
