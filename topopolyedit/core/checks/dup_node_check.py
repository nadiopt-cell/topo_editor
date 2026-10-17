# -*- coding: utf-8 -*-
"""Проверка DUP_NODE: две подряд идущие вершины в пределах допуска
(этап 5 ТЗ).

Допуск — топологический допуск слоя match_eps (совпадение узлов;
TOPO_MATCH_EPS = 1e-6 ед. слоя). Замыкающая точка кольца (копия первой
вершины в конце) дубликатом НЕ считается — проверяются только сегменты
между соседними вершинами.

Исправление payload {"pos": (x, y)} — фикс удаляет ВСЕ подряд идущие
дубли кольца (см. fixes.remove_consecutive_dups): находка ставится на
первую пару, чистятся все — повторный прогон после «Исправить все»
даёт 0.
"""

from qgis.core import QgsPointXY

from .base import (
    Finding, CODE_DUP_NODE, _cancelled, _progress, _opts, _polys, _geoms,
    _iter_rings,
)


class DupNodeCheck(object):
    """Сегменты кольца короче match_eps (включая дыры и части)."""

    CODE = CODE_DUP_NODE

    def run(self, ctx):
        out = []
        eps = float(_opts(ctx, "match_eps") or 0.0)
        if eps <= 0.0:
            return out
        eps2 = eps * eps
        geoms = _geoms(ctx)
        total = max(len(geoms), 1)
        for i, (fid, g) in enumerate(geoms):
            if _cancelled(ctx):
                return out
            polys = _polys(ctx, fid, g)
            for pi, ri, ring in _iter_rings(polys):
                n = len(ring)
                if n < 4:
                    continue  # минимальное замкнутое кольцо
                # сравниваются ВСЕ пары соседних вершин (i, i+1),
                # i = 0..n-2: сегмент (n-2, n-1) — последний перед
                # замыкающей копией первой вершины, и он тоже честный
                # сегмент кольца (дубль «последняя реальная ≈ первая»
                # — ошибка). Сама замыкающая копия пары не образует.
                for si in range(n - 1):
                    a, b = ring[si], ring[si + 1]
                    if a.sqrDist(b) <= eps2:
                        out.append(self._finding(ctx, fid, a, b))
                        break  # одна находка на кольцо; чистятся все
            _progress(ctx, (i + 1) / float(total),
                      u"DUP_NODE: проверка дублей вершин")
        return out

    @staticmethod
    def _finding(ctx, fid, a, b):
        pos = (a.x(), a.y())
        return Finding(
            CODE_DUP_NODE, fid=fid, point=QgsPointXY(a), area=None,
            message=u"две подряд идущие вершины в пределах допуска "
                    u"({:.6g}, {:.6g})".format(b.x(), b.y()),
            fixable=True, payload={"pos": pos},
            layer_id=ctx.get("layer_id", u""),
            layer_name=ctx.get("layer_name", u""))
