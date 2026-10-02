# -*- coding: utf-8 -*-
"""Поисковик привязок (snapping) к узлам и рёбрам всех включенных слоёв.

Участвуют все векторные слои, отмеченные (включенные) в панели слоёв и
имеющие геометрию, а также редактируемый слой — даже если он выключен.
Поиск ограничен текущим экстентом карты (запрос по габаритному прямоугольнику).

Приоритет прилипания: узел > ребро. Слои изменяться не могут — движок
возвращает только точки привязки (модифицируется лишь редактируемый слой).
"""

from qgis.core import (
    QgsProject, QgsFeatureRequest, QgsCoordinateTransform,
    QgsPointXY, QgsRectangle, QgsWkbTypes, QgsUnitTypes,
    QgsMapLayerType, QgsVectorLayer,
)

from .geo_utils import norm_polys, iter_ring_segments, iter_polyline_parts, point_to_segment

TOLERANCE_PX = 10.0            # радиус прилипания, пикселей
MAX_FEATURES_PER_LAYER = 20000  # предохранитель от слишком тяжёлых запросов


def get_snap_layers(editable_layer=None):
    """Список слоёв-участников прилипания.

    :param editable_layer: редактируемый слой (включается принудительно)
    :return: [QgsVectorLayer, ...]
    """
    layers = []
    seen = set()
    root = QgsProject.instance().layerTreeRoot()
    try:
        checked = root.checkedLayers()
    except Exception:
        checked = []
    for lyr in checked:
        if lyr is None or not lyr.isValid() or lyr.id() in seen:
            continue
        if lyr.type() != QgsMapLayerType.VectorLayer:
            continue
        if lyr.geometryType() == QgsWkbTypes.NullGeometry:
            continue
        layers.append(lyr)
        seen.add(lyr.id())
    if editable_layer is not None and editable_layer.isValid() \
            and editable_layer.id() not in seen:
        layers.insert(0, editable_layer)
    return layers


class SnapResult(object):
    """Результат прилипания (точка в CRS карты)."""

    __slots__ = ("point", "snap_type", "layer", "fid", "segment")

    def __init__(self, point, snap_type, layer=None, fid=None, segment=None):
        self.point = point            # QgsPointXY (CRS карты)
        self.snap_type = snap_type    # "vertex" | "edge"
        self.layer = layer            # QgsVectorLayer или None
        self.fid = fid                # ID фичи или None
        self.segment = segment        # (a, b) QgsPointXY для рёбер


class SnappingEngine(object):
    """Движок прилипания к узлам и рёбрам включенных слоёв в экстенте."""

    def __init__(self, canvas, tolerance_px=TOLERANCE_PX):
        self.canvas = canvas
        self.tolerance_px = float(tolerance_px)

    # ------------------------------------------------------------------
    def tolerance_map_units(self):
        """Радиус прилипания в единицах карты."""
        try:
            return self.canvas.mapSettings().convertToMapUnits(
                self.tolerance_px, QgsUnitTypes.RenderPixels)
        except Exception:
            return self.tolerance_px

    # ------------------------------------------------------------------
    def snap(self, map_pt, exclude=None, editable_layer=None):
        """Поиск привязки для точки в CRS карты.

        :param map_pt: QgsPointXY — позиция курсора (CRS карты)
        :param exclude: множество (layer_id, fid) — фичи, к которым
                        прилипать нельзя (например, перемещаемые)
        :param editable_layer: редактируемый слой (гарантированно участвует)
        :return: SnapResult или None
        """
        dest = self.canvas.mapSettings().destinationCrs()
        tol = self.tolerance_map_units()
        tol2 = tol * tol
        rect_map = QgsRectangle(map_pt.x() - tol, map_pt.y() - tol,
                                map_pt.x() + tol, map_pt.y() + tol)
        best_v = None   # (d2, point, layer, fid)
        best_e = None   # (d2, proj, layer, fid, (a, b))

        for lyr in get_snap_layers(editable_layer):
            try:
                res = self._snap_layer(lyr, map_pt, rect_map, dest,
                                       exclude, tol2)
            except Exception:
                continue
            if res is None:
                continue
            v = res[0]
            e = res[1]
            if v is not None and (best_v is None or v[0] < best_v[0]):
                best_v = v
            if e is not None and (best_e is None or e[0] < best_e[0]):
                best_e = e

        if best_v is not None:
            return SnapResult(best_v[1], "vertex", best_v[2], best_v[3])
        if best_e is not None:
            return SnapResult(best_e[1], "edge", best_e[2], best_e[3],
                              best_e[4])
        return None

    # ------------------------------------------------------------------
    def _snap_layer(self, lyr, map_pt, rect_map, dest_crs, exclude, tol2):
        """Поиск по одному слою. Возвращает (best_vertex, best_edge)."""
        same = lyr.crs().authid() == dest_crs.authid()
        ct = None
        if same:
            rect_l = rect_map
        else:
            try:
                ct_m2l = QgsCoordinateTransform(dest_crs, lyr.crs(),
                                                QgsProject.instance())
                rect_l = ct_m2l.transformBoundingBox(rect_map)
                ct = QgsCoordinateTransform(lyr.crs(), dest_crs,
                                            QgsProject.instance())
            except Exception:
                return None, None

        req = QgsFeatureRequest(rect_l)
        req.setSubsetOfAttributes([])
        best_v = None
        best_e = None
        cnt = 0
        gtype = lyr.geometryType()

        for feat in lyr.getFeatures(req):
            cnt += 1
            if cnt > MAX_FEATURES_PER_LAYER:
                break
            if exclude is not None and (lyr.id(), feat.id()) in exclude:
                continue
            g = feat.geometry()
            if g is None or g.isNull() or g.isEmpty():
                continue
            if ct is not None:
                try:
                    g = QgsGeometry(g)
                    if g.transform(ct) != 0:
                        continue
                except Exception:
                    continue

            # --- кандидаты-вершины и сегменты по типу геометрии ---
            pts = []
            segs = []
            if gtype == QgsWkbTypes.PolygonGeometry:
                nr = norm_polys(g)
                if nr:
                    for part in nr[0]:
                        for ring in part:
                            n = len(ring)
                            if n > 1 and ring[0].sqrdist(ring[-1]) <= 1e-12:
                                pts.extend(ring[:-1])
                            else:
                                pts.extend(ring)
                            for _si, a, b in iter_ring_segments(ring):
                                segs.append((a, b))
            elif gtype == QgsWkbTypes.LineGeometry:
                for line in iter_polyline_parts(g):
                    pts.extend(line)
                    for i in range(len(line) - 1):
                        segs.append((line[i], line[i + 1]))
            else:  # точечная геометрия
                if g.isMultipart():
                    pts.extend(g.asMultiPoint())
                else:
                    p = g.asPoint()
                    if p is not None:
                        pts.append(QgsPointXY(p))

            for p in pts:
                d2 = map_pt.sqrdist(p)
                if d2 <= tol2 and (best_v is None or d2 < best_v[0]):
                    best_v = (d2, p, lyr, feat.id())

            for a, b in segs:
                proj, d2, _t = point_to_segment(map_pt, a, b)
                if d2 <= tol2 and (best_e is None or d2 < best_e[0]):
                    best_e = (d2, proj, lyr, feat.id(), (a, b))

        return best_v, best_e
