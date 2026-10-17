# -*- coding: utf-8 -*-
"""Проверка SPIKE: «шип» — угол при вершине меньше порога (этап 5 ТЗ).

Логика распознавания ровно та же, что у правила NoSpikes валидатора
этапа 1 (общий метод _check_ring): совпадающие подряд вершины дают
DUP_VERTEX и угол на нулевом отрезке не считается шипом. Исправление —
удалить вершину ВО ВСЕХ полигонах, где она есть: payload {"pos"} —
позиция шипа (фикс чистит все копии узла в слое).
"""

from qgis.core import QgsPointXY, QgsWkbTypes

from ..validator import NoSpikes
from .base import (
    Finding, CODE_SPIKE, _cancelled, _progress, _opts, _polys, _geoms,
)


class SpikeCheck(object):
    """Угол при вершине меньше SPIKE_MIN_ANGLE_DEG во всех кольцах."""

    CODE = CODE_SPIKE

    def run(self, ctx):
        out = []
        min_angle = float(_opts(ctx, "spike_min_angle_deg") or 0.5)
        geoms = _geoms(ctx)
        total = max(len(geoms), 1)
        for i, (fid, g) in enumerate(geoms):
            if _cancelled(ctx):
                return out
            if g is None or g.isNull() or g.isEmpty():
                continue
            if g.type() != QgsWkbTypes.PolygonGeometry:
                continue
            try:
                polys = (g.asMultiPolygon() if g.isMultipart()
                         else [g.asPolygon()])
            except Exception:
                continue
            seen = []
            for poly in polys:
                for ring in poly:
                    for v in NoSpikes._check_ring(fid, ring, min_angle,
                                                  "error"):
                        if v.code != "spike" or v.point is None:
                            continue  # дубли вершин — код DUP_NODE
                        pos = (v.point.x(), v.point.y())
                        if any(abs(pos[0] - s[0]) <= 1e-12
                               and abs(pos[1] - s[1]) <= 1e-12
                               for s in seen):
                            continue
                        seen.append(pos)
                        out.append(self._finding(ctx, fid, v, ring))
            _progress(ctx, (i + 1) / float(total),
                      u"SPIKE: проверка углов")
        return out

    @staticmethod
    def _finding(ctx, fid, v, ring=None):
        # площадь треугольника (prev, остриё, next) — ровно столько
        # площади теряет полигон при удалении шипа; движок исправлений
        # ослабляет порог «шва» на эту величину (см. layer_audit.py)
        spike_area = 0.0
        try:
            tip = v.point
            pts = list(ring or [])
            n = len(pts)
            for vi in range(n):
                if not _near(pts[vi], tip):
                    continue
                prev_p = pts[(vi - 1) % n]
                next_p = pts[(vi + 1) % n]
                if _near(prev_p, tip) or _near(next_p, tip):
                    continue  # дубль рядом — площадь не определена
                v1x = prev_p.x() - tip.x()
                v1y = prev_p.y() - tip.y()
                v2x = next_p.x() - tip.x()
                v2y = next_p.y() - tip.y()
                spike_area = 0.5 * abs(v1x * v2y - v1y * v2x)
                break
        except Exception:
            spike_area = 0.0
        return Finding(
            CODE_SPIKE, fid=fid, point=QgsPointXY(v.point), area=None,
            message=v.message or u"шип: угол меньше порога",
            fixable=True,
            payload={"pos": (v.point.x(), v.point.y()),
                     "spike_area": float(spike_area)},
            layer_id=ctx.get("layer_id", u""),
            layer_name=ctx.get("layer_name", u""))


def _near(a, b):
    try:
        return a.sqrDist(b) <= 1e-18
    except Exception:
        return False
