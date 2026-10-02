import unittest

try:  # внутри QGIS папка плагина может называться topo_editor
    from topo_editor.core.graph import build_graph
    from topo_editor.core.ops import (
        CROSSES_OTHER_FACE,
        SELF_INTERSECTION,
        nearest_vertex,
        plan_vertex_move,
    )
    from topo_editor.tests.test_graph import grid2x2, square
except ImportError:
    from topo_editor.core.graph import build_graph
    from topo_editor.core.ops import (
        CROSSES_OTHER_FACE,
        SELF_INTERSECTION,
        nearest_vertex,
        plan_vertex_move,
    )
    from topo_editor.tests.test_graph import grid2x2, square

TOL = 0.001


def vertex_at(graph, xy):
    vid = nearest_vertex(graph, xy, 0.01)
    assert vid is not None, xy
    return vid


class MovePlanTests(unittest.TestCase):
    def setUp(self):
        self.faces = grid2x2()
        self.g = build_graph(self.faces, TOL)

    def test_valid_move_of_shared_center(self):
        plan = plan_vertex_move(self.g, vertex_at(self.g, (1, 1)), (1.2, 0.9), TOL)
        self.assertTrue(plan.ok, plan.violations)
        self.assertEqual({f for f, _, _ in plan.point_moves}, {"A", "B", "C", "D"})
        # при переносе общей вершины суммарная площадь мозаики не меняется
        delta = sum(new - old for old, new in plan.areas.values())
        self.assertAlmostEqual(delta, 0.0, places=9)
        # хотя бы у одного участка площадь действительно изменилась
        self.assertTrue(any(abs(new - old) > 1e-6 for old, new in plan.areas.values()))

    def test_move_t_junction_is_valid(self):
        plan = plan_vertex_move(self.g, vertex_at(self.g, (1, 0)), (1.1, 0.0), TOL)
        self.assertTrue(plan.ok, plan.violations)
        self.assertEqual({f for f, _, _ in plan.point_moves}, {"A", "B"})

    def test_move_unshared_corner(self):
        plan = plan_vertex_move(self.g, vertex_at(self.g, (0, 0)), (-0.2, -0.2), TOL)
        self.assertTrue(plan.ok)
        self.assertEqual({f for f, _, _ in plan.point_moves}, {"A"})
        old, new = plan.areas["A"]
        self.assertGreater(new, old)

    def test_bowtie_is_blocked_and_located(self):
        plan = plan_vertex_move(self.g, vertex_at(self.g, (1, 1)), (0.5, -1.0), TOL)
        self.assertFalse(plan.ok)
        selfs = [v for v in plan.violations if v.kind == SELF_INTERSECTION]
        self.assertTrue(selfs)
        # отрезок (0.5,-1)-(0,1) пересекает (0,0)-(1,0) в точке (0.25, 0)
        self.assertTrue(
            any(abs(v.point[0] - 0.25) < 1e-6 and abs(v.point[1]) < 1e-6 for v in selfs),
            [v.point for v in selfs],
        )

    def test_crossing_other_face_in_graph(self):
        faces = {"A": [square(0, 0, 1, 1)], "B": [square(1.5, 0, 2.5, 1)]}
        g = build_graph(faces, TOL)
        plan = plan_vertex_move(g, vertex_at(g, (1, 1)), (2.0, 0.5), TOL)
        self.assertFalse(plan.ok)
        self.assertIn(CROSSES_OTHER_FACE, {v.kind for v in plan.violations})

    def test_crossing_other_face_via_extra_provider(self):
        g = build_graph({"A": [square(0, 0, 1, 1)]}, TOL)
        calls = []

        def provider(bbox):
            calls.append(bbox)
            return {"B": [square(1.5, 0, 2.5, 1)]}

        plan = plan_vertex_move(g, vertex_at(g, (1, 1)), (2.0, 0.5), TOL, provider)
        self.assertEqual(len(calls), 1)
        self.assertFalse(plan.ok)
        self.assertIn(CROSSES_OTHER_FACE, {v.kind for v in plan.violations})

    def test_touching_other_face_is_blocked(self):
        faces = {"A": [square(0, 0, 1, 1)], "B": [square(1.5, 0, 2.5, 1)]}
        g = build_graph(faces, TOL)
        plan = plan_vertex_move(g, vertex_at(g, (1, 1)), (1.5, 0.5), TOL)
        self.assertFalse(plan.ok)
        self.assertTrue(any(v.detail == "touch" for v in plan.violations))

    def test_no_violations_when_far_from_others(self):
        faces = {"A": [square(0, 0, 1, 1)], "B": [square(1.5, 0, 2.5, 1)]}
        g = build_graph(faces, TOL)
        plan = plan_vertex_move(g, vertex_at(g, (1, 1)), (1.2, 1.3), TOL)
        self.assertTrue(plan.ok, plan.violations)

    def test_fold_back_over_neighbour_segment_is_blocked(self):
        # вершина (1,1) переносится на продолжение сегмента (1,0)-(0,0): складка
        g = build_graph({"A": [square(0, 0, 1, 1)]}, TOL)
        plan = plan_vertex_move(g, vertex_at(g, (1, 1)), (0.5, 0.0), TOL)
        self.assertFalse(plan.ok)

    def test_hole_crossing_outer_ring_is_blocked(self):
        outer = square(0, 0, 10, 10)
        hole = square(4, 4, 6, 6)[::-1]
        g = build_graph({"A": [outer, hole]}, TOL)
        plan = plan_vertex_move(g, vertex_at(g, (6, 6)), (12, 6), TOL)
        self.assertFalse(plan.ok)
        self.assertIn(SELF_INTERSECTION, {v.kind for v in plan.violations})

    def test_unknown_vertex_gives_empty_plan(self):
        plan = plan_vertex_move(self.g, 10_000, (0, 0), TOL)
        self.assertFalse(plan.ok)
        self.assertEqual(plan.point_moves, [])

    def test_graph_is_not_mutated(self):
        before = [list(r) for rings in self.faces.values() for r in rings]
        plan_vertex_move(self.g, vertex_at(self.g, (1, 1)), (5, 5), TOL)
        after = [list(r) for rings in self.g.faces.values() for r in rings]
        self.assertEqual(before, after)


