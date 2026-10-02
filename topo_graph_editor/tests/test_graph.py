import unittest

from topo_graph_editor.core.graph import build_graph


def square(x0, y0, x1, y1):
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]


def grid2x2(shift=0.0):
    """Четыре единичных квадрата; общая вершина (1,1) у D смещена на shift."""
    c = 1.0 + shift
    return {
        "A": [square(0, 0, 1, 1)],
        "B": [square(1, 0, 2, 1)],
        "C": [square(0, 1, 1, 2)],
        "D": [[(c, c), (2, 1), (2, 2), (1, 2), (c, c)]],
    }


class GraphTests(unittest.TestCase):
    def test_grid_topology(self):
        g = build_graph(grid2x2(), 0.001)
        # центр + 4 T-узла на внешней границе
        self.assertEqual(len(g.nodes), 5)
        # 4 общих "спицы" + 4 внешних дуги
        self.assertEqual(len(g.edges), 8)
        self.assertEqual(len(g.shared_edges()), 4)

    def test_single_polygon_gets_forced_node(self):
        g = build_graph({"A": [square(0, 0, 1, 1)]}, 0.001)
        self.assertEqual(len(g.nodes), 1)
        self.assertEqual(len(g.edges), 1)
        self.assertTrue(next(iter(g.edges.values())).is_loop)

    def test_polygon_with_hole(self):
        outer = square(0, 0, 10, 10)
        hole = square(4, 4, 6, 6)[::-1]
        g = build_graph({"A": [outer, hole]}, 0.001)
        self.assertEqual(len(g.edges), 2)

    def test_tolerance_merges_close_vertices(self):
        # вершина D смещена на 0.0005: при допуске 0.001 граф тот же
        g = build_graph(grid2x2(shift=0.0005), 0.001)
        self.assertEqual(len(g.nodes), 5)
        self.assertEqual(len(g.shared_edges()), 4)

    def test_small_tolerance_exposes_gap(self):
        # вершина D не склеилась с центром: у D нет общих рёбер с B и C
        g = build_graph(grid2x2(shift=0.0005), 0.0001)
        self.assertEqual(len(g.shared_edges()), 2)

    def test_ring_reconstruction(self):
        faces = grid2x2()
        g = build_graph(faces, 0.001)
        for fid, rings in faces.items():
            rebuilt = g.ring_coords(fid, 0)
            original = set(rings[0][:-1])
            self.assertEqual(set(rebuilt[:-1] if rebuilt[0] == rebuilt[-1] else rebuilt), original)

    def test_shared_edge_has_both_faces(self):
        g = build_graph(grid2x2(), 0.001)
        pairs = {e.faces for e in g.shared_edges()}
        self.assertEqual(
            pairs,
            {frozenset("AB"), frozenset("AC"), frozenset("BD"), frozenset("CD")},
        )


if __name__ == "__main__":
    unittest.main()
