# -*- coding: utf-8 -*-
"""Проверка NEAR_NODE: узлы разных полигонов в пределах допуска, но не
совпадающие (этап 5 ТЗ).

Узлы собираются в кластеры (union-find) по парам «расстояние в
NEAR_NODE_TOL_M, но больше допуска совпадения match_eps» — совпадающие
узлы (общие узлы топологии) кластером не считаются.

Исправление — слив кластера в его центроид ТОЛЬКО если безопасно
(ТЗ): каждое затронутое ребро каждого узла кластера короче 2× допуска
ИЛИ коллинеарно продолжению — ребру другого узла кластера (параллельно
с отступом не больше допуска). Иначе — только показ: слив одной пары
может изогнуть длинную общую границу.

Копии узла в других фичах двигаются вместе (replace_vertices по всему
слою) — как штатное топологическое перемещение узла.
"""

import math

from qgis.core import QgsPointXY

from .base import (
    Finding, CODE_NEAR_NODE, _cancelled, _progress, _opts, _polys,
    _geoms,
)

# допуск угла «коллинеарно»: |sin| между направлениями рёбер
_PARALLEL_SIN = 1e-3  # ~0.057°


class NearNodeCheck(object):
    """Кластеры почти совпавших узлов разных полигонов."""

    CODE = CODE_NEAR_NODE

    def run(self, ctx):
        out = []
        tol = float(_opts(ctx, "near_node_tol") or 0.0)
        match = float(_opts(ctx, "match_eps") or 1e-6)
        if tol <= 0.0:
            return out
        if tol <= match:
            tol = match * 10.0  # защита от перепутанных настроек
        # 1) узлы слоя: позиция -> (набор fid, счётчик)
        positions = self._collect_positions(ctx, match)
        # 2) кластеры: пары «match < dist <= tol» между РАЗНЫМИ
        #    полигонами (наборы fid не пересекаются)
        clusters = self._clusters(positions, tol, match)
        total = max(len(clusters), 1)
        for i, (pos_keys, pair_list) in enumerate(clusters):
            if _cancelled(ctx):
                return out
            edges = self._attached_edges(ctx, pos_keys, positions)
            safe, reason = self._cluster_safe(pos_keys, edges, tol)
            out.append(self._finding(ctx, pos_keys, positions,
                                     pair_list, safe, reason, edges))
            _progress(ctx, (i + 1) / float(total),
                      u"NEAR_NODE: проверка почти совпавших узлов")
        return out

    # ------------------------------------------------------------------
    # Сбор узлов
    # ------------------------------------------------------------------

    def _collect_positions(self, ctx, match):
        """{(x, y): {"fids": set, "pt": QgsPointXY}} — вершины слоя,
        совпадающие в пределах match_eps слиты в одну позицию."""
        eps2 = match * match
        cell = max(match, 1e-9)
        grid = {}
        positions = {}
        for fid, g in _geoms(ctx):
            polys = _polys(ctx, fid, g)
            for part in polys:
                for ring in part:
                    n = len(ring)
                    for vi in range(n):
                        if vi == n - 1 and n > 1 \
                                and ring[vi].sqrDist(ring[0]) <= eps2:
                            continue  # замыкающая копия
                        self._add_point(ring[vi], fid, positions,
                                        grid, cell, eps2)
        return positions

    def _add_point(self, pt, fid, positions, grid, cell, eps2):
        cx = int(math.floor(pt.x() / cell))
        cy = int(math.floor(pt.y() / cell))
        key = None
        for gx in (cx - 1, cx, cx + 1):
            for gy in (cy - 1, cy, cy + 1):
                for k in grid.get((gx, gy), ()):
                    p = positions[k]["pt"]
                    if pt.sqrDist(p) <= eps2:
                        key = k
                        break
                if key is not None:
                    break
            if key is not None:
                break
        if key is None:
            key = (pt.x(), pt.y())
            positions[key] = {"pt": QgsPointXY(pt), "fids": set()}
            grid.setdefault((cx, cy), []).append(key)
        positions[key]["fids"].add(fid)

    # ------------------------------------------------------------------
    # Кластеры почти совпавших узлов
    # ------------------------------------------------------------------

    def _clusters(self, positions, tol, match):
        """Список кластеров: [(ключи позиций, [(k1, k2), ...]), ...].

        Кластер — связная компонента графа пар «match < dist <= tol и
        наборы fid не пересекаются» (union-find)."""
        keys = sorted(positions.keys())
        n = len(keys)
        parent = list(range(n))
        index_of = {k: i for i, k in enumerate(keys)}
        pair_list = []
        cell = max(tol, 1e-9)

        def find(a):
            while parent[a] != a:
                parent[a] = parent[parent[a]]
                a = parent[a]
            return a

        def union(a, b):
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[rb] = ra

        grid = {}
        for i, k in enumerate(keys):
            p = positions[k]["pt"]
            cx = int(math.floor(p.x() / cell))
            cy = int(math.floor(p.y() / cell))
            grid.setdefault((cx, cy), []).append(i)
        tol2 = tol * tol
        match2 = match * match
        for i, k in enumerate(keys):
            p = positions[k]["pt"]
            fids_p = positions[k]["fids"]
            cx = int(math.floor(p.x() / cell))
            cy = int(math.floor(p.y() / cell))
            for gx in (cx - 1, cx, cx + 1):
                for gy in (cy - 1, cy, cy + 1):
                    for j in grid.get((gx, gy), []):
                        if j <= i:
                            continue
                        k2 = keys[j]
                        fids_q = positions[k2]["fids"]
                        if fids_p & fids_q:
                            continue  # узлы одного полигона
                        d2 = p.sqrDist(positions[k2]["pt"])
                        if d2 <= match2 or d2 > tol2:
                            continue
                        union(i, j)
                        pair_list.append((k, k2))
        groups = {}
        for i, k in enumerate(keys):
            groups.setdefault(find(i), []).append(k)
        out = []
        for root, members in sorted(groups.items()):
            if len(members) < 2:
                continue
            pairs = [pr for pr in pair_list
                     if pr[0] in members and pr[1] in members]
            out.append((tuple(members), pairs))
        return out

    # ------------------------------------------------------------------
    # Правило безопасности слияния (ТЗ этап 5)
    # ------------------------------------------------------------------

    def _cluster_safe(self, pos_keys, edges, tol):
        """(safe: bool, причина) — у каждого узла кластера каждое
        присоединённое ребро либо короче 2× допуска, либо коллинеарно
        ребру другого узла кластера (параллельно с отступом <= tol)."""
        short2 = (2.0 * tol) * (2.0 * tol)
        for pos_key, lst in edges.items():
            for (u, v) in lst:
                d2 = u.sqrDist(v)
                if d2 <= short2:
                    continue  # короткое ребро — искажение ограничено
                ok = False
                for other_key in pos_keys:
                    if other_key == pos_key:
                        continue
                    for (u2, v2) in edges.get(other_key, []):
                        if _edges_aligned(u, v, u2, v2, tol):
                            ok = True
                            break
                    if ok:
                        break
                if not ok:
                    return False, (u"длинное ребро ({:.3g}) без "
                                   u"коллинеарного продолжения"
                                   .format(math.sqrt(d2)))
        return True, u""

    def _attached_edges(self, ctx, pos_keys, positions):
        """{ключ позиции: [(a, b), ...]} — все рёбра, входящие в узел,
        по всем фичам, где этот узел есть."""
        match = float(_opts(ctx, "match_eps") or 1e-6)
        eps2 = match * match
        wanted = {}
        for k in pos_keys:
            wanted[k] = positions[k]["pt"]
        edges = {k: [] for k in pos_keys}
        fids = set()
        for k in pos_keys:
            fids |= positions[k]["fids"]
        lookup = dict(_geoms(ctx))
        for fid in sorted(fids):
            g = lookup.get(fid)
            if g is None:
                continue
            polys = _polys(ctx, fid, g)
            for part in polys:
                for ring in part:
                    pts = _open_ring(ring, eps2)
                    m = len(pts)
                    if m < 3:
                        continue
                    for vi in range(m):
                        for k, pt in wanted.items():
                            if pts[vi].sqrDist(pt) > eps2:
                                continue
                            prev_v = pts[vi - 1]
                            next_v = pts[(vi + 1) % m]
                            if prev_v.sqrDist(pt) > eps2:
                                edges[k].append((QgsPointXY(prev_v),
                                                 QgsPointXY(pt)))
                            if next_v.sqrDist(pt) > eps2:
                                edges[k].append((QgsPointXY(pt),
                                                 QgsPointXY(next_v)))
        return edges

    # ------------------------------------------------------------------
    # Находка
    # ------------------------------------------------------------------

    def _finding(self, ctx, pos_keys, positions, pair_list, safe, reason,
                 edges=None):
        pts = [positions[k]["pt"] for k in pos_keys]
        cx = sum(p.x() for p in pts) / len(pts)
        cy = sum(p.y() for p in pts) / len(pts)
        fids = sorted(set().union(*[positions[k]["fids"]
                                    for k in pos_keys]))
        # максимальный сдвиг узла к центроиду и максимальная длина
        # присоединённого ребра: движок исправлений использует их для
        # ослабления порога «шва» (слив в центроид неизбежно оставляет
        # клин площадью <= сдвиг × длина ребра — см. layer_audit.py)
        max_shift = max((math.hypot(p.x() - cx, p.y() - cy)
                         for p in pts), default=0.0)
        max_edge = 0.0
        for _k, lst in (edges or {}).items():
            for u, v in lst:
                try:
                    max_edge = max(max_edge, math.sqrt(u.sqrDist(v)))
                except Exception:
                    continue
        payload = {
            "positions": [(p.x(), p.y()) for p in pts],
            "centroid": (cx, cy),
            "fids": fids,
            "safe": bool(safe),
            "max_shift": float(max_shift),
            "max_edge_len": float(max_edge),
        }
        p0 = pts[0]
        msg = u"узлы разных полигонов в пределах допуска ({} шт., " \
              u"разброс <= допуска); фичи: {}".format(
                  len(pos_keys),
                  u", ".join(u"#{}".format(f) for f in fids[:6]))
        if not safe:
            msg += u"; слияние небезопасно: " + reason
        return Finding(
            CODE_NEAR_NODE, fid=fids[0] if fids else None,
            point=QgsPointXY(p0), area=None, message=msg,
            fixable=bool(safe), payload=payload,
            layer_id=ctx.get("layer_id", u""),
            layer_name=ctx.get("layer_name", u""))


