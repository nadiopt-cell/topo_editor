# -*- coding: utf-8 -*-
"""Приёмка этапа 5 ТЗ: аудит слоя и безопасное исправление.

Фикстура «с подсадкой»: фоновая сетка (масса площади) + по одной
ошибке каждого класса (INVALID, GAP, OVERLAP, DUP_NODE, NEAR_NODE,
T_JUNCTION, SPIKE, SLIVER), разнесённые по слою. Критерии ТЗ:

  1) на подсаженном слое находятся ошибки ВСЕХ восьми кодов;
  2) на чистом «кадастре» — 0 находок (нет ложных срабатываний);
  3) «Исправить все (безопасные)» -> повторный прогон даёт 0 ошибок
     исправленных кодов (DUP_NODE/NEAR_NODE/T_JUNCTION/GAP),
     суммарная площадь изменилась <= 0,001 %;
  4) всё отменяется ОДНОЙ командой отмены (Ctrl+Z);
  5) клейка щели: площадь соседа вырастает ровно на площадь щели
     (в этой фикстуре щель закрывается слиянием почти совпавших
     узлов, а клейка честно отказывается — двойное исправление дало
     бы перехлёст; отдельный тест клейки проверяет точный прирост);
  6) производительность: 10 000 полигонов <= 60 с (время в лог CI
     через -rP).

Запуск: QT_QPA_PLATFORM=offscreen python3 -m pytest tests/qgis -v
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

# диагностика GAP-проверки в CI: pytest показывает captured stdout
# упавших тестов
os.environ.setdefault("TOPOPOLEDIT_GAP_DEBUG", "1")

from qgis.core import QgsFeature, QgsGeometry, QgsPointXY  # noqa: E402

from fixtures_data import (  # noqa: E402
    add_polygon_feature,
    make_cadastral_layer,
    make_single_polygon_layer,
)

from topopolyedit.layer_audit import FixEngine, run_audit_sync  # noqa: E402
from topopolyedit.core.checks import (  # noqa: E402
    CODE_DUP_NODE,
    CODE_GAP,
    CODE_INVALID,
    CODE_NEAR_NODE,
    CODE_OVERLAP,
    CODE_SPIKE,
    CODE_SLIVER,
    CODE_T_JUNCTION,
    SAFE_FIX_CODES,
)

# пороги как в «Порогах по умолчанию» ТЗ (единицы СК слоя до этапа 7)
OPTS = {
    "gap_max_area": 0.01,
    "near_node_tol": 0.01,
    "sliver_ratio": 1e-4,
    "seam_min_ratio": 1e-8,
    "spike_min_angle_deg": 0.5,
    "match_eps": 1e-6,
    "overlap_min_ratio": 1e-8,
}

GAP_A = [(210, 0), (220, 0), (220, 0.4), (220.02, 0.4), (220.02, 10),
         (210, 10)]
GAP_B = [(220.02, 0), (230, 0), (230, 10), (220.02, 10)]
GAP_BOT = [(210, -10), (230, -10), (230, 0), (210, 0)]


def _closed(pts):
    ring = [QgsPointXY(x, y) for x, y in pts]
    if ring[0] != ring[-1]:
        ring.append(QgsPointXY(ring[0]))
    return ring


def _add(layer, tag, pts):
    geom = QgsGeometry.fromPolygonXY([_closed(pts)])
    return add_polygon_feature(layer, geom, tag=tag)


def make_seeded_layer():
    """Слой с подсадкой всех восьми классов ошибок (MultiPolygon)."""
    layer = make_single_polygon_layer(name="audit_seeded",
                                      wkb="MultiPolygon")
    fids = {}
    # масса слоя: сетка 12x12 (144 участка, 14 400 ед. кв.)
    for j in range(12):
        for i in range(12):
            x, y = i * 10.0, j * 10.0
            _add(layer, "g{},{}".format(i, j),
                 [(x, y), (x + 10, y), (x + 10, y + 10), (x, y + 10)])
    # INVALID: «бабочка» (самопересечение)
    fids["invalid"] = _add(layer, "invalid",
                           [(200, 0), (202, 2), (200, 2), (202, 0)])
    # GAP: щель 0.02 x 0.4 = 0.008 ед. кв. (исправима, ширина > допуска
    # NEAR — углы не сливаются), замкнута снизу «крышкой», сверху —
    # уступом L-образного gap_a; все стыки — по РЁБРАМ (точечные
    # касания дробят объединение, и щель перестаёт быть дырой)
    fids["gap_a"] = _add(layer, "gap_a", GAP_A)
    fids["gap_b"] = _add(layer, "gap_b", GAP_B)
    fids["gap_bot"] = _add(layer, "gap_bot", GAP_BOT)
    # OVERLAP: перекрытие 50 ед. кв.; B больше — обрежется A
    fids["overlap_a"] = _add(layer, "overlap_a",
                             [(240, 0), (250, 0), (250, 10), (240, 10)])
    fids["overlap_b"] = _add(layer, "overlap_b",
                             [(245, -5), (255, -5), (255, 10),
                              (245, 10)])
    # DUP_NODE: две подряд вершины в 5e-9 (валидатор считает их
    # дублем и не даёт ложный SPIKE)
    fids["dup"] = _add(layer, "dup",
                       [(270, 0), (280, 0), (280, 10), (275, 10),
                        (275 + 5e-9, 10), (270, 10)])
    # NEAR_NODE: две безопасные пары (шаг 0.0005 < допуска, рёбра
    # коллинеарны продолжению) — слив двигает обе границы навстречу
    fids["near_f"] = _add(layer, "near_f",
                          [(300, 0), (310, 0), (310, 10), (300, 10)])
    fids["near_g"] = _add(layer, "near_g",
                          [(300, 10.0005), (310, 10.0005),
                           (310, 20), (300, 20)])
    # T_JUNCTION: оба конца общей границы висят на чужих рёбрах
    fids["tj_h"] = _add(layer, "tj_h",
                        [(340, 0), (360, 0), (360, 10), (340, 10)])
    fids["tj_i"] = _add(layer, "tj_i",
                        [(345, 10), (365, 10), (365, 20), (345, 20)])
    # SPIKE: игла с остриём (385, 15)
    fids["spike"] = _add(layer, "spike",
                         [(380, 0), (390, 0), (390, 10),
                          (385.0001, 10), (385, 15), (385, 10),
                          (380, 10)])
    # SLIVER: вытянутый треугольник 50 x 0.04
    fids["sliver"] = _add(layer, "sliver",
                          [(420, 0), (470, 0), (420, 0.04)])
    layer.updateExtents()
    return layer, fids


def _by_code(findings):
    out = {}
    for f in findings:
        out.setdefault(f.code, []).append(f)
    return out


def _geom_of(layer, fid):
    for f in layer.getFeatures():
        if f.id() == fid:
            return f.geometry()
    raise AssertionError("фича #{} не найдена".format(fid))


# ---------------------------------------------------------------------------
# 1) все ошибки находятся; 2) чистый слой — ноль находок
# ---------------------------------------------------------------------------

def test_seeded_layer_all_codes_found():
    layer, fids = make_seeded_layer()
    findings = run_audit_sync(layer, OPTS)
    by = _by_code(findings)

    assert len(by.get(CODE_INVALID, [])) >= 1, by.keys()
    assert any(f.fid == fids["invalid"] for f in by[CODE_INVALID])

    gaps = by.get(CODE_GAP, [])
    assert len(gaps) == 1, gaps
    assert gaps[0].fixable is True
    assert gaps[0].fid == fids["gap_a"]  # сосед по длиннейшей границе
    assert abs(gaps[0].area - 0.008) < 1e-9, gaps[0].area
    overlaps = by.get(CODE_OVERLAP, [])
    assert len(overlaps) == 1, overlaps
    pl = overlaps[0].payload
    assert {pl["a_fid"], pl["b_fid"]} == {fids["overlap_a"],
                                          fids["overlap_b"]}
    assert abs(overlaps[0].area - 50.0) < 1e-6
    assert abs(pl["area_a"] - 100.0) < 1e-6
    assert abs(pl["area_b"] - 150.0) < 1e-6  # 10 x 15

    dups = by.get(CODE_DUP_NODE, [])
    assert len(dups) == 1 and dups[0].fid == fids["dup"], dups

    nears = by.get(CODE_NEAR_NODE, [])
    # 2 пары фикстуры near_f/near_g (углы щели разнесены на 0.02 —
    # шире допуска, NEAR-пар не образуют)
    assert len(nears) == 2, [(n.fid, n.message) for n in nears]
    assert all(n.fixable for n in nears), \
        [(n.fid, n.message) for n in nears]

    tjs = by.get(CODE_T_JUNCTION, [])
    # 2 у T-фикстуры + 2 у щели (углы gap_a/gap_b на крышке gap_bot)
    assert len(tjs) == 4, [(t.fid, t.message) for t in tjs]
    assert all(t.fixable for t in tjs)

    spikes = by.get(CODE_SPIKE, [])
    assert len(spikes) == 2, [(s.fid, s.point) for s in spikes]
    assert any(s.fid == fids["spike"]
               and abs(s.point.x() - 385) < 1e-9
               and abs(s.point.y() - 15) < 1e-9 for s in spikes)

    slivers = by.get(CODE_SLIVER, [])
    assert len(slivers) == 1 and slivers[0].fid == fids["sliver"]
    assert slivers[0].fixable is False  # только показ


def test_clean_layer_zero_findings():
    layer, _info = make_cadastral_layer()
    findings = run_audit_sync(layer, OPTS)
    assert findings == [], [(f.code, f.fid, f.message) for f in findings]


# ---------------------------------------------------------------------------
# 3)+4) «Исправить все (безопасные)»: 0 ошибок, площадь, один Ctrl+Z
# ---------------------------------------------------------------------------

def test_fix_all_safe_rerun_clean_area_and_single_undo():
    from topology_asserts import layer_snapshot, total_area

    layer, fids = make_seeded_layer()
    snapshot_before = layer_snapshot(layer)
    area_before = total_area(layer)

    findings = run_audit_sync(layer, OPTS)
    batch = [f for f in findings
             if f.fixable and f.code in SAFE_FIX_CODES]
    engine = FixEngine(None, layer)
    report = engine.fix_findings(
        batch, u"Аудит: исправить все (безопасные)")
    assert report["error"] is None, report
    assert report["violations"] is None, report
    # все безопасные исправления применились без отказов: щель приклеена
    # к gap_a (длиннейшая общая граница), слияния NEAR прошли
    assert report["refused"] == [], report["refused"]

    # повторный прогон: 0 ошибок безопасных кодов
    findings2 = run_audit_sync(layer, OPTS)
    left = [f for f in findings2 if f.code in SAFE_FIX_CODES]
    assert left == [], [(f.code, f.fid, f.message) for f in left]

    # суммарная площадь изменилась не более чем на 0,001 %
    area_after = total_area(layer)
    pct = abs(area_after - area_before) / area_before * 100.0
    assert pct <= 0.001, pct
    assert report["area_change_pct"] <= 0.001

    # всё отменяется одной командой отмены (Ctrl+Z)
    layer.undoStack().undo()
    assert layer_snapshot(layer) == snapshot_before


def test_gap_glue_neighbor_area_grows_exactly():
    """Отдельная клейка: площадь соседа вырастает ровно на щель."""
    layer, fids = make_seeded_layer()
    findings = run_audit_sync(layer, OPTS)
    gap = [f for f in findings if f.code == CODE_GAP][0]
    area_a_before = _geom_of(layer, fids["gap_a"]).area()

    engine = FixEngine(None, layer)
    report = engine.fix_findings([gap], u"Аудит: клейка щели")
    assert report["error"] is None, report
    assert report["refused"] == [], report["refused"]

    area_a_after = _geom_of(layer, fids["gap_a"]).area()
    assert abs((area_a_after - area_a_before) - gap.area) < 1e-9, \
        (area_a_before, area_a_after, gap.area)
    # щель исчезла
    findings2 = [f for f in run_audit_sync(layer, OPTS)
                 if f.code == CODE_GAP]
    assert findings2 == []


# ---------------------------------------------------------------------------
# Ручные исправления: SPIKE, OVERLAP, INVALID (makeValid по выбору)
# ---------------------------------------------------------------------------

def test_spike_fix_removes_tip():
    layer, fids = make_seeded_layer()
    findings = run_audit_sync(layer, OPTS)
    needle = [f for f in findings if f.code == CODE_SPIKE
              and abs(f.point.x() - 385) < 1e-9
              and abs(f.point.y() - 15) < 1e-9]
    assert len(needle) == 1
    engine = FixEngine(None, layer)
    report = engine.fix_findings(needle, u"Аудит: удаление шипа")
    assert report["error"] is None, report
    assert report["refused"] == [], report["refused"]
    # остриё больше не вершина фичи
    g = _geom_of(layer, fids["spike"])
    pts = g.asMultiPolygon()[0][0] if g.isMultipart() else g.asPolygon()[0]
    assert not any(abs(p.x() - 385) < 1e-9 and abs(p.y() - 15) < 1e-9
                   for p in pts)
    # исходное место больше не помечено как шип
    rest = [f for f in run_audit_sync(layer, OPTS)
            if f.code == CODE_SPIKE
            and abs(f.point.x() - 385) < 1e-9
            and abs(f.point.y() - 15) < 1e-9]
    assert rest == []


def test_overlap_trim_smaller():
    layer, fids = make_seeded_layer()
    findings = run_audit_sync(layer, OPTS)
    overlap = [f for f in findings if f.code == CODE_OVERLAP][0]
    area_a_before = _geom_of(layer, fids["overlap_a"]).area()

    engine = FixEngine(None, layer)
    report = engine.fix_findings(
        [overlap], u"Аудит: обрезка перехлёста",
        overlap_decider=lambda f: "smaller")
    assert report["error"] is None, report
    assert report["refused"] == [], report["refused"]

    area_a_after = _geom_of(layer, fids["overlap_a"]).area()
    assert abs((area_a_before - area_a_after) - overlap.area) < 1e-6
    # перехлёст исчез
    rest = [f for f in run_audit_sync(layer, OPTS)
            if f.code == CODE_OVERLAP]
    assert rest == [], [(f.fid, f.message) for f in rest]


def test_invalid_makevalid_by_choice():
    layer, fids = make_seeded_layer()
    findings = run_audit_sync(layer, OPTS)
    inv = [f for f in findings if f.code == CODE_INVALID
           and f.fid == fids["invalid"]]
    assert inv, findings
    engine = FixEngine(None, layer)

    # пользователь отказался — слой не изменён; отказ фиксируется
    # в refused (пачка пуста — движок честно сообщает «нечего писать»)
    report = engine.fix_findings(
        inv, u"Аудит: makeValid",
        makevalid_decider=lambda f, g, a0, a1: False)
    assert report["fixed"] == 0, report
    assert not _geom_of(layer, fids["invalid"]).isGeosValid()

    # пользователь подтвердил — геометрия стала валидной
    report = engine.fix_findings(
        inv, u"Аудит: makeValid",
        makevalid_decider=lambda f, g, a0, a1: True)
    assert report["error"] is None, report
    assert report["refused"] == [], report["refused"]
    assert _geom_of(layer, fids["invalid"]).isGeosValid()
    rest = [f for f in run_audit_sync(layer, OPTS)
            if f.code == CODE_INVALID and f.fid == fids["invalid"]]
    assert rest == []


# ---------------------------------------------------------------------------
# 5) Производительность: 10 000 полигонов <= 60 с (ТЗ этап 5, п. 4)
# ---------------------------------------------------------------------------

def test_perf_10k_polygons_under_60s():
    layer = make_single_polygon_layer(name="audit_big",
                                      wkb="MultiPolygon")
    feats = []
    for j in range(100):
        for i in range(100):
            x, y = i * 10.0, j * 10.0
            f = QgsFeature(layer.fields())
            f.setGeometry(QgsGeometry.fromPolygonXY([_closed(
                [(x, y), (x + 10, y), (x + 10, y + 10), (x, y + 10)])]))
            f.setAttribute("id", "{},{}".format(i, j))
            feats.append(f)
    layer.dataProvider().addFeatures(feats)
    layer.updateExtents()

    t0 = time.time()
    findings = run_audit_sync(layer, OPTS)
    elapsed = time.time() - t0
    print("PERF аудит 10 000 полигонов: {:.1f} с (найдено: {})".format(
        elapsed, len(findings)))
    assert elapsed < 60.0, elapsed
    # сетка из точных смежных квадратов — ни одной ошибки
    assert findings == [], [(f.code, f.fid) for f in findings]


# ---------------------------------------------------------------------------
# Док-панель: таблица, фильтр (без холста — оверлей отключается)
# ---------------------------------------------------------------------------

def test_audit_dock_table_and_filter(stub_iface):
    from qgis.PyQt.QtCore import Qt

    from topopolyedit.audit_panel import AuditDock
    from topopolyedit.core.checks import CHECK_TITLES

    layer, _fids = make_seeded_layer()
    findings = run_audit_sync(layer, OPTS)

    dock = AuditDock(stub_iface)
    try:
        assert dock.table.columnCount() == 7
        dock._findings = list(findings)
        dock._refill()
        assert dock.table.rowCount() == len(findings)

        # фильтр по коду: только строки этого кода
        idx = dock.cmb_filter.findData(CODE_SPIKE)
        assert idx >= 0
        dock.cmb_filter.setCurrentIndex(idx)
        expected = len([f for f in findings if f.code == CODE_SPIKE])
        assert dock.table.rowCount() == expected

        # «Все коды» возвращает полный список
        dock.cmb_filter.setCurrentIndex(
            dock.cmb_filter.findData(""))
        assert dock.table.rowCount() == len(findings)

        # названия кодов для таблицы заведены для всех восьми
        for code in (CODE_INVALID, CODE_GAP, CODE_OVERLAP, CODE_DUP_NODE,
                     CODE_NEAR_NODE, CODE_T_JUNCTION, CODE_SPIKE,
                     CODE_SLIVER):
            assert code in CHECK_TITLES
    finally:
        dock.shutdown()
        dock.deleteLater()
