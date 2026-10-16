# -*- coding: utf-8 -*-
"""Регрессионные тесты исторических багов на настоящем QGIS (этап 0 ТЗ).

Каждый тест защищает конкретную ошибку из истории репозитория — на
реальном API, а не на стабах, которые эти ошибки пропускали:
- combineExtentWith/grow возвращают None и меняют объект на месте
  (скрытый баг «bbox = bbox.grow(...)»);
- импорт QgsGeometry в snapping_engine (v1.5.0: слои с CRS != CRS карты
  молча выпадали из прилипания);
- сигнатура _update_preview одним кортежем delta (v1.4.1: TypeError при
  каждом захвате ребра);
- enum_member на работающем Qt (PyQt5/PyQt6);
- равенство CRS-объектов (v1.6.3: кастомные СК с пустым authid);
- пул лент QgsRubberBand (v1.6.3: сцена холста росла при каждом Drag).
"""
import inspect
import re
from pathlib import Path

import pytest
from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsRectangle,
)
from qgis.PyQt.QtWidgets import QAction

from topology_asserts import layer_snapshot

PLUGIN_DIR = Path(__file__).resolve().parents[2] / "topopolyedit"


# --- combineExtentWith/grow возвращают None (меняют объект на месте) -----

def test_grow_and_combine_return_none_and_mutate_inplace():
    r = QgsRectangle(0.0, 0.0, 1.0, 1.0)
    res = r.grow(0.5)
    assert res is None, "grow() вернул значение — нельзя присваивать"
    assert r.width() == 2.0, "grow() должен менять прямоугольник на месте"
    r2 = QgsRectangle(0.0, 0.0, 1.0, 1.0)
    res2 = r2.combineExtentWith(QgsRectangle(2.0, 2.0, 3.0, 3.0))
    assert res2 is None, "combineExtentWith() вернул значение"
    assert r2.width() == 3.0


def test_sources_never_assign_grow_or_combine_result():
    """Скан исходников: результат grow/combineExtentWith не присваивается.

    Регрессия на скрытый баг присваивания (значение всегда None)."""
    bad = []
    pat = re.compile(r"=\s*[A-Za-z_][\w.]*\.(grow|combineExtentWith)\(")
    for py in sorted(PLUGIN_DIR.glob("*.py")):
        for n, line in enumerate(
                py.read_text(encoding="utf-8").splitlines(), 1):
            if pat.search(line):
                bad.append("{}:{}: {}".format(py.name, n, line.strip()))
    assert not bad, "присваивание результата grow/combineExtentWith:\n" \
        + "\n".join(bad)


# --- импорт QgsGeometry в snapping_engine (v1.5.0) ------------------------

def test_qgsgeometry_importable_in_snapping_engine():
    import topopolyedit.snapping_engine as se
    assert se.QgsGeometry is not None, (
        "QgsGeometry не импортирован в snapping_engine — слои с CRS != "
        "CRS карты молча выпадают из прилипания")


# --- сигнатура _update_preview (v1.4.1) -----------------------------------

def test_update_preview_takes_single_delta():
    from topopolyedit.topo_edge_move_tool import TopoEdgeMoveTool
    sig = inspect.signature(TopoEdgeMoveTool._update_preview)
    params = [name for name in sig.parameters if name != "self"]
    assert params == ["d_map"], (
        "регрессия v1.4.1: delta должен передаваться ОДНИМ кортежем, "
        "сигнатура сейчас {}".format(params))


# --- utils.enum_member на работающем Qt -----------------------------------

def test_enum_member_works_on_real_qt():
    from qgis.core import Qgis, QgsWkbTypes
    from topopolyedit import utils
    # реальная сигнатура хелпера: (owner, enum_name, member)
    assert utils.enum_member(Qgis, "MessageLevel", "Info") is not None
    assert utils.enum_member(
        QgsWkbTypes, "GeometryType", "PolygonGeometry") is not None


# --- равенство CRS-объектов (v1.6.3) ---------------------------------------

def test_crs_objects_equality_with_empty_authid():
    a = QgsCoordinateReferenceSystem("EPSG:3857")
    b = QgsCoordinateReferenceSystem("EPSG:3857")
    assert a.isValid() and b.isValid()
    assert a == b, "одинаковые СК должны быть равны как объекты"
    custom = QgsCoordinateReferenceSystem()  # невалидная, authid пуст
    assert not custom.isValid()
    assert not (custom == a), (
        "СК с пустым authid не должна считаться равной EPSG:3857 "
        "(регрессия строкового сравнения authid)")


# --- пул лент QgsRubberBand (v1.6.3) ---------------------------------------

def test_rubber_pool_does_not_grow_scene(iface_canvas):
    from topopolyedit.topo_move_tool import TopoMoveTool
    action = QAction("test", iface_canvas.mapCanvas())
    tool = TopoMoveTool(iface_canvas, action)
    rb1 = tool._make_polygon_rubber()
    tool._reset_poly_rubbers()
    rb2 = tool._make_polygon_rubber()
    # сброс обязателен: иначе следующая выдача из пула (индекс 1 при
    # пуле из 1) честно создаст НОВУЮ ленту — регрессия этапа 0
    tool._reset_poly_rubbers()
    assert rb1 is rb2, "лента должна переиспользоваться из пула"
    scene = iface_canvas.mapCanvas().scene()
    # scene.items() на настоящем Qt — список Python: только len()
    count_after_warm = len(scene.items())
    for _ in range(10):
        tool._make_polygon_rubber()
        tool._reset_poly_rubbers()
    assert len(scene.items()) == count_after_warm, (
        "регрессия v1.6.3: сцена холста растёт при перетаскивании — "
        "ленты не переиспользуются")
    tool.deactivate()


# --- состояние слоя битово (используется в приёмке отката) -----------------

def test_layer_snapshot_is_bitwise():
    from fixtures_data import make_grid_layer
    from qgis.core import QgsGeometry
    layer, _tags = make_grid_layer(1, 1)
    snap1 = layer_snapshot(layer)
    layer.startEditing()
    layer.beginEditCommand("сдвиг")
    f = next(layer.getFeatures())
    g2 = QgsGeometry(f.geometry())
    g2.translate(2.0, 1.0)  # меняет на месте; возвращает код операции
    layer.changeGeometry(f.id(), g2)
    layer.endEditCommand()
    snap2 = layer_snapshot(layer)
    assert snap1 != snap2
    layer.undoStack().undo()
    assert layer_snapshot(layer) == snap1, (
        "после undo слой должен быть битово идентичен исходному")
    layer.rollBack()


if __name__ == "__main__":
    raise SystemExit("Запускайте: python3 -m pytest tests/qgis -v")