def _edges_aligned(u1, v1, u2, v2, tol):
    """Рёбра (u1,v1) и (u2,v2) коллинеарны в смысле правила ТЗ:
    направления параллельны (|sin| <= порога) и прямые отстоят не
    дальше допуска."""
    d1x, d1y = v1.x() - u1.x(), v1.y() - u1.y()
    d2x, d2y = v2.x() - u2.x(), v2.y() - u2.y()
    l1 = math.hypot(d1x, d1y)
    l2 = math.hypot(d2x, d2y)
    if l1 <= 0.0 or l2 <= 0.0:
        return False
    sin = abs((d1x * d2y - d1y * d2x) / (l1 * l2))
    if sin > _PARALLEL_SIN:
        return False
    # отступ прямой (u2,v2) от прямой (u1,v1): расстояние от u2 до
    # линии (u1,v1) — при параллельности это и есть отступ прямых
    cross = abs(d1x * (u2.y() - u1.y()) - d1y * (u2.x() - u1.x())) / l1
    return cross <= tol


def _open_ring(ring, eps2):
    """Открытое кольцо: замыкающая копия первой вершины удаляется."""
    pts = list(ring)
    if len(pts) > 1 and pts[0].sqrDist(pts[-1]) <= eps2:
        pts = pts[:-1]
    return pts
