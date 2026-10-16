# -*- coding: utf-8 -*-
"""Тесты валидатора (этап 1 ТЗ) на настоящем QGIS.

Приёмка этапа 1:
  * перетащить узел так, чтобы рёбра полигона пересеклись -> слой
    битово НЕизменён, есть точка нарушения;
  * увод соседа в самопересечение ловится (правила применяются ко ВСЕМ
    затронутым полигонам, не только перемещаемым);
  * makeValid из конвейера убран: невалидный слой блокирует операцию,
    а не чинится молча;
  * производительность: валидация операции на 10 000 полигонов
    измеряется и пишется в лог (порог с запасом, CI-машины разные).
"""
import time

import pytest
from qgis.core import QgsGeometry, QgsPointXY, QgsFeature

from fixtures_data import (
    add_polygon_feature,
    make_grid_layer,
    make_single_polygon_layer,
    make_tjunction_layer,
)
from topology_asserts import layer_snapshot

from topopolyedit.core.validator import (
    validate_operation, default_rules, blocking,
    GeosValidity, NoSpikes, NoSameLayerOverlap, NoNewGaps,
    SharedBoundaryPreserved,
    SEV_ERROR, SEV_WARNING,
    CODE_SELF_INTERSECTION, CODE_SPIKE, CODE_DUP_VERTEX,
    CODE_OVERLAP, CODE_NEW_GAP, CODE_SHARED_BOUNDARY,
)


def _sq(x0, y0, x1, y1):
    return QgsGeometry.fromPolygonXY([[
        QgsPointXY(x0, y0), QgsPointXY(x1, y0),
        QgsPointXY(x1, y1), QgsPointXY(x0, y1)]])


def _bowtie():
    return QgsGeometry.fromWkt(
        "POLYGON((0 0, 10 0, 5 -5, 0 10))")


# ---------------------------------------------------------------- правила

def test_geos_validity_butterfly_has_point_and_error():
    vs = GeosValidity().check(7, _bowtie(), {})
    errs = blocking(vs)
    assert errs, "самопересечение должно быть error"
    assert any(v.code == CODE_SELF_INTERSECTION for v in errs)
    assert any(v.point is not None for v in errs), "нужна точка места"
    assert all(v.fid == 7 for v in vs)


def test_geos_validity_clean_polygon_passes():
    assert GeosValidity().check(1, _sq(0, 0, 10, 10), {}) == []


def test_nospikes_detects_needle_and_dup_vertex():
    # игла: угол в вершине (10,0) около 0.006 градуса
    needle = QgsGeometry.fromPolygonXY([[
        QgsPointXY(0, 0), QgsPointXY(10, 0), QgsPointXY(0, 0.001)]])
    vs = NoSpikes().check(1, needle, {})
    spikes = [v for v in vs if v.code == CODE_SPIKE]
    assert spikes, "игла должна ловиться как шип"
    assert all(v.severity == SEV_WARNING for v in spikes), \
        "по умолчанию шипы — warning"

    # та же игла при spike_severity=error — блокирует
    vs_err = NoSpikes().check(1, needle, {"spike_severity": SEV_ERROR})
    assert blocking(vs_err), "настройка серьёзности должна работать"

    # две подряд совпадающие вершины
    dup = QgsGeometry.fromPolygonXY([[
        QgsPointXY(0, 0), QgsPointXY(5, 0), QgsPointXY(5, 0),
        QgsPointXY(5, 5), QgsPointXY(0, 5)]])
    vs_dup = NoSpikes().check(2, dup, {})
    assert any(v.code == CODE_DUP_VERTEX and v.fid == 2 for v in vs_dup)

    # нормальный квадрат — чисто
    assert NoSpikes().check(3, _sq(0, 0, 10, 10), {}) == []


