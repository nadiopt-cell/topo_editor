# -*- coding: utf-8 -*-
"""Тесты трёх текущих инструментов на настоящих слоях (этап 0 ТЗ).

Инструменты проверяются через их ядро — публичный API TopoEditor
(move_vertex / move_edge / add_vertex_on_edge), как это делают
инструменты карты после обработки ввода. Плюс smoke-тест создания
инструментов на настоящем холсте.

Приёмка этапа 0: топология целостна (assert_topology_ok), одна операция
= один шаг отмены, откат возвращает слой битово.
"""
import pytest
from qgis.PyQt.QtWidgets import QAction

from fixtures_data import GRID_CELL, make_tjunction_layer
from topology_asserts import (
    assert_topology_ok,
    layer_snapshot,
    shared_pairs_multiset,
)

from topopolyedit import settings as tp_settings
import topopolyedit.topo_editor as te_module


@pytest.fixture
def editor_keep(monkeypatch, stub_iface):
    """TopoEditor с режимом перехлёстов «оставить» (без диалогов)."""
    monkeypatch.setattr(
        te_module.settings, "overlap_mode",
        lambda: tp_settings.OVERLAP_KEEP)

    def _make(layer):
        return te_module.TopoEditor(stub_iface, layer)

    return _make


def test_move_shared_node_four_polygons(editor_keep, grid_layer):
    """Узел (10,10) — угол всех четырёх участков 2x2: двигаются все."""
    layer, _tags = grid_layer
    ed = editor_keep(layer)
    before = layer_snapshot(layer)
    pairs_before = shared_pairs_multiset(layer)

    rep = ed.move_vertex(
        _pt(GRID_CELL, GRID_CELL), _pt(GRID_CELL + 2.0, GRID_CELL + 1.0))

    assert not rep.get("error"), rep
    assert rep["moved"] == 4, rep
    assert layer_snapshot(layer) != before
    # площадь при переносе узла НЕ сохраняется — проверяем только топологию
    assert_topology_ok(layer, holes_before=0, msg="move_node 2x2")
    # И3: общий узел сохранился у той же четвёрки фич
    assert shared_pairs_multiset(layer) == pairs_before

    # одна операция = один шаг отмены
    layer.undoStack().undo()
    assert layer_snapshot(layer) == before


def test_move_edge_two_anchors(editor_keep, grid_layer):
    """Общее ребро x=10 (y 0..10): полигоны слева и справа двигаются."""
    layer, _tags = grid_layer
    ed = editor_keep(layer)
    before = layer_snapshot(layer)

    anchors = [_pt(GRID_CELL, 0.0), _pt(GRID_CELL, GRID_CELL)]
    rep = ed.move_edge(anchors, (2.0, 0.0))

    assert not rep.get("error"), rep
    assert rep["moved"] == 4, rep  # (0,0),(1,0) целиком + (0,1),(1,1) якорем
    assert_topology_ok(layer, holes_before=0, msg="move_edge 2x2")
    layer.undoStack().undo()
    assert layer_snapshot(layer) == before


def test_add_vertex_on_shared_edge(editor_keep, grid_layer):
    """Вставка узла на общее ребро: появляется в обоих полигонах."""
    layer, _tags = grid_layer
    ed = editor_keep(layer)
    before = layer_snapshot(layer)
    mid = _pt(GRID_CELL, 4.0)

    rep = ed.add_vertex_on_edge(
        _pt(GRID_CELL, 0.0), _pt(GRID_CELL, GRID_CELL), mid)

    assert not rep.get("error"), rep
    assert rep["features"] == 2, rep  # оба смежных полигона
    assert _has_vertex(layer, mid), "узел не появился в геометриях"
    assert_topology_ok(layer, holes_before=0, msg="add_vertex")
    layer.undoStack().undo()
    assert layer_snapshot(layer) == before


def test_move_node_on_cadastral(editor_keep, cadastral_layer):
    """«Кадастр»: непрямые границы, дыра, мультиполигон — общий узел
    (19.7, 10.4) двигают; дыра и отдельная часть мультиполигона
    неподвижны, топология целостна, щелей нет."""
    layer, info = cadastral_layer
    tags = info["tags"]
    ed = editor_keep(layer)

    far_part_before = _far_part_bbox(layer, tags[(2, 1)])
    hole_area_before = _hole_area(layer, tags[(2, 0)])

    node = _pt(19.7, 10.4)
    rep = ed.move_vertex(node, _pt(20.1, 10.9))
    assert not rep.get("error"), rep
    assert rep["moved"] >= 3, rep  # участки (1,0), (2,0), (1,1), (2,1)

    assert_topology_ok(layer, holes_before=1, msg="cadastr")
    assert _hole_area(layer, tags[(2, 0)]) == hole_area_before
    assert _far_part_bbox(layer, tags[(2, 1)]) == far_part_before


