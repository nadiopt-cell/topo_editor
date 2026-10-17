# -*- coding: utf-8 -*-
"""Проверка SLIVER: сливер — площадь / периметр² ниже порога (этап 5 ТЗ).

Для многочастевой геометрии периметр — сумма длин всех колец всех
частей (включая дыры). Исправления нет — только показ.
"""

from qgis.core import QgsWkbTypes

from .base import (
    Finding, CODE_SLIVER, _cancelled, _progress, _opts, _polys, _geoms,
    polys_perimeter, geom_area,
)


class SliverCheck(object):
    """area / perimeter² < SLIVER_RATIO (по умолчанию 1e-4)."""

    CODE = CODE_SLIVER

    def run(self, ctx):
        out = []
        ratio = float(_opts(ctx, "sliver_ratio") or 0.0)
        if ratio <= 0.0:
            return out
        geoms = _geoms(ctx)
        total = max(len(geoms), 1)
        for i, (fid, g) in enumerate(geoms):
            if _cancelled(ctx):
                return out
            if g is None or g.isNull() or g.isEmpty():
                continue
            if g.type() != QgsWkbTypes.PolygonGeometry:
                continue
            area = geom_area(g)
            if area <= 0.0:
                continue
            polys = _polys(ctx, fid, g)
            perim = polys_perimeter(polys)
            if perim <= 0.0:
                continue
            k = area / (perim * perim)
            if k < ratio:
                out.append(self._finding(ctx, fid, g, area, k))
            _progress(ctx, (i + 1) / float(total),
                      u"SLIVER: проверка формы")
        return out

    @staticmethod
    def _finding(ctx, fid, g, area, k):
        pt = None
        try:
            v = g.vertexAt(0)  # первая вершина как ориентир места
            if v is not None:
                import math
                if not (math.isnan(v.x()) or math.isnan(v.y())):
                    from qgis.core import QgsPointXY
                    pt = QgsPointXY(v.x(), v.y())
        except Exception:
            pt = None
        return Finding(
            CODE_SLIVER, fid=fid, point=pt, area=area,
            message=u"сливер: площадь/периметр² = {:.3g} < {:.3g}"
                    u" (площадь {:g})".format(k, _ratio(ctx), area),
            fixable=False, payload={},
            layer_id=ctx.get("layer_id", u""),
            layer_name=ctx.get("layer_name", u""))


def _ratio(ctx):
    from .base import _opts
    try:
        return float(_opts(ctx, "sliver_ratio") or 0.0)
    except Exception:
        return 0.0