def test_overlap_rule_and_keep_choice():
    new_geoms = {1: _sq(0, 0, 6, 6)}
    neighbors = [(2, _sq(5, 0, 10, 5))]  # перехлёст 1x5
    vs = NoSameLayerOverlap().check(
        {"new_geoms": new_geoms, "neighbors": neighbors})
    errs = blocking(vs)
    assert errs and all(v.code == CODE_OVERLAP for v in errs)
    assert any(v.geom is not None for v in errs), "нужна геометрия заливки"

    # пользователь выбрал «оставить» — не нарушение
    vs_keep = NoSameLayerOverlap().check(
        {"new_geoms": new_geoms, "neighbors": neighbors,
         "overlap_allowed": True})
    assert vs_keep == []


def test_new_gap_detected_and_seam_ignored():
    # два смежных квадрата 0..1 и 1..2 по x; правый уезжает на 0.5 —
    # щель 0.5x1
    old_geoms = {1: _sq(0, 0, 1, 1), 2: _sq(1, 0, 2, 1)}
    new_geoms = {1: _sq(0, 0, 1, 1), 2: _sq(1.5, 0, 2.5, 1)}
    vs = NoNewGaps().check(
        {"old_geoms": old_geoms, "new_geoms": new_geoms, "neighbors": []})
    errs = blocking(vs)
    assert errs and errs[0].code == CODE_NEW_GAP
    assert errs[0].geom is not None
    assert abs(errs[0].geom.area() - 0.5) < 1e-6, "щель 0.5x1"

    # щель-«шов» площадью 1e-10 от полигона 1.0 — порог 1e-8 отсекает
    new_seam = {1: _sq(0, 0, 1 - 1e-10, 1), 2: _sq(1, 0, 2, 1)}
    vs_seam = NoNewGaps().check(
        {"old_geoms": old_geoms, "new_geoms": new_seam, "neighbors": []})
    assert blocking(vs_seam) == [], "шов стыковки не нарушение"


def test_shared_boundary_preserved_and_lost():
    # общий узел (1,0) у двух квадратов; переезд ВМЕСТЕ — не нарушение
    old_geoms = {1: _sq(0, 0, 1, 1), 2: _sq(1, 0, 2, 1)}
    moved_together = {1: _sq(0.5, 0.5, 1.5, 1.5),
                      2: _sq(1.5, 0.5, 2.5, 1.5)}
    vs = SharedBoundaryPreserved().check(
        {"old_geoms": old_geoms, "new_geoms": moved_together,
         "neighbors": []})
    assert vs == [], "узел переехал со всеми копиями — чисто"

    # разъезд: у правого узел остался, у левого уехал
    torn = {1: _sq(0.5, 0.5, 1.4, 1.4), 2: _sq(1.5, 0.5, 2.5, 1.5)}
    vs_torn = SharedBoundaryPreserved().check(
        {"old_geoms": old_geoms, "new_geoms": torn, "neighbors": []})
    lost = [v for v in vs_torn if v.code == CODE_SHARED_BOUNDARY]
    assert lost, "потеря общего узла должна замечаться"
    assert lost[0].severity == SEV_WARNING, "по умолчанию warning"


def test_validate_operation_checks_all_affected_polygons():
    """Приёмка 2: увод СОСЕДА в самопересечение ловится, хотя
    перемещаемый полигон валиден."""
    old_geoms = {1: _sq(0, 0, 10, 10), 2: _sq(10, 0, 20, 10)}
    new_geoms = {1: _sq(0, 0, 10, 10),          # не изменён — валиден
                 2: _bowtie()}                   # сосед сломан
    vs = validate_operation(old_geoms, new_geoms, [])
    errs = blocking(vs)
    assert any(v.code == CODE_SELF_INTERSECTION and v.fid == 2
               for v in errs), "правила применяются ко всем затронутым"


# -------------------------------------------------- интеграция (приёмка)

