# -*- coding: utf-8 -*-
"""Приёмка этапа 3 ТЗ: индекс топологии (TopologyIndex).

Требования ТЗ (этап 3, «Приёмка»):
  1. На слое из 10 000 полигонов первая операция в новой области
     <= 0,5 с, повторные — <= 50 мс (поиск узла и соседей)
     (test_first_operation_within_budget, test_repeated_operation_fast);
  2. после цепочки «правка -> отмена -> повтор» индекс равен полной
     пересборке (test_integrity_after_undo_redo_chain,
     test_integrity_after_rollback);
  3. удаление/добавление фич внешними инструментами QGIS отражается
     в индексе без перезапуска (test_external_add_delete_reflected).

Дополнительно: хирургическая инвалидация не ломает последующие
операции (test_index_survives_edit_then_move_again), T-примыкание
через индекс (test_tjunction_move_via_index), лимит соседей с явным
сообщением (test_neighbor_limit_message), работа при выключенном
индексе (test_index_disabled_falls_back), API-методы
(test_api_nodes_edges_neighbors),.prefetch синхронно
(test_prefetch_builds_tiles).

Запуск: QT_QPA_PLATFORM=offscreen python3 -m pytest tests/qgis -v
"""
import time

import pytest
from qgis.core import (
    QgsFeature, QgsGeometry, QgsPointXY, QgsRectangle,
)

from fixtures_data import (
    make_cadastral_layer,
    make_grid_layer,
    make_single_polygon_layer,
)
from topology_asserts import assert_topology_ok

from topopolyedit import settings as tp_settings
from topopolyedit import topology_index as tindex
from topopolyedit import utils as tp_utils
from topopolyedit.geo_utils import norm_polys, contains_vertex
from topopolyedit.topo_editor import TopoEditor


def _pt(x, y):
    return QgsPointXY(x, y)


@pytest.fixture(autouse=True)
def _clean_registry():
    """Реестр индексов и журнал операций чисты перед и после теста."""
    tindex.shutdown_all()
    tp_utils.reset_operation_journal()
    yield
    tindex.shutdown_all()
    tp_utils.reset_operation_journal()


@pytest.fixture
def editor_keep(monkeypatch, stub_iface):
    """TopoEditor с режимом перехлёстов «оставить» (без диалогов)."""
    monkeypatch.setattr(
        tp_settings, "overlap_mode", lambda: tp_settings.OVERLAP_KEEP)

    def _make(layer):
        return TopoEditor(stub_iface, layer)

    return _make


# ---------------------------------------------------------------------------
# Приёмка 1: перфоманс на 10 000 полигонов
# ---------------------------------------------------------------------------

def _big_grid():
    """Сетка 100x100 = 10 000 смежных участков (ячейка 10 м)."""
    layer, tags = make_grid_layer(100, 100)
    layer.startEditing()
    return layer, tags


def test_first_operation_within_budget():
    """Первая операция в новой области <= 0,5 с (включая сборку плитки).

    Бюджет сборки плитки 0,4 с: при превышении плитка честно не
    строится, операция идёт прямым запросом — суммарно всё равно
    в пределах приёмки."""
    layer, tags = _big_grid()
    try:
        ed = TopoEditor(None, layer)
        # узел (10, 10) — общий для 4 участков
        t0 = time.perf_counter()
        rep = ed.move_vertex(_pt(10, 10), _pt(10.001, 10.001))
        first = time.perf_counter() - t0
        assert rep.get("error") is None, rep
        assert rep["moved"] == 4
        print("\n[perf] первая операция (10k): {:.3f} с".format(first))
        assert first <= 0.5, \
            "первая операция {:.3f} с > 0,5 с".format(first)
    finally:
        layer.rollBack()


