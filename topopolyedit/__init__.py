# -*- coding: utf-8 -*-
"""TopoPolyEdit — топологическое редактирование полигонов для QGIS.

Точка входа плагина: фабрика класса.
"""


def classFactory(iface):
    """QGIS вызывает эту функцию при загрузке плагина.

    :param iface: интерфейс QgisInterface
    :return: экземпляр класса плагина
    """
    from .plugin import TopoPolyEditPlugin
    return TopoPolyEditPlugin(iface)
