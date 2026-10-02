import unittest

from topo_graph_editor.core.graph import build_graph
from topo_graph_editor.core.ops import (
    CROSSES_OTHER_FACE,
    MERGE_SAME_FACE,
    find_snap_target,
    nearest_vertex,
    plan_vertex_move,
)
from topo_graph_editor.tests.test_graph import square

TOL = 0.001


def gap_faces(gap=0.05):
    """A и C разделены щелью шириной gap."""
    return {"A": [square(0, 0, 1, 1)], "C": [square(1 + gap, 0, 2 + gap, 1)]}


def vertex_at(graph, xy):
    vid = nearest_vertex(graph, xy, 0.01)
    assert vid is not None, xy
    return vid


class MergeTests(unittest.TestCase):
    def test_gap_is_not_shared_before_merge(self):
        g = build_graph(gap_faces(), TOL)
        self.assertEqual(len(g.shared_edges()), 0)

    def test_merge_closes_gap_in_two_steps(self):
        faces = gap_faces()
        g = build_graph(faces, TOL)

        # шаг 1: верхняя вершина A -> верхняя вершина C
        p1 = plan_vertex_move(
            g, vertex_at(g, (1, 1)), (0, 0), TOL, merge_into=vertex_at(g, (1.05, 1))
        )
        self.assertTrue(p1.ok, p1.violations)
        self.assertEqual(p1.new_xy, (1.05, 1))
        self.assertEqual({f for f, _, _ in p1.point_moves}, {"A"})
        old, new = p1.areas["A"]
        self.assertAlmostEqual(new - old, 0.025, places=9)

        for (face, ri), ring in p1.new_rings.items():
            faces[face][ri] = ring
        g = build_graph(faces, TOL)

        # шаг 2: нижняя вершина A -> нижняя вершина C
        p2 = plan_vertex_move(
            g, vertex_at(g, (1, 0)), (0, 0), TOL, merge_into=vertex_at(g, (1.05, 0))
        )
        self.assertTrue(p2.ok, p2.violations)
        for (face, ri), ring in p2.new_rings.items():
            faces[face][ri] = ring

        # после двух слияний у A и C появилась общая граница
        g = build_graph(faces, TOL)
        shared = g.shared_edges()
        self.assertEqual(len(shared), 1)
        self.assertEqual(shared[0].faces, frozenset({"A", "C"}))

    def test_free_move_onto_neighbour_vertex_is_blocked(self):
        # без слияния почти совпавшие вершины считаются касанием
        g = build_graph(gap_faces(), TOL)
        plan = plan_vertex_move(g, vertex_at(g, (1, 1)), (1.0495, 1.0), TOL)
        self.assertFalse(plan.ok)
        self.assertIn(CROSSES_OTHER_FACE, {v.kind for v in plan.violations})

    def test_merge_vertices_of_same_face_is_blocked(self):
        g = build_graph({"A": [square(0, 0, 1, 1)]}, TOL)
        plan = plan_vertex_move(
            g, vertex_at(g, (1, 1)), (0, 0), TOL, merge_into=vertex_at(g, (1, 0))
        )
        self.assertFalse(plan.ok)
        self.assertEqual({v.kind for v in plan.violations}, {MERGE_SAME_FACE})

    def test_merge_into_self_or_unknown_gives_empty_plan(self):
        g = build_graph(gap_faces(), TOL)
        v = vertex_at(g, (1, 1))
        self.assertFalse(plan_vertex_move(g, v, (0, 0), TOL, merge_into=v).ok)
        self.assertFalse(plan_vertex_move(g, v, (0, 0), TOL, merge_into=99_999).ok)

    def test_merge_that_crosses_third_face_is_blocked(self):
        # между A и C стоит узкий участок B, перепрыгнуть через него нельзя
        faces = gap_faces(gap=1.0)
        faces["B"] = [square(1.4, -1, 1.6, 2)]
        g = build_graph(faces, TOL)
        plan = plan_vertex_move(
            g, vertex_at(g, (1, 1)), (0, 0), TOL, merge_into=vertex_at(g, (2, 1))
        )
        self.assertFalse(plan.ok)
        self.assertIn(CROSSES_OTHER_FACE, {v.kind for v in plan.violations})


class SnapTargetTests(unittest.TestCase):
    def setUp(self):
        self.g = build_graph(gap_faces(), TOL)
        self.v = vertex_at(self.g, (1, 1))

    def test_finds_neighbour_vertex_in_radius(self):
        w = find_snap_target(self.g, self.v, (1.049, 1.002), 0.01)
        self.assertEqual(self.g.coords[w], (1.05, 1))

    def test_ignores_own_face_vertices(self):
        # рядом с курсором только вершина (1, 0) того же участка A
        self.assertIsNone(find_snap_target(self.g, self.v, (1.0, 0.002), 0.01))

    def test_nothing_in_radius(self):
        self.assertIsNone(find_snap_target(self.g, self.v, (5, 5), 0.01))

    def test_picks_nearest(self):
        faces = gap_faces()
        faces["D"] = [square(1.052, 1.05, 2, 2)]
        g = build_graph(faces, TOL)
        v = vertex_at(g, (1, 1))
        w = find_snap_target(g, v, (1.0505, 1.0), 0.1)
        self.assertEqual(g.coords[w], (1.05, 1))


if __name__ == "__main__":
    unittest.main()