class InsertVertexTests(unittest.TestCase):
    """Режим insert=True: добавление новой вершины на ребро без переноса."""

    def setUp(self):
        self.faces = grid2x2()
        self.g = build_graph(self.faces, TOL)

    def test_insert_on_shared_edge_updates_both_faces(self):
        # вершина (1, 0) - общий конец рёбер A|B и примыкания C|D;
        # сегмент (1,0)-(1,1) принадлежит сразу двум участкам (A и B)
        vid = vertex_at(self.g, (1, 0))
        plan = plan_vertex_move(self.g, vid, (1, 0.4), TOL, insert=True)
        self.assertTrue(plan.ok, plan.violations)
        self.assertEqual({f for f, _, _ in plan.point_inserts}, {"A", "B"})
        self.assertEqual(plan.point_moves, [])
        ra = plan.new_rings[("A", 0)]
        rb = plan.new_rings[("B", 0)]
        self.assertIn((1, 0.4), ra)
        self.assertIn((1, 0.4), rb)
        # в кольце A новая точка лежит между узлами общего ребра (1,0)-(1,1);
        # в кольце B обход идёт в обратную сторону: соседи (1,0) и (2,0)
        ia, ib = ra.index((1, 0.4)), rb.index((1, 0.4))
        self.assertEqual({ra[ia - 1], ra[ia + 1]}, {(1, 0), (1, 1)})
        self.assertEqual({rb[ib - 1], rb[ib + 1]}, {(1, 0), (2, 0)})
        # щель не появляется: соседние участки остаются общими с A и B.
        # точка (1, 0.4) НЕ лежит на исходном ребре A|B (оно идёт через (2, 0)),
        # поэтому площадь B уменьшается ровно на "вырезанный" треугольник
        # (1,0)-(1,0.4)-(1,1): 1.0 - 0.2 = 0.8; у A кольцо обходит новую точку
        # с другой стороны, его площадь остаётся 1.0.
        self.assertAlmostEqual(plan.areas["A"][1], 1.0, places=9)
        self.assertAlmostEqual(plan.areas["B"][1], 0.8, places=9)

    def test_insert_neighbour_vertex_remains_untouched(self):
        vid = vertex_at(self.g, (1, 0))
        old_xy = self.g.coords[vid]
        self.assertEqual(old_xy, (1, 0))
        plan = plan_vertex_move(self.g, vid, (1, 0.4), TOL, insert=True)
        self.assertTrue(plan.ok, plan.violations)
        for fid in ("A", "B"):
            ring = plan.new_rings[(fid, 0)]
            self.assertIn(old_xy, ring)  # старая вершина осталась на месте
        # вставлена ровно одна новая точка в каждое кольцо
        self.assertEqual(len(plan.point_inserts), 2)

    def test_insert_into_ring_start_with_closed_key(self):
        faces = {"A": [[(0, 0), (1, 0), (1, 1), (0, 1), (0, 0)],
                       [(3, 3), (4, 3), (4, 4), (3, 4), (3, 3)]]}
        g = build_graph(faces, TOL)
        vid = vertex_at(g, (0, 0))
        plan = plan_vertex_move(g, vid, (0.5, 0.0), TOL, insert=True)
        self.assertTrue(plan.ok, plan.violations)
        outer = plan.new_rings[("A", 0)]
        self.assertIn((0.5, 0.0), outer)
        self.assertNotIn(("A", 1), plan.new_rings)  # дыра не затронута планом
        self.assertEqual(faces["A"][1], g.faces["A"][1])  # исходная дыра цела
        self.assertEqual(outer[0], outer[-1])  # кольцо осталось замкнутым
        self.assertAlmostEqual(plan.areas["A"][0], plan.areas["A"][1], places=9)

    def test_insert_far_from_edge_is_blocked(self):
        # (1, 2.0) далеко за пределами сегмента (1,0)-(1,1): правка блокируется
        vid = vertex_at(self.g, (1, 0))
        plan = plan_vertex_move(self.g, vid, (1, 2.0), TOL, insert=True)
        self.assertFalse(plan.ok)
        self.assertTrue(plan.violations)

    def test_insert_crossing_other_face_is_blocked(self):
        faces = {"A": [square(0, 0, 1, 1)], "B": [square(1.5, 0, 2.5, 1)]}
        g = build_graph(faces, TOL)
        vid = vertex_at(g, (1, 0))
        plan = plan_vertex_move(g, vid, (2.0, 0.5), TOL, insert=True)
        self.assertFalse(plan.ok)
        self.assertIn(CROSSES_OTHER_FACE, {v.kind for v in plan.violations})

    def test_insert_ignores_merge_into(self):
        vid = vertex_at(self.g, (1, 0))
        other = vertex_at(self.g, (0, 1))
        plan = plan_vertex_move(self.g, vid, (1, 0.4), TOL,
                                merge_into=other, insert=True)
        self.assertIsNone(plan.merge_into)
        self.assertTrue(plan.ok, plan.violations)

    def test_plan_ok_requires_inserts(self):
        plan = plan_vertex_move(self.g, vertex_at(self.g, (1, 0)), (1, 0.4), TOL)
        self.assertTrue(plan.ok)  # обычный перенос тоже работает (point_moves)


if __name__ == "__main__":
    unittest.main()