def test_repeated_operation_fast():
    """Повторная операция в той же области <= 50 мс."""
    layer, tags = _big_grid()
    try:
        ed = TopoEditor(None, layer)
        rep = ed.move_vertex(_pt(10, 10), _pt(10.001, 10.001))
        assert rep.get("error") is None, rep
        # вторая операция: плитки уже построены, инвалидация хирургическая
        t0 = time.perf_counter()
        rep2 = ed.move_vertex(_pt(20, 10), _pt(20.001, 10.001))
        repeated = time.perf_counter() - t0
        assert rep2.get("error") is None, rep2
        assert rep2["moved"] == 4
        print("\n[perf] повторная операция (10k): {:.4f} с".format(repeated))
        assert repeated <= 0.05, \
            "повторная операция {:.4f} с > 50 мс".format(repeated)

        # прямой поиск по API индекса тоже мгновенен
        idx = tindex.index_for_layer(layer)
        assert idx is not None
        t0 = time.perf_counter()
        refs = idx.nodes_at(_pt(30, 10))
        nb = idx.bbox_features(QgsRectangle(29, 9, 31, 11))
        lookup = time.perf_counter() - t0
        assert len(refs) == 4 and len(nb.get(idx.layer_id, ())) >= 1
        print("[perf] nodes_at+bbox (10k): {:.4f} с".format(lookup))
        assert lookup <= 0.05
    finally:
        layer.rollBack()


# ---------------------------------------------------------------------------
# Приёмка 2: целостность после цепочек «правка -> отмена -> повтор»
# ---------------------------------------------------------------------------

def test_integrity_after_undo_redo_chain(editor_keep):
    """правка -> undo (кнопка QGIS) -> redo -> undo: индекс == пересборка.

    QGIS издаёт geometryChanged при откате/повторе геометрических
    команд — индекс обновляется по тем же сигналам (ТЗ этап 3, п. 3)."""
    layer, tags = make_grid_layer(2, 2)
    layer.startEditing()
    try:
        ed = editor_keep(layer)
        assert ed.move_vertex(_pt(10, 10), _pt(10.5, 10.5))["moved"] == 4
        idx = tindex.index_for_layer(layer)
        assert idx is not None
        assert idx.check_integrity() == []

        stack = layer.undoStack()
        stack.undo()  # штатная отмена QGIS (Ctrl+Z)
        assert idx.check_integrity() == []
        refs = idx.nodes_at(_pt(10, 10))
        assert {r.fid for r in refs} == set(tags.values())

        stack.redo()  # повтор
        assert idx.check_integrity() == []
        assert len(idx.nodes_at(_pt(10.5, 10.5))) == 4

        stack.undo()  # и снова отмена
        assert idx.check_integrity() == []
        assert idx.nodes_at(_pt(10.5, 10.5)) == []
    finally:
        layer.rollBack()


def test_integrity_after_rollback(editor_keep):
    """rollback сеанса правки: плитки bbox последней операции
    пересобираются, индекс равен полной пересборке (ТЗ этап 3, п. 3)."""
    layer, tags = make_grid_layer(2, 2)
    layer.startEditing()
    try:
        ed = editor_keep(layer)
        assert ed.move_vertex(_pt(10, 10), _pt(10.5, 10.5))["moved"] == 4
        idx = tindex.index_for_layer(layer)
        assert idx is not None
        assert idx.check_integrity() == []
        # журнал операции записан редактором: bbox накрывает старые и
        # новые положения; откат БЕЗ сигналов по фичам
        layer.rollBack()
        assert idx.check_integrity() == []
        refs = idx.nodes_at(_pt(10, 10))
        assert {r.fid for r in refs} == set(tags.values())
        assert idx.nodes_at(_pt(10.5, 10.5)) == []
    finally:
        pass  # правки уже откачены


def test_index_survives_edit_then_move_again(editor_keep):
    """Хирургическая инвалидация: после правки следующая операция
    работает по свежим данным и остаётся быстрой."""
    layer, tags = make_grid_layer(3, 3)
    layer.startEditing()
    try:
        ed = editor_keep(layer)
        rep1 = ed.move_vertex(_pt(10, 10), _pt(10.4, 10.4))
        assert rep1.get("error") is None, rep1
        assert rep1["moved"] == 4
        idx = tindex.index_for_layer(layer)
        assert idx.nodes_at(_pt(10, 10)) == []
        rep2 = ed.move_vertex(_pt(20, 20), _pt(20.4, 20.4))
        assert rep2.get("error") is None, rep2
        assert rep2["moved"] == 4
        assert idx.check_integrity() == []
        assert len(idx.nodes_at(_pt(20.4, 20.4))) == 4
        assert_topology_ok(layer)
    finally:
        layer.rollBack()