def test_tjunction_current_behavior_documented(editor_keep, stub_iface):
    """Документация ТЕКУЩЕГО (до этапа 2) поведения на T-примыкании.

    Вершина B (5,10) лежит на ребре A без узла: перемещение узла B НЕ
    вставляет вершину в A — известная дыра И4 (этап 2 закроет её
    автовставкой; этот тест тогда ПЕРЕПИСЫВАЕТСЯ на новую семантику)."""
    layer, tags = make_tjunction_layer()
    layer.startEditing()
    try:
        ed = editor_keep(layer)
        node_b = _pt(25.0, 10.0)  # правый верхний угол B, вне ребра A
        rep = ed.move_vertex(node_b, _pt(27.0, 12.0))
        assert not rep.get("error"), rep
        # A не должен измениться вовсе (его вершины не двигались)
        geom_a = _geom_by_tag(layer, "A")
        assert _vertex_count(geom_a) == 4, (
            "A изменился без причины")
        # и в A нет вершины в (5,10) — дыра И4, документируем
        assert not _has_point_as_vertex(geom_a, _pt(5.0, 10.0))
    finally:
        layer.rollBack()


def test_tools_attach_and_detach(iface_canvas, grid_layer):
    """Smoke: все три инструмента создаются, ставятся и снимаются
    с настоящего холста без исключений."""
    from topopolyedit.topo_add_vertex_tool import TopoAddVertexTool
    from topopolyedit.topo_edge_move_tool import TopoEdgeMoveTool
    from topopolyedit.topo_move_tool import TopoMoveTool

    layer, _tags = grid_layer
    canvas = iface_canvas.mapCanvas()
    for cls in (TopoMoveTool, TopoEdgeMoveTool, TopoAddVertexTool):
        action = QAction("t", canvas)
        tool = cls(iface_canvas, action)
        canvas.setMapTool(tool)
        assert canvas.mapTool() is tool
        canvas.unsetMapTool(tool)


# ---------------------------------------------------------------- helpers

def _pt(x, y):
    from qgis.core import QgsPointXY
    return QgsPointXY(x, y)


def _geom_by_tag(layer, tag):
    for f in layer.getFeatures():
        if f["id"] == tag:
            return f.geometry()
    raise AssertionError("фича {} не найдена".format(tag))


def _vertex_count(geom):
    parts = geom.asMultiPolygon() if geom.isMultipart() else [geom.asPolygon()]
    return sum(len(ring) for poly in parts for ring in poly)


def _has_vertex(layer, pt):
    for _fid, g in [(f.id(), f.geometry()) for f in layer.getFeatures()]:
        if _has_point_as_vertex(g, pt):
            return True
    return False


def _has_point_as_vertex(geom, pt, eps=1e-6):
    parts = geom.asMultiPolygon() if geom.isMultipart() else [geom.asPolygon()]
    for poly in parts:
        for ring in poly:
            for p in ring:
                if abs(p.x() - pt.x()) <= eps and abs(p.y() - pt.y()) <= eps:
                    return True
    return False


def _far_part_bbox(layer, fid):
    """Габарит отдельно стоящей части мультиполигона (x > 45)."""
    g = None
    for f in layer.getFeatures():
        if f.id() == fid:
            g = f.geometry()
            break
    assert g is not None and g.isMultipart()
    best = None
    for part in g.asMultiPolygon():
        xs = [p.x() for ring in part for p in ring]
        if max(xs) < 45.0:
            continue
        box = (min(xs), max(xs))
        if best is None:
            best = box
    assert best is not None, "отдельная часть не найдена"
    return best


def _hole_area(layer, fid):
    """Площадь дыры полигона (внутреннее кольцо)."""
    for f in layer.getFeatures():
        if f.id() == fid:
            parts = (f.geometry().asMultiPolygon()
                     if f.geometry().isMultipart()
                     else [f.geometry().asPolygon()])
            outer, hole = parts[0][0], parts[0][1]
            return (QgsGeometry_shoelace(outer) -
                    abs(QgsGeometry_shoelace(hole)))
    raise AssertionError("фича {} не найдена".format(fid))


def QgsGeometry_shoelace(ring):
    """Знаковая площадь замкнутого кольца (метод трапеций)."""
    s = 0.0
    n = len(ring)
    for i in range(n):
        p1 = ring[i]
        p2 = ring[(i + 1) % n]
        s += p1.x() * p2.y() - p2.x() * p1.y()
    return s / 2.0


if __name__ == "__main__":
    raise SystemExit("Запускайте: python3 -m pytest tests/qgis -v")
