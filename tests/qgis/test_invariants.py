# -*- coding: utf-8 -*-
"""Свойственные тесты (hypothesis): случайные перемещения узлов сетки.

Инвариант (ТЗ, этап 0): либо операция заблокирована (report["error"] /
cancelled — слой битово не изменён), либо assert_topology_ok проходит и
И3 соблюдён (структура «общий узел -> фичи» сохранена).

XFAIL (strict=False) — известные дыры до этапов 1–2:
- И1: невалидная геометрия молча чинится makeValid (topo_editor.py
  _apply_moves) — форма меняется без ведома пользователя;
- И4: T-примыкания без узла не латываются (сосед не следует).
После этапа 2 маркер снимается; XPASS до того — норма.
"""
import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

import topopolyedit.topo_editor as te_module
from fixtures_data import make_grid_layer
from topology_asserts import (
    assert_topology_ok,
    layer_snapshot,
    shared_pairs_multiset,
    total_area,
)

pytestmark = pytest.mark.xfail(
    strict=False,
    reason="Этапы 1–2 не выполнены: молчаливый makeValid (И1), "
           "T-примыкания (И4) — см. ТЗ этап 0, требование 5")

from qgis.core import QgsPointXY  # noqa: E402

CELL = 10.0


@settings(max_examples=200, deadline=None, database=None)
@given(
    node_i=st.integers(min_value=1, max_value=2),
    node_j=st.integers(min_value=1, max_value=2),
    dx=st.integers(min_value=-3, max_value=3),
    dy=st.integers(min_value=-3, max_value=3),
)
def test_random_node_moves_keep_topology(node_i, node_j, dx, dy):
    """Случайный внутренний узел сетки 3x3 + случайное смещение.

    Сетка 3x3: внутренние узлы (10|20, 10|20). Смещение больше допуска
    совпадения, иначе операция законна «не смещена»."""
    assume(abs(dx) + abs(dy) > 0)
    assume(abs(dx) > 1 or abs(dy) > 1)  # дальше TOPO_MATCH_EPS

    from topopolyedit import settings as tp_settings

    layer, _tags = make_grid_layer(3, 3)
    layer.startEditing()
    old_mode = te_module.settings.overlap_mode
    te_module.settings.overlap_mode = lambda: tp_settings.OVERLAP_KEEP
    try:
        ed = te_module.TopoEditor(None, layer)
        before = layer_snapshot(layer)
        pairs_before = shared_pairs_multiset(layer)

        old_pt = QgsPointXY(node_i * CELL, node_j * CELL)
        new_pt = QgsPointXY(node_i * CELL + dx, node_j * CELL + dy)
        rep = ed.move_vertex(old_pt, new_pt)

        if rep.get("error") or rep.get("cancelled"):
            # блокировка тоже допустима, но тогда слой не тронут
            assert layer_snapshot(layer) == before, (
                "ошибка/отмена reported, а слой изменился: {}".format(rep))
            assume(False)  # блокированный пример не считается попыткой
            return

        assert_topology_ok(layer, holes_before=0, msg="property")
        assert shared_pairs_multiset(layer) == pairs_before, (
            "И3: структура общих узлов изменилась")
        # площадь не проверяем: перенос узла законно меняет площади
    finally:
        te_module.settings.overlap_mode = old_mode
        layer.rollBack()


if __name__ == "__main__":
    raise SystemExit("Запускайте: python3 -m pytest tests/qgis -v")