# ---------------------------------------------------------------------------
# Приёмка 3: внешние правки отражаются в индексе без перезапуска
# ---------------------------------------------------------------------------

def test_external_add_delete_reflected(editor_keep):
    """addFeature/deleteFeature сторонними средствами QGIS (edit buffer)
    обновляют индекс: узлы новой фичи видны, удалённой — нет."""
    layer, tags = make_grid_layer(2, 2)
    layer.startEditing()
    try:
        ed = editor_keep(layer)
        assert ed.move_vertex(_pt(10, 10), _pt(10.3, 10.3))["moved"] == 4
        idx = tindex.index_for_layer(layer)
        assert idx is not None

        # ВНЕШНЕЕ добавление (как инструмент QGIS)
        f = QgsFeature(layer.fields())
        ring = [_pt(40, 40), _pt(50, 40), _pt(50, 50), _pt(40, 50),
                _pt(40, 40)]
        f.setGeometry(QgsGeometry.fromPolygonXY([ring]))
        assert layer.addFeature(f), "фича не добавлена"
        new_fid = f.id()
        assert len(idx.nodes_at(_pt(40, 40))) == 1
        assert idx.features_touching(_pt(45, 40)) != {}
        assert idx.check_integrity() == []

        # ВНЕШНЕЕ удаление
        assert layer.deleteFeature(new_fid)
        assert idx.nodes_at(_pt(40, 40)) == []
        assert idx.check_integrity() == []
    finally:
        layer.rollBack()


# ---------------------------------------------------------------------------
# Интеграция с операциями этапов 1–2 через индекс
# ---------------------------------------------------------------------------

def _poly_geom(pts):
    """[(x, y), ...] -> замкнутый полигон QgsGeometry."""
    ring = [QgsPointXY(x, y) for x, y in pts]
    ring.append(QgsPointXY(ring[0]))
    return QgsGeometry.fromPolygonXY([ring])


def test_tjunction_move_via_index(editor_keep):
    """T-примыкание обрабатывается индексным поиском: у A НЕТ вершины
    в точке узла B, A находится поиском ребра под точкой (index edges),
    залатывается и двигается вместе с B (moved == 2).

    Геометрия — «открытое T»: ребро B заканчивается ОБЩЕЙ вершиной A
    (20,10). Иначе (ребро B продолжается за конец ребра A) сдвиг T-узла
    рождает клин между прямым ребром B и согнутым ребром A — такие
    операции валидатор честно блокирует (этап 1; см. докстринг
    test_t_junction.py), и приёмка «оба полигона двигаются» на такой
    геометрии недостижима по определению."""
    layer = make_single_polygon_layer("t_index")
    for tag, geom in [
        ("A", _poly_geom([(0, 0), (20, 0), (20, 10), (0, 10)])),
        ("B", _poly_geom([(5, 10), (20, 10), (20, 20)])),
    ]:
        f = QgsFeature(layer.fields())
        f.setGeometry(geom)
        f.setAttribute("id", tag)
        assert layer.dataProvider().addFeatures([f])
    layer.startEditing()
    try:
        idx = tindex.index_for_layer(layer)
        assert idx is not None
        # индекс находит ОБА полигона: B по вершине, A по ребру под точкой
        near = idx.features_near_points([_pt(5, 10)], 1e-6)
        assert near is not None and len(near) == 2, near
        ed = editor_keep(layer)
        rep = ed.move_vertex(_pt(5, 10), _pt(8, 13))
        assert rep.get("error") is None, rep
        assert rep["moved"] == 2, rep  # A (T-залатывание) + B
        assert idx.check_integrity() == []
        geoms = {f2["id"]: f2.geometry() for f2 in layer.getFeatures()}
        assert set(geoms) == {"A", "B"}
        for tag, g in geoms.items():
            nr = norm_polys(g)
            assert contains_vertex(nr[0], _pt(8, 13), 1e-6), tag
        assert_topology_ok(layer, holes_before=0, msg="t-index")
    finally:
        layer.rollBack()


