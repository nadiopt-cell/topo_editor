"""Адаптер между QGIS и чистым ядром графа (QGIS 3.x)."""
from qgis.core import (
    QgsFeatureRequest,
    QgsGeometry,
    QgsPointXY,
    QgsRectangle,
    QgsWkbTypes,
)

# Совместимость: в 3.30+ появился Qgis.GeometryType, старые константы устарели
try:
    from qgis.core import Qgis

    POLYGON = Qgis.GeometryType.Polygon
    LINE = Qgis.GeometryType.Line
    POINT = Qgis.GeometryType.Point
except (ImportError, AttributeError):
    POLYGON = QgsWkbTypes.PolygonGeometry
    LINE = QgsWkbTypes.LineGeometry
    POINT = QgsWkbTypes.PointGeometry


def geometry_parts(geom):
    """QgsGeometry -> список частей; часть = список колец; кольцо = [(x, y), ...].

    Кривые линеаризуются средствами asPolygon/asMultiPolygon (для прототипа).
    """
    if geom is None or geom.isNull():
        return []
    polys = geom.asMultiPolygon() if geom.isMultipart() else [geom.asPolygon()]
    return [[[(p.x(), p.y()) for p in ring] for ring in poly] for poly in polys if poly]


def pick_feature(layer, point, radius):
    """Ближайший к точке объект в радиусе (point и radius - в СК слоя)."""
    rect = QgsRectangle(
        point.x() - radius, point.y() - radius, point.x() + radius, point.y() + radius
    )
    pt_geom = QgsGeometry.fromPointXY(point)
    best, best_d = None, None
    for f in layer.getFeatures(QgsFeatureRequest().setFilterRect(rect)):
        g = f.geometry()
        if g.isNull():
            continue
        d = g.distance(pt_geom)
        if d <= radius and (best_d is None or d < best_d):
            best, best_d = f, d
    return best


def collect_neighborhood(layer, seed, tolerance, depth=2, reach=None):
    """Объект + соседи на `depth` шагов.

    Возвращает {fid: (feature, level)}: level 0 - выбранный объект,
    1 - прямые соседи (будут редактироваться), 2 - контекст (нужен только,
    чтобы узлы на краю окрестности определялись правильно).
    reach - на каком расстоянии объект считается соседом (по умолчанию 2*допуск;
    для привязки к вершинам через щель его увеличивают до радиуса привязки).
    """
    found = {seed.id(): (seed, 0)}
    frontier = [seed]
    if reach is None:
        reach = tolerance * 2
    for level in range(1, depth + 1):
        nxt = []
        for f in frontier:
            geom = f.geometry()
            rect = geom.boundingBox()
            rect.grow(reach)
            for g in layer.getFeatures(QgsFeatureRequest().setFilterRect(rect)):
                if g.id() in found or g.geometry().isNull():
                    continue
                if g.geometry().distance(geom) <= reach:
                    found[g.id()] = (g, level)
                    nxt.append(g)
        frontier = nxt
    return found


def faces_from_features(found):
    """{fid: (feature, level)} -> faces для build_graph и {fid: level}."""
    faces, levels = {}, {}
    for fid, (feat, level) in found.items():
        for part_idx, rings in enumerate(geometry_parts(feat.geometry())):
            faces[(fid, part_idx)] = rings
        levels[fid] = level
    return faces, levels


def polylines_to_geometry(polylines):
    return QgsGeometry.fromMultiPolylineXY(
        [[QgsPointXY(x, y) for x, y in pl] for pl in polylines]
    )


def points_to_geometry(points):
    return QgsGeometry.fromMultiPointXY([QgsPointXY(x, y) for x, y in points])


def collect_extra_faces(layer, known_fids, bbox):
    """Объекты слоя в bbox, не входящие в граф (для проверки пересечений)."""
    extra = {}
    rect = QgsRectangle(*bbox)
    for f in layer.getFeatures(QgsFeatureRequest().setFilterRect(rect)):
        if f.id() in known_fids or f.geometry().isNull():
            continue
        for part_idx, rings in enumerate(geometry_parts(f.geometry())):
            extra[("extra", f.id(), part_idx)] = rings
    return extra


def vertex_offsets(parts):
    """{(часть, кольцо): индекс первой вершины кольца в QgsGeometry}."""
    offsets, n = {}, 0
    for pi, rings in enumerate(parts):
        for ri, ring in enumerate(rings):
            offsets[(pi, ri)] = n
            n += len(ring)
    return offsets
