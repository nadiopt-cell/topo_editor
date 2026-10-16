# -*- coding: utf-8 -*-
"""Генераторы тестовых данных для QGIS-тестов TopoPolyEdit (этап 0 ТЗ).

Все фикстуры детерминированы: случайность не используется, чтобы падение
воспроизводилось битово. Слои памятьные (memory:) — ничего не пишется на диск.

Сетки строятся в EPSG:3857 (метры): значения TOPO_MATCH_EPS (1e-6 ед. слоя)
и snap-допусков осмысленны, площади положительные.
"""
from qgis.core import (
    QgsFeature,
    QgsField,
    QgsGeometry,
    QgsPointXY,
    QgsVectorLayer,
)
from qgis.PyQt.QtCore import QVariant

GRID_CELL = 10.0

# детерминированные «непрямые» внутренние узлы кадастровой фикстуры:
# общая граница между рядами становится двухсегментной
PERTURBED = {(1, 1): (10.3, 10.2), (2, 1): (19.7, 10.4)}


def _memory_polygon_layer(name, crs="EPSG:3857"):
    layer = QgsVectorLayer(
        "Polygon?crs={}&index=no".format(crs), name, "memory")
    assert layer.isValid(), "memory-слой не создан: " + name
    layer.dataProvider().addAttributes([QgsField("id", QVariant.String)])
    layer.updateFields()
    return layer


def _ring(pts):
    """[(x, y), ...] -> закрытое кольцо QgsPointXY."""
    ring = [QgsPointXY(x, y) for x, y in pts]
    if ring[0] != ring[-1]:
        ring.append(QgsPointXY(ring[0]))
    return ring


def _tag_map(layer):
    """{значение атрибута id: fid} после добавления фич."""
    out = {}
    for f in layer.getFeatures():
        out[f["id"]] = f.id()
    return out


def _add_feature(layer, tag, geoms):
    f = QgsFeature(layer.fields())
    if len(geoms) == 1:
        f.setGeometry(geoms[0])
    else:
        f.setGeometry(QgsGeometry.collectGeometry(geoms))
    f.setAttribute("id", tag)
    layer.dataProvider().addFeatures([f])


def make_grid_layer(nx, ny, cell=GRID_CELL, x0=0.0, y0=0.0,
                    name="grid", crs="EPSG:3857"):
    """Сетка nx x ny смежных прямоугольных участков.

    :return: (QgsVectorLayer, {(i, j): fid})
    """
    layer = _memory_polygon_layer(name, crs)
    for j in range(ny):
        for i in range(nx):
            x = x0 + i * cell
            y = y0 + j * cell
            ring = _ring([(x, y), (x + cell, y),
                          (x + cell, y + cell), (x, y + cell)])
            _add_feature(layer, "{},{}".format(i, j),
                         [QgsGeometry.fromPolygonXY([ring])])
    layer.updateExtents()
    tags = {}
    for key, fid in _tag_map(layer).items():
        i, j = key.split(",")
        tags[(int(i), int(j))] = fid
    return layer, tags


def _grid_node(i, j):
    """Координата узла сетки кадастра с детерминированными сдвигами."""
    p = PERTURBED.get((i, j))
    if p is not None:
        return p
    return (i * GRID_CELL, j * GRID_CELL)


def make_cadastral_layer(name="kadastr", crs="EPSG:3857"):
    """«Кадастр» 3x2: непрямые границы, дыра, мультиполигон.

    Общие узлы строк разделены детерминированными сдвигами (PERTURBED),
    поэтому общие границы становятся двухсегментными. Участок 2,0 —
    полигон с дырой; участок 2,1 — мультиполигон из двух частей
    (вторая часть стоит отдельно в (50,0)..(55,5)).

    :return: (QgsVectorLayer, {"tags": {(i, j): fid}})
    """
    layer = _memory_polygon_layer(name, crs)
    tags = {}

    def node(i, j):
        return _grid_node(i, j)

    def cell_ring(i, j):
        return _ring([node(i, j), node(i + 1, j),
                      node(i + 1, j + 1), node(i, j + 1)])

    # обычные участки
    for (i, j) in [(0, 0), (1, 0), (0, 1), (1, 1)]:
        _add_feature(layer, "{},{}".format(i, j),
                     [QgsGeometry.fromPolygonXY([cell_ring(i, j)])])

    # 2,0: полигон с дырой
    hole = _ring([(24, 4), (27, 4), (27, 8), (24, 8)])
    _add_feature(layer, "2,0", [QgsGeometry.fromPolygonXY(
        [cell_ring(2, 0), hole])])

    # 2,1: мультиполигон (основная часть + отдельно стоящая)
    far = _ring([(50, 0), (55, 0), (55, 5), (50, 5)])
    _add_feature(layer, "2,1", [QgsGeometry.fromPolygonXY([cell_ring(2, 1)]),
                                QgsGeometry.fromPolygonXY([far])])

    layer.updateExtents()
    for key, fid in _tag_map(layer).items():
        i, j = key.split(",")
        tags[(int(i), int(j))] = fid
    return layer, {"tags": tags}


def make_tjunction_layer(name="tjunction", crs="EPSG:3857"):
    """Пара полигонов с T-примыканием без узла.

    Угловая вершина B (5,10) лежит на верхнем ребре A ((0,10)-(20,10)),
    но у A вершины в этой точке нет. Используется в этапе 2 (автовставка)
    и сейчас — как документация текущего поведения.

    :return: (QgsVectorLayer, {"A": fid, "B": fid})
    """
    layer = _memory_polygon_layer(name, crs)
    _add_feature(layer, "A", [QgsGeometry.fromPolygonXY([_ring(
        [(0, 0), (20, 0), (20, 10), (0, 10)])])])
    _add_feature(layer, "B", [QgsGeometry.fromPolygonXY([_ring(
        [(5, 10), (25, 10), (25, 20), (5, 20)])])])
    layer.updateExtents()
    return layer, _tag_map(layer)


def make_grid_other_crs(nx, ny, cell=0.001, x0=30.0, y0=50.0,
                        name="grid_wgs84"):
    """Сетка в географической СК (EPSG:4326) — для тестов трансформации.

    Используется начиная с этапа 4 (правка слоёв в разных СК); в этапе 0
    фикстуру держим, чтобы приёмка этапа 4 опиралась на готовое.
    """
    return make_grid_layer(nx, ny, cell=cell, x0=x0, y0=y0,
                           name=name, crs="EPSG:4326")