def test_move_creating_self_intersection_blocked(qgis_canvas):
    """Приёмка 1: узел заводит полигон в самопересечение -> блокировка,
    слой битово неизменён, есть точка нарушения."""
    layer, tags = make_grid_layer(1, 1)
    layer.startEditing()
    try:
        from topopolyedit import settings as tp_settings
        import topopolyedit.topo_editor as te_module
        old_mode = te_module.settings.overlap_mode
        te_module.settings.overlap_mode = lambda: tp_settings.OVERLAP_KEEP
        try:
            before = layer_snapshot(layer)
            ed = te_module.TopoEditor(None, layer)
            # угол (10,10) -> (5,-5): кольцо (0,0),(10,0),(5,-5),(0,10)
            # самопересекается на ребре (0,0)-(10,0)
            rep = ed.move_vertex(_pt(10, 10), _pt(5, -5))
            assert rep.get("error"), "операция обязана быть заблокирована"
            assert rep.get("violations"), "нужен список нарушений"
            pts = [v["point"] for v in rep["violations"] if v["point"]]
            assert pts, "валидатор сообщает место нарушения"
            assert abs(pts[0][0] - 10.0 / 3.0) < 0.5, \
                "точка пересечения около (3.33, 0): {}".format(pts[0])
            assert layer_snapshot(layer) == before, \
                "слой битово не изменился"
        finally:
            te_module.settings.overlap_mode = old_mode
    finally:
        layer.rollBack()


def test_makevalid_is_not_applied_silently():
    """makeValid из конвейера убран: невалидная фича блокирует операцию,
    а не чинится молча."""
    layer = make_single_polygon_layer("bowtie_layer")
    g = QgsGeometry.fromWkt("POLYGON((0 0, 2 0, 0 2, 2 2, 0 0))")
    fid = add_polygon_feature(layer, g)
    layer.startEditing()
    try:
        from topopolyedit import settings as tp_settings
        import topopolyedit.topo_editor as te_module
        old_mode = te_module.settings.overlap_mode
        te_module.settings.overlap_mode = lambda: tp_settings.OVERLAP_KEEP
        try:
            before = layer_snapshot(layer)
            ed = te_module.TopoEditor(None, layer)
            # двигаем невалидную фичу целиком (все вершины совпадают с
            # узлом (2,0)? нет: двигаем вершину (2,0) -> (3,0), кольцо
            # остаётся самопересечным)
            rep = ed.move_vertex(_pt(2, 0), _pt(3, 0))
            assert rep.get("error"), "невалидная геометрия блокирует"
            assert layer_snapshot(layer) == before
            # фича осталась битово той же (не отремонтирована)
            after_geom = None
            for f in layer.getFeatures():
                if f.id() == fid:
                    after_geom = f.geometry()
            assert after_geom.equals(g), "молча makeValid запрещён"
        finally:
            te_module.settings.overlap_mode = old_mode
    finally:
        layer.rollBack()


def test_move_makevalid_repair_with_decider():
    """При включённой настройке и согласии пользователя makeValid
    применяется ОТКРЫТО (report помечает ремонт)."""
    layer = make_single_polygon_layer("bowtie_repair")
    fid = add_polygon_feature(layer, QgsGeometry.fromWkt(
        "POLYGON((0 0, 2 0, 0 2, 2 2, 0 0))"))
    layer.startEditing()
    try:
        import topopolyedit.topo_editor as te_module
        from topopolyedit import settings as tp_settings
        old_mode = te_module.settings.overlap_mode
        old_offer = te_module.settings.offer_makevalid
        te_module.settings.overlap_mode = lambda: tp_settings.OVERLAP_KEEP
        te_module.settings.offer_makevalid = lambda: True
        try:
            ed = te_module.TopoEditor(None, layer,
                                      makevalid_decider=lambda info: True)
            rep = ed.move_vertex(_pt(2, 0), _pt(3, 0))
            assert not rep.get("error"), rep
            assert rep.get("makevalid_repaired") == 1, \
                "ремонт должен быть помечен в отчёте"
            f = next(layer.getFeatures())
            assert f.geometry().isGeosValid(), "ремонт дал валидную форму"
            assert layer.undoStack().count() >= 1
        finally:
            te_module.settings.overlap_mode = old_mode
            te_module.settings.offer_makevalid = old_offer
    finally:
        layer.rollBack()


