# -*- coding: utf-8 -*-
"""Проверка OVERLAP: перехлёст двух полигонов одного слоя (этап 5 ТЗ).

Пары ищутся по пространственному индексу габаритов (без него —
перебор), перехлёст считается при площади пересечения больше
OVERLAP_MIN_RATIO × площадь меньшего из пары (та же семантика, что у
find_overlaps и правила NoSameLayerOverlap).

Исправление — обрезать меньший / больший / вручную: payload хранит
обе площади и геометрию пересечения; выбор делает диалог панели
(decider) или вызывающий код.
"""

from qgis.core import QgsPointXY, QgsRectangle

from .base import (
    Finding, CODE_OVERLAP, _cancelled, _progress, _opts, _geoms,
    candidate_fids, geom_area,
)


class OverlapCheck(object):
    """Все пары фич слоя с площадью пересечения выше порога."""

    CODE = CODE_OVERLAP

    def run(self, ctx):
        out = []
        ratio = float(_opts(ctx, "overlap_min_ratio") or 1e-8)
        geoms = dict()
        items = []
        for fid, g in _geoms(ctx):
            if g is None or g.isNull() or g.isEmpty():
                continue
            items.append((fid, g))
            geoms[fid] = g
        total = max(len(items), 1)
        for i, (fid, g) in enumerate(items):
            if _cancelled(ctx):
                return out
            bbox = g.boundingBox()
            area_a = geom_area(g)
            if area_a <= 0.0:
                continue
            for nfid in sorted(candidate_fids(ctx, bbox)):
                if nfid <= fid or nfid == fid:
                    continue  # каждая пара один раз, по возрастанию fid
                ng = geoms.get(nfid)
                if ng is None:
                    continue
                try:
                    if not g.intersects(ng):
                        continue
                    inter = g.intersection(ng)
                except Exception:
                    continue
                if inter is None or inter.isNull() or inter.isEmpty():
                    continue
                area = _polygon_area(inter)
                area_b = geom_area(ng)
                if area <= 0.0 or area <= ratio * min(area_a, area_b):
                    continue
                out.append(self._finding(ctx, fid, nfid, inter,
                                         area, area_a, area_b))
            _progress(ctx, (i + 1) / float(total),
                      u"OVERLAP: проверка перехлёстов")
        return out

    @staticmethod
    def _finding(ctx, fid_a, fid_b, inter, area, area_a, area_b):
        pt = _rep_point(inter)
        smaller, larger = ((fid_a, fid_b) if area_a <= area_b
                           else (fid_b, fid_a))
        return Finding(
            CODE_OVERLAP, fid=fid_a, point=pt, area=area,
            message=u"перехлёст #{} и #{}: площадь {:g}".format(
                fid_a, fid_b, area),
            fixable=True,
            payload={"a_fid": fid_a, "b_fid": fid_b,
                     "area_a": area_a, "area_b": area_b,
                     "smaller": smaller, "larger": larger},
            layer_id=ctx.get("layer_id", u""),
            layer_name=ctx.get("layer_name", u""))


def _rep_point(g):
    """Точка-ориентир геометрии: pointOnSurface, иначе центроид габарита."""
    try:
        p = g.pointOnSurface()
        if p is not None:
            import math
            if not (math.isnan(p.x()) or math.isnan(p.y())):
                return QgsPointXY(p)
    except Exception:
        pass
    try:
        r = QgsRectangle(g.boundingBox())
        return QgsPointXY(r.center())
    except Exception:
        return None


def _polygon_area(g):
    """Площадь ПОЛИГОНАЛЬНОЙ части пересечения (линия касания — 0)."""
    from qgis.core import QgsWkbTypes
    try:
        if g.type() != QgsWkbTypes.PolygonGeometry:
            return 0.0
        return float(g.area())
    except Exception:
        return 0.0
