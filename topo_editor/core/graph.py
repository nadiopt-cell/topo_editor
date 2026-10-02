"""Планарный граф рёбер для границ полигонов.

Модуль не импортирует QGIS, поэтому его можно тестировать отдельно.

Терминология:
    вершина (vertex) - кластер точек в пределах допуска; хранится как id.
    узел (node)      - вершина, где меняется набор смежных граней
                       (T-образное примыкание, угол нескольких участков и т.п.).
    ребро (edge)     - цепочка вершин между двумя узлами; все сегменты ребра
                       принадлежат одному и тому же набору граней.
    грань (face)     - любой хешируемый идентификатор (например (fid, номер части)).

Вход: dict face_id -> список колец, кольцо = список (x, y).
"""
import math
from collections import defaultdict
from dataclasses import dataclass, field


class VertexIndex:
    """Сопоставляет точки вершинам с учётом допуска (сетка 3x3 ячейки).

    Известное ограничение: кластеризация не транзитивна (A~B, B~C, но A!~C).
    Для кадастровых данных с допуском в доли сантиметра это не критично.
    """

    def __init__(self, tolerance):
        self.tol = max(float(tolerance), 1e-12)
        self.coords = []
        self._cells = defaultdict(list)

    def _cell(self, x, y):
        return (math.floor(x / self.tol), math.floor(y / self.tol))

    def get_or_add(self, x, y):
        cx, cy = self._cell(x, y)
        tol2 = self.tol * self.tol
        best, best_d = None, None
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for vid in self._cells.get((cx + dx, cy + dy), ()):
                    vx, vy = self.coords[vid]
                    d = (vx - x) ** 2 + (vy - y) ** 2
                    if d <= tol2 and (best_d is None or d < best_d):
                        best, best_d = vid, d
        if best is not None:
            return best
        vid = len(self.coords)
        self.coords.append((x, y))
        self._cells[(cx, cy)].append(vid)
        return vid


def _seg(a, b):
    return (a, b) if a < b else (b, a)


@dataclass
class Edge:
    id: int
    vertices: list      # id вершин; первая и последняя - узлы
    faces: frozenset    # грани, для которых это ребро является границей

    @property
    def start(self):
        return self.vertices[0]

    @property
    def end(self):
        return self.vertices[-1]

    @property
    def is_loop(self):
        return self.vertices[0] == self.vertices[-1]

    @property
    def is_shared(self):
        return len(self.faces) >= 2


@dataclass
class Graph:
    coords: list                                  # vid -> (x, y)
    nodes: set = field(default_factory=set)
    edges: dict = field(default_factory=dict)     # edge id -> Edge
    node_edges: dict = field(default_factory=dict)  # vid узла -> [edge id, ...]
    # (face_id, номер кольца) -> [(edge id, forward), ...] в порядке обхода
    ring_edges: dict = field(default_factory=dict)
    faces: dict = field(default_factory=dict)         # исходные кольца граней
    # (face_id, номер кольца) -> [vid на каждую исходную точку кольца]
    point_vids: dict = field(default_factory=dict)
    # vid -> [((face_id, номер кольца), индекс точки), ...]
    vertex_refs: dict = field(default_factory=dict)

    def edge_coords(self, edge_id):
        return [self.coords[v] for v in self.edges[edge_id].vertices]

    def shared_edges(self):
        return [e for e in self.edges.values() if e.is_shared]

    def edges_of_face(self, face_id):
        return [e for e in self.edges.values() if face_id in e.faces]

    def ring_coords(self, face_id, ring_index):
        """Собирает координаты кольца из рёбер (замкнутое кольцо)."""
        pts = []
        for eid, forward in self.ring_edges[(face_id, ring_index)]:
            vs = self.edges[eid].vertices
            vs = vs if forward else vs[::-1]
            pts.extend(vs if not pts else vs[1:])
        return [self.coords[v] for v in pts]


def build_graph(faces, tolerance):
    index = VertexIndex(tolerance)

    # 1. Кольца -> последовательности вершин, сегменты -> наборы граней
    rings = {}
    point_vids = {}
    seg_faces = defaultdict(set)
    for fid, face_rings in faces.items():
        for ri, ring in enumerate(face_rings):
            pv = [index.get_or_add(x, y) for x, y in ring]
            vids = []
            for v in pv:
                if not vids or vids[-1] != v:
                    vids.append(v)
            while len(vids) > 1 and vids[0] == vids[-1]:
                vids.pop()
            if len(vids) < 3:
                continue  # вырожденное кольцо
            rings[(fid, ri)] = vids
            point_vids[(fid, ri)] = pv
            n = len(vids)
            for i in range(n):
                seg_faces[_seg(vids[i], vids[(i + 1) % n])].add(fid)

    vertex_segs = defaultdict(list)
    for s in seg_faces:
        vertex_segs[s[0]].append(s)
        vertex_segs[s[1]].append(s)

    # 2. Узлы: степень != 2 или смена набора граней вдоль вершины
    nodes = set()
    for v, segs in vertex_segs.items():
        if len(segs) != 2 or seg_faces[segs[0]] != seg_faces[segs[1]]:
            nodes.add(v)

    # кольца без узлов (изолированный участок, остров, дыра): ставим искусственный
    for key, vids in rings.items():
        if not any(v in nodes for v in vids):
            nodes.add(vids[0])

    # 3. Трассировка рёбер от узлов
    used = set()
    edges = {}
    seg_edge = {}
    node_edges = defaultdict(list)
    for n in sorted(nodes):
        for seg in vertex_segs[n]:
            if seg in used:
                continue
            used.add(seg)
            seg_list = [seg]
            path = [n]
            cur = seg[1] if seg[0] == n else seg[0]
            last = seg
            while True:
                path.append(cur)
                if cur in nodes:
                    break
                s1, s2 = vertex_segs[cur]
                nxt = s2 if s1 == last else s1
                used.add(nxt)
                seg_list.append(nxt)
                cur = nxt[1] if nxt[0] == cur else nxt[0]
                last = nxt
            eid = len(edges)
            edges[eid] = Edge(eid, path, frozenset(seg_faces[seg]))
            for s in seg_list:
                seg_edge[s] = eid
            node_edges[path[0]].append(eid)
            node_edges[path[-1]].append(eid)

    # 4. Порядок рёбер в каждом кольце (нужно для последующей пересборки геометрий)
    ring_edges = {}
    for key, vids in rings.items():
        start = next(i for i, v in enumerate(vids) if v in nodes)
        rot = vids[start:] + vids[:start]
        rot.append(rot[0])
        chunks, cur = [], [rot[0]]
        for v in rot[1:]:
            cur.append(v)
            if v in nodes:
                chunks.append(cur)
                cur = [v]
        seq = []
        for ch in chunks:
            eid = seg_edge[_seg(ch[0], ch[1])]
            ev = edges[eid].vertices
            seq.append((eid, ch[0] == ev[0] and ch[1] == ev[1]))
        ring_edges[key] = seq

    vertex_refs = defaultdict(list)
    for key, pv in point_vids.items():
        for idx, v in enumerate(pv):
            vertex_refs[v].append((key, idx))

    return Graph(
        index.coords, nodes, edges, dict(node_edges), ring_edges,
        faces=faces, point_vids=point_vids, vertex_refs=dict(vertex_refs),
    )
