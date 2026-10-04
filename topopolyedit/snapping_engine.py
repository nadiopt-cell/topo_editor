# -*- coding: utf-8 -*-
"""Поисковик привязок (snapping) к узлам и рёбрам всех включенных слоёв.

Участвуют все векторные слои, отмеченные (включенные) в панели слоёв и
имеющие геометрию, а также редактируемый слой — даже если он выключен.
Полигональные, ЛИНЕЙНЫЕ и точечные слои. Поиск ограничен текущим
экстентом карты (запрос по габаритному прямоугольнику).

Приоритет прилипания: узел > ребро (узел побеждает, даже если ребро
ближе). «Строгий» снэп к узлам: радиус поиска узла БОЛЬШЕ радиуса
поиска ребра (VERTEX_TOLERANCE_PX > TOLERANCE_PX) — в узел эталонного
слоя легче попасть. Слои изменяться не могут — движок возвращает только
точки привязки (модифицируется лишь редактируемый слой).

Список слоёв строится РУЧНЫМ обходом дерева панели слоёв (по галочкам
видимости) с дополнительным источником checkedLayers(): раньше список
зависел только от checkedLayers(), и если метод недоступен или вёл себя
иначе в какой-то версии QGIS, все эталонные слои ТИХО выпадали из
прилипания (не работал снэп к узлам эталонов и к линиям).
"""

from qgis.core import (
    QgsProject, QgsFeatureRequest, QgsCoordinateTransform,
    QgsPointXY, QgsRectangle, QgsWkbTypes, QgsUnitTypes,
    QgsMapLayerType, QgsVectorLayer, QgsLayerTreeNode,
)

from .geo_utils import norm_polys, iter_ring_segments, iter_polyline_parts, point_to_segment

TOLERANCE_PX = 10.0            # радиус прилипания к рёбрам, пикселей
VERTEX_TOLERANCE_PX = 15.0     # радиус «строгого» прилипания к УЗЛАМ, px
MAX_FEATURES_PER_LAYER = 20000  # предохранитель от слишком тяжёлых запросов


def get_snap_layers(editable_layer=None):
    """Список слоёв-участников прилипания.

    Участвуют все ВКЛЮЧЕННЫЕ (галочка видимости в панели слоёв,
    включая вложенные группы) векторные слои с геометрией;
    редактируемый слой добавляется принудительно, даже если выключен.

    Дерево обходится вручную по itemVisibilityChecked(); checkedLayers()
    используется как дополнительный источник (страховка от различий
    версий QGIS — см. докстринг модуля).

    :param editable_layer: редактируемый слой (включается принудительно)
    :return: [QgsVectorLayer, ...]
    """
    layers = []
    seen = set()

    def _add(lyr):
        if lyr is None or not lyr.isValid() or lyr.id() in seen:
            return
        if lyr.type() != QgsMapLayerType.VectorLayer:
            return
        if lyr.geometryType() == QgsWkbTypes.NullGeometry:
            return
        layers.append(lyr)
        seen.add(lyr.id())

    root = QgsProject.instance().layerTreeRoot()

    # 1) ручной обход дерева: включённая группа раскрывает детей,
    #    выключенный узел (слой или группа) исключается целиком
    def _walk(node):
        for child in node.children():
            if not child.itemVisibilityChecked():
                continue
            if child.nodeType() == QgsLayerTreeNode.NodeLayer:
                _add(child.layer())
            else:
                _walk(child)

    try:
        _walk(root)
    except Exception:
        pass

    # 2) страховка: checkedLayers() как дополнительный источник
    try:
        for lyr in root.checkedLayers():
            _add(lyr)
    except Exception:
        pass

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
        """Радиус прилипания к рёбрам в единицах карты."""
        try:
            return self.canvas.mapSettings().convertToMapUnits(
                self.tolerance_px, QgsUnitTypes.RenderPixels)
        except Exception:
            return self.tolerance_px

    def vertex_tolerance_map_units(self):
        """Радиус «строгого» прилипания к узлам в единицах карты."""
        try:
            return self.canvas.mapSettings().convertToMapUnits(
                VERTEX_TOLERANCE_PX, QgsUnitTypes.RenderPixels)
        except Exception:
            return VERTEX_TOLERANCE_PX

    # ------------------------------------------------------------------
    @staticmethod
    def _pick(best_v, best_e):
        """Выбор итоговой привязки: узел ВСЕГДА важнее ребра."""
        if best_v is not None:
            return SnapResult(best_v[1], "vertex", best_v[2], best_v[3])
        if best_e is not None:
            return SnapResult(best_e[1], "edge", best_e[2], best_e[3],
                              best_e[4])
        return None

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
        tol_v = self.vertex_tolerance_map_units()
        tol2 = tol * tol
        tol_v2 = tol_v * tol_v
        reach = max(tol, tol_v)
        rect_map = QgsRectangle(map_pt.x() - reach, map_pt.y() - reach,
                                map_pt.x() + reach, map_pt.y() + reach)
        best_v = None   # (d2, point, layer, fid)
        best_e = None   # (d2, proj, layer, fid, (a, b))

        for lyr in get_snap_layers(editable_layer):
            try:
                res = self._snap_layer(lyr, map_pt, rect_map, dest,
                                       exclude, tol2, tol_v2)
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

        return self._pick(best_v, best_e)

    # ------------------------------------------------------------------
    def _snap_layer(self, lyr, map_pt, rect_map, dest_crs, exclude,
                    tol2, tol_v2=None):
        """Поиск по одному слою. Возвращает (best_vertex, best_edge).

        :param tol2: квадрат допуска для рёбер
        :param tol_v2: квадрат допуска для узлов (строгий снэп —
                       обычно больше tol2); None => равен tol2
        """
        if tol_v2 is None:
            tol_v2 = tol2
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
                            if n > 1 and ring[0].sqrDist(ring[-1]) <= 1e-12:
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
                d2 = map_pt.sqrDist(p)
                if d2 <= tol_v2 and (best_v is None or d2 < best_v[0]):
                    best_v = (d2, p, lyr, feat.id())

            for a, b in segs:
                proj, d2, _t = point_to_segment(map_pt, a, b)
                if d2 <= tol2 and (best_e is None or d2 < best_e[0]):
                    best_e = (d2, proj, lyr, feat.id(), (a, b))

        return best_v, best_e