def test_neighbor_limit_message(monkeypatch, editor_keep):
    """Лимит соседей (этап 3, п. 7): явное сообщение при превышении."""
    monkeypatch.setattr(tp_settings, "max_overlap_neighbors",
                        lambda: 1)
    layer, tags = make_grid_layer(3, 3)
    layer.startEditing()
    try:
        ed = editor_keep(layer)
        rep = ed.move_vertex(_pt(10, 10), _pt(10.3, 10.3))
        assert rep.get("error") is not None
        assert "больше 1" in rep["error"]
        assert rep.get("moved", 0) == 0
        # слой битово цел
        geoms = {f.id(): f.geometry() for f in layer.getFeatures()}
        nr = norm_polys(geoms[tags[(0, 0)]])
        assert contains_vertex(nr[0], _pt(0, 0), 1e-6)
        assert contains_vertex(nr[0], _pt(10, 10), 1e-6)
    finally:
        layer.rollBack()


def test_index_disabled_falls_back(monkeypatch, editor_keep):
    """Настройка «индекс включён» = False: работа как до этапа 3."""
    monkeypatch.setattr(tp_settings, "index_enabled", lambda: False)
    layer, tags = make_grid_layer(2, 2)
    layer.startEditing()
    try:
        ed = editor_keep(layer)
        rep = ed.move_vertex(_pt(10, 10), _pt(10.3, 10.3))
        assert rep.get("error") is None, rep
        assert rep["moved"] == 4
        assert tindex.index_for_layer(layer) is None
        assert tindex.registry_size() == 0
        assert_topology_ok(layer)
    finally:
        layer.rollBack()


def test_api_nodes_edges_neighbors(editor_keep):
    """API ТЗ: nodes_at / edges_near / features_touching / bbox_features /
    snapshot_geometry / neighbors на «кадастре» (дыра + мультиполигон)."""
    layer, info = make_cadastral_layer()
    layer.startEditing()
    tags = info["tags"]
    try:
        idx = tindex.index_for_layer(layer)
        assert idx is not None
        idx.ensure_for(layer.extent())

        # узлы: общий угол участков (0,0),(1,0),(0,1),(1,1). В кадастре
        # внутренние узлы сдвинуты (PERTURBED): узел (1,1) = (10.3, 10.2)
        refs = idx.nodes_at(_pt(10.3, 10.2))
        assert {r.fid for r in refs} == {
            tags[(0, 0)], tags[(1, 0)], tags[(0, 1)], tags[(1, 1)]}
        assert all(r.layer_id == layer.id() for r in refs)

        # рёбра: точка на границе (5, 0) — ребро участка (0, 0)
        edges = idx.edges_near(_pt(5, 0))
        assert edges and all(e.fid == tags[(0, 0)] for e in edges)

        # touching + bbox
        touch = idx.features_touching(_pt(10.3, 10.2))
        assert set(touch[layer.id()]) == {
            tags[(0, 0)], tags[(1, 0)], tags[(0, 1)], tags[(1, 1)]}
        bbox = idx.bbox_features(layer.extent())
        assert set(bbox[layer.id()]) == set(tags.values())

        # снимок и соседи
        snap = idx.snapshot_geometry(layer, tags[(2, 0)])
        assert snap is not None and snap[1] is False
        # у участка 2,0 есть дыра: одно часть, два кольца
        assert len(snap[0][0]) == 2
        nbrs = idx.neighbors(tags[(0, 0)], layer, depth=1)
        assert set(nbrs[layer.id()]) == {
            tags[(0, 1)], tags[(1, 0)], tags[(1, 1)]}

        assert idx.check_integrity() == []
    finally:
        layer.rollBack()


def test_prefetch_builds_tiles():
    """Синхронная сборка области (prefetch): после неё запросы не строят
    плиток (приёмка «первая операция в новой области <= 0,5 с»)."""
    layer, tags = make_grid_layer(5, 5)
    layer.startEditing()
    try:
        idx = tindex.index_for_layer(layer)
        assert idx is not None
        assert idx.ensure_for(layer.extent()) is True
        assert idx._tiles and not idx._dirty
        refs = idx.nodes_at(_pt(20, 20))
        assert {r.fid for r in refs} == {
            tags[(1, 1)], tags[(2, 1)], tags[(1, 2)], tags[(2, 2)]}
        assert idx.check_integrity() == []
    finally:
        layer.rollBack()