def test_tjunction_tear_now_blocked_by_gap_rule(qgis_canvas):
    """Этап 1: разъезд T-примыкания создаёт щель -> операция блокируется
    (раньше щель возникала молча; автовставка узла — этап 2)."""
    layer, tags = make_tjunction_layer()
    layer.startEditing()
    try:
        from topopolyedit import settings as tp_settings
        import topopolyedit.topo_editor as te_module
        old_mode = te_module.settings.overlap_mode
        te_module.settings.overlap_mode = lambda: tp_settings.OVERLAP_KEEP
        try:
            before = layer_snapshot(layer)
            ed = te_module.TopoEditor(None, layer)
            rep = ed.move_vertex(_pt(25, 10), _pt(27, 12))
            assert rep.get("error"), "щель 20 ед.кв. обязана блокировать"
            codes = {v["code"] for v in (rep.get("violations") or [])}
            assert CODE_NEW_GAP in codes, \
                "нарушение — новая щель, коды: {}".format(codes)
            assert layer_snapshot(layer) == before, "слой не тронут"
        finally:
            te_module.settings.overlap_mode = old_mode
    finally:
        layer.rollBack()


def test_valid_shared_node_move_still_works(editor_keep, grid_layer):
    """Валидатор не блокирует корректные операции (регрессия удобства)."""
    layer, _tags = grid_layer
    ed = editor_keep(layer)
    rep = ed.move_vertex(_pt(10, 10), _pt(12, 11))
    assert not rep.get("error"), rep
    assert rep["moved"] == 4


def test_perf_validation_on_10000_polygons(qgis_canvas):
    """Приёмка (этап 1): на 10 000 полигонов валидация операции узла
    укладывается в бюджет; фактическое время пишется в лог."""
    layer = make_single_polygon_layer("perf_grid")
    n = 100  # 100x100 = 10 000 участков
    cell = 10.0
    feats = []
    fields = layer.fields()
    for j in range(n):
        for i in range(n):
            x, y = i * cell, j * cell
            f = QgsFeature(fields)
            f.setGeometry(QgsGeometry.fromPolygonXY([[
                QgsPointXY(x, y), QgsPointXY(x + cell, y),
                QgsPointXY(x + cell, y + cell), QgsPointXY(x, y + cell)]]))
            f.setAttribute("id", "{},{}".format(i, j))
            feats.append(f)
    layer.dataProvider().addFeatures(feats)
    layer.updateExtents()
    layer.startEditing()
    try:
        import topopolyedit.topo_editor as te_module
        from topopolyedit import settings as tp_settings
        old_mode = te_module.settings.overlap_mode
        te_module.settings.overlap_mode = lambda: tp_settings.OVERLAP_KEEP
        try:
            ed = te_module.TopoEditor(None, layer)
            # внутренний узел сетки: 4 смежных полигона
            t0 = time.monotonic()
            rep = ed.move_vertex(_pt(500, 500), _pt(501, 502))
            dt = time.monotonic() - t0
            assert not rep.get("error"), rep
            print("PERF: валидация+запись узла на 10 000 полигонов: "
                  "{:.1f} мс".format(dt * 1000.0))
            assert dt < 2.0, (
                "валидация слишком медленная: {:.0f} мс (бюджет ТЗ 150 мс "
                "на узел — см. лог, порог теста с запасом на CI)".format(
                    dt * 1000.0))
        finally:
            te_module.settings.overlap_mode = old_mode
    finally:
        layer.rollBack()


def _pt(x, y):
    return QgsPointXY(x, y)


if __name__ == "__main__":
    raise SystemExit("Запускайте: python3 -m pytest tests/qgis -v")
