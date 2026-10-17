# -*- coding: utf-8 -*-
"""Проверка T_JUNCTION: узел на чужом ребре без вершины (этап 5 ТЗ).

Для каждой вершины каждой фичи ищутся ЧУЖИЕ рёбра, на которых она
лежит строго внутри (find_t_edges из geo_utils — та же механика, что у
правила NoTJunction и конвейера T-залатывания этапа 2).

Исправление — вставить вершину в ребро (insert_vertex_topo): payload
хранит точку и список {(fid, p1, p2)} — ВСЕХ фич с ребром под точкой,
чтобы после исправления T-примыкание исчезло на всей общей границе
(ребро может принадлежать нескольким фичам).
"""

from qgis.core import QgsPointXY, QgsRectangle

from ...geo_utils import find_t_edges
from .base import (
    Finding, CODE_T_JUNCTION, _cancelled, _progress, _opts, _polys,
    _geoms, candidate_fids,
)


class TJunctionCheck(object):
    """Вершина фичи лежит на чужом ребре без вершины там."""

    CODE = CODE_T_JUNCTION

    def run(self, ctx):
        out = []
        eps = float(_opts(ctx, "match_eps") or 1e-6)
        eps2 = eps * eps
        geoms = _geoms(ctx)
        lookup = dict(geoms)
        total = max(len(geoms), 1)
        for i, (fid, g) in enumerate(geoms):
            if _cancelled(ctx):
                return out
            polys = _polys(ctx, fid, g)
            seen_pts = set()
            for part in polys:
                for ring in part:
                    n = len(ring)
                    for vi in range(n):
                        # замыкающую копию пропускаем — совпадает с [0]
                        if vi == n - 1 and n > 1 \
                                and ring[vi].sqrDist(ring[0]) <= eps2:
                            continue
                        pt = ring[vi]
                        key = (round(pt.x(), 9), round(pt.y(), 9))
                        if key in seen_pts:
                            continue
                        seen_pts.add(key)
                        out.extend(self._check_point(
                            ctx, fid, pt, lookup, eps2))
            _progress(ctx, (i + 1) / float(total),
                      u"T_JUNCTION: проверка примыканий")
        return out

    def _check_point(self, ctx, fid, pt, lookup, eps2):
        """Находка для вершины pt фичи fid: чужие рёбра под точкой."""
        rect = _point_rect(pt, eps2)
        hits = []
        for nfid in candidate_fids(ctx, rect, exclude_fid=fid):
            g = lookup.get(nfid)
            if g is None:
                continue
            try:
                npolys = _polys(ctx, nfid, g)
            except Exception:
                continue
            if not npolys:
                continue
            for a, b in find_t_edges(npolys, pt, eps2):
                hits.append((nfid, a, b))
        if not hits:
            return []
        # одна находка на точку: fid первой фичи с ребром (для таблицы),
        # в payload — ВСЕ фичи с ребром под точкой (вставка во все);
        # insert_vertex_topo сам не вставляет дубль в кольцо
        payload = {
            "pos": (pt.x(), pt.y()),
            "edges": [(f, (a.x(), a.y()), (b.x(), b.y()))
                      for f, a, b in hits],
        }
        f0 = hits[0][0]
        return [Finding(
            CODE_T_JUNCTION, fid=f0, point=QgsPointXY(pt), area=None,
            message=u"узел ({:.6g}, {:.6g}) лежит на ребре фичи #{} "
                    u"без вершины".format(pt.x(), pt.y(), f0),
            fixable=True, payload=payload,
            layer_id=ctx.get("layer_id", u""),
            layer_name=ctx.get("layer_name", u""))]


def _point_rect(pt, eps):
    """Габарит вокруг точки с запасом допуска (для запроса индекса)."""
    return QgsRectangle(pt.x() - eps * 2.0, pt.y() - eps * 2.0,
                        pt.x() + eps * 2.0, pt.y() + eps * 2.0)
