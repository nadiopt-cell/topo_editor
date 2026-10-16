# -*- coding: utf-8 -*-
"""Приёмка этапа 2 ТЗ: T-примыкания и автовставка вершин.

Сценарии построены так, чтобы при движении T-узла ребро соседа,
следуя за узлом, ОСТАВАЛОСЬ склеенным с ребром перемещаемого полигона:
ребро B лежит на ребре A от T-узла до КОНЦА ребра A (общая вершина).
Тогда после залатывания край A (новая вершина -> общий конец) совпадает
с новым ребром B — щелей и перехлёстов нет (приёмка ТЗ). Для T-узла
в середине длинного совпадающего участка любой сдвиг рождает клин
между прямым ребром B и согнутым ребром A — такие операции честно
блокирует валидатор (покрыто тестами этапа 1).

Требования ТЗ (этап 2, «Приёмка»):
  1. полигон B имеет на ребре вершину без пары у A; перемещаем узел B ->
     в A появляется вершина, оба полигона двигаются, щелей и перехлёста
     нет (test_t_node_move_patches_neighbor);
  2. то же для перемещения ребра с двумя T-концами
     (test_edge_move_with_two_t_ends);
  3. прилипание к ребру своего слоя вставляет вершину в это ребро
     (test_snap_edge_inserts_vertex_into_own_layer_edge);
  4. работает для дыр (внутренних колец) и частей мультиполигонов
     (test_t_patch_on_hole_ring, test_t_patch_on_multipolygon_part);
  5. правило валидатора NoTJunction: узел на чужом ребре без вершины —
     warning, не блокирует (test_no_t_junction_rule_warns_not_blocks);
  6. пассажир: чужая T-вершина на перемещаемом ребре следует за ним
     (test_edge_move_moves_rider_t_vertex — этап сбора; полная запись
     с такой геометрией остаётся на контроле валидатора).

Запуск: QT_QPA_PLATFORM=offscreen python3 -m pytest tests/qgis -v
"""
import pytest
from qgis.core import QgsFeature, QgsGeometry, QgsPointXY

from fixtures_data import (
    make_cadastral_layer,
    make_single_polygon_layer,
    make_tjunction_layer,
    add_polygon_feature,
)
from topology_asserts import assert_topology_ok, layer_geoms

from topopolyedit.geo_utils import norm_polys, contains_vertex
from topopolyedit.core.validator import (
    validate_operation, blocking, CODE_T_JUNCTION, SEV_WARNING,
)


def _pt(x, y):
    return QgsPointXY(x, y)


def _poly(pts):
    ring = [QgsPointXY(x, y) for x, y in pts]
    ring.append(QgsPointXY(ring[0]))
    return QgsGeometry.fromPolygonXY([ring])


def _geom_by_tag(layer, tag):
    for f in layer.getFeatures():
        if f["id"] == tag:
            return f.geometry()
    raise AssertionError("фича {} не найдена".format(tag))


def _has_vertex(geom, pt, eps=1e-6):
    nr = norm_polys(geom)
    return bool(nr) and contains_vertex(nr[0], pt, eps * eps)


def _layer_from_polys(name, shapes):
    """Слой из [(тег, геометрия), ...] в режиме правки не переводится."""
    layer = make_single_polygon_layer(name)
    for tag, geom in shapes:
        add_polygon_feature(layer, geom, tag)
    return layer


@pytest.fixture
def tj_layer():
    """Пара полигонов с T-примыканием (фикстура этапа 0) в правке."""
    layer, tags = make_tjunction_layer()
    layer.startEditing()
    yield layer, tags
    layer.rollBack()


# ---------------------------------------------------------------------------
# Приёмка 1: узел B на ребре A — в A появляется вершина, оба двигаются
# ---------------------------------------------------------------------------

def _open_t_layer(name):
    """A (0,0)-(20,10) + треугольник B над ребром A.

    Нижнее ребро B (5,10)-(20,10) лежит на верхнем ребре A: T-узел
    (5,10) без вершины у A, правый конец (20,10) — общая вершина."""
    return _layer_from_polys(name, [
        ("A", _poly([(0, 0), (20, 0), (20, 10), (0, 10)])),
        ("B", _poly([(5, 10), (20, 10), (20, 20)])),
    ])


def test_t_node_move_patches_neighbor(editor_keep):
    layer = _open_t_layer("t_open")
    layer.startEditing()
    try:
        ed = editor_keep(layer)
        old_pt = _pt(5, 10)   # T-узел B на ребре A без вершины
        new_pt = _pt(8, 13)
        rep = ed.move_vertex(old_pt, new_pt)
        assert not rep.get("error"), rep
        # оба полигона двигаются: B (вершина) и A (залатанное ребро)
        assert rep["moved"] == 2, rep
        ga = _geom_by_tag(layer, "A")
        gb = _geom_by_tag(layer, "B")
        # в A появилась вершина в новом положении узла, старой копии нет
        assert _has_vertex(ga, new_pt), "в A нет вершины в новом положении"
        assert not _has_vertex(ga, old_pt), "в A осталась вершина старом месте"
        # ребро A следует за ребром B: обе вершины общего края на месте
        assert _has_vertex(ga, _pt(0, 10)) and _has_vertex(ga, _pt(20, 10))
        assert _has_vertex(gb, new_pt) and _has_vertex(gb, _pt(20, 10))
        # щелей и перехлёста нет, валидность сохранена
        assert_topology_ok(layer, holes_before=0, msg="t-node")
        # залатывание сняло T-примыкание: предупреждений нет
        assert not (rep.get("warnings") or []), rep.get("warnings")
    finally:
        layer.rollBack()


# ---------------------------------------------------------------------------
# Приёмка 2: перемещение ребра с двумя T-концами
# ---------------------------------------------------------------------------

def test_edge_move_with_two_t_ends(editor_keep):
    """Нижнее ребро B (оба конца на верхнем ребре A) уезжает вверх:
    в A появляются вершины в новых положениях концов, средний сегмент
    ребра A следует за B, щелей и перехлёста нет."""
    layer = _layer_from_polys("t_two_ends", [
        ("A", _poly([(0, 0), (30, 0), (30, 20), (0, 20)])),
        ("B", _poly([(10, 20), (20, 20), (20, 30), (10, 30)])),
    ])
    layer.startEditing()
    try:
        ed = editor_keep(layer)
        anchors = [_pt(10, 20), _pt(20, 20)]
        rep = ed.move_edge(anchors, (0.0, 2.0))
        assert not rep.get("error"), rep
        assert rep["moved"] == 2, rep
        ga = _geom_by_tag(layer, "A")
        gb = _geom_by_tag(layer, "B")
        # в A вершины в новых положениях концов (ребро A догнало B)
        assert _has_vertex(ga, _pt(10, 22)), "A: нет вершины (10, 22)"
        assert _has_vertex(ga, _pt(20, 22)), "A: нет вершины (20, 22)"
        # концы ребра A на месте
        assert _has_vertex(ga, _pt(0, 20)) and _has_vertex(ga, _pt(30, 20))
        # нижнее ребро B уехало вверх, верх не тронут
        assert _has_vertex(gb, _pt(10, 22)) and _has_vertex(gb, _pt(20, 22))
        assert _has_vertex(gb, _pt(10, 30)) and _has_vertex(gb, _pt(20, 30))
        assert_topology_ok(layer, holes_before=0, msg="two-t-ends")
    finally:
        layer.rollBack()


# ---------------------------------------------------------------------------
# Приёмка 3: прилипание к ребру своего слоя вставляет вершину
# ---------------------------------------------------------------------------

def test_snap_edge_inserts_vertex_into_own_layer_edge(editor_keep):
    """Новая позиция узла легла на ребро соседа своего слоя — в это ребро
    вставляется вершина в той же операции (t_inserted)."""
    layer = _layer_from_polys("t_snap_edge", [
        ("A", _poly([(0, 0), (20, 0), (20, 20), (0, 20)])),
        ("B", _poly([(20, 0), (35, 0), (35, 10), (20, 10)])),
        ("D", _poly([(25, 20), (45, 20), (45, 25), (25, 25)])),
    ])
    layer.startEditing()
    try:
        ed = editor_keep(layer)
        # узел (35,10) B уезжает вверх-вправо на ребро D (y=20, x в [25,45])
        rep = ed.move_vertex(_pt(35, 10), _pt(40, 20))
        assert not rep.get("error"), rep
        assert rep["moved"] == 1, rep
        assert rep.get("t_inserted", 0) == 1, rep
        gd = _geom_by_tag(layer, "D")
        assert _has_vertex(gd, _pt(40, 20)), "в D нет вершины (40, 20)"
        assert_topology_ok(layer, holes_before=0, msg="snap-edge")
    finally:
        layer.rollBack()


# ---------------------------------------------------------------------------
# Приёмка 4: дыры и части мультиполигонов
# ---------------------------------------------------------------------------

def test_t_patch_on_hole_ring(editor_keep):
    """T-узел на ребре ДЫРЫ: вершина вставляется во внутреннее кольцо,
    сосед следует за узлом (приёмка «работает для дыр»)."""
    layer = make_single_polygon_layer("t_hole")
    # M: внешний квадрат с дырой (5,10)-(25,20)
    outer = [QgsPointXY(0, 0), QgsPointXY(30, 0), QgsPointXY(30, 30),
             QgsPointXY(0, 30), QgsPointXY(0, 0)]
    hole = [QgsPointXY(5, 10), QgsPointXY(25, 10), QgsPointXY(25, 20),
            QgsPointXY(5, 20), QgsPointXY(5, 10)]
    f = QgsFeature(layer.fields())
    f.setGeometry(QgsGeometry.fromPolygonXY([outer, hole]))
    f.setAttribute("id", "M")
    layer.dataProvider().addFeatures([f])
    # B внутри дыры, левое ребро x=5 на ребре дыры
    add_polygon_feature(layer, _poly([(5, 12), (15, 12), (15, 18),
                                      (5, 18)]), "B")
    layer.startEditing()
    try:
        ed = editor_keep(layer)
        # узел B (5,12) уезжает ВНИЗ по ребру дыры: B расширяется в дыру,
        # ребро дыры следует за узлом (вершина в (5,11))
        rep = ed.move_vertex(_pt(5, 12), _pt(5, 11))
        assert not rep.get("error"), rep
        assert rep["moved"] == 2, rep
        gm = _geom_by_tag(layer, "M")
        assert _has_vertex(gm, _pt(5, 11)), "в дыре M нет вершины (5, 11)"
        assert not _has_vertex(gm, _pt(5, 12)), "старая копия в дыре осталась"
        assert_topology_ok(layer, holes_before=1, msg="hole-t")
    finally:
        layer.rollBack()


def test_t_patch_on_multipolygon_part(editor_keep):
    """T-узел на ребре ЧАСТИ мультиполигона: залатывание правит нужную
    часть, вторая (дальняя) часть не трогается.

    Ребро B заканчивается на КРАЮ ребра первой части (общая вершина
    (20,10)) — после залатывания край части совпадает с ребром B.
    T-узел в середине длинного совпадающего участка дал бы клин
    между прямым ребром B и согнутым ребром части (перехлёст) —
    его блокирует валидатор (см. докстринг модуля)."""
    part1 = _poly([(0, 0), (20, 0), (20, 10), (0, 10)])
    part2 = _poly([(50, 0), (55, 0), (55, 5), (50, 5)])
    layer = make_single_polygon_layer("t_multipoly", wkb="MultiPolygon")
    f = QgsFeature(layer.fields())
    f.setGeometry(QgsGeometry.collectGeometry([part1, part2]))
    f.setAttribute("id", "M")
    layer.dataProvider().addFeatures([f])
    add_polygon_feature(layer, _poly([(10, 10), (20, 10), (20, 20)]), "B")
    layer.startEditing()
    try:
        ed = editor_keep(layer)
        rep = ed.move_vertex(_pt(10, 10), _pt(13, 13))
        assert not rep.get("error"), rep
        assert rep["moved"] == 2, rep
        gm = _geom_by_tag(layer, "M")
        assert gm.isMultipart(), "мультиполигон потерял тип"
        assert _has_vertex(gm, _pt(13, 13)), "в части M нет вершины (13, 13)"
        # дальняя часть не сдвинулась
        pts = gm.asMultiPolygon()[1][0]
        xs = sorted(set(p.x() for p in pts))
        ys = sorted(set(p.y() for p in pts))
        assert xs == [50.0, 55.0] and ys == [0.0, 5.0], "дальняя часть уехала"
        assert_topology_ok(layer, holes_before=0, msg="multipoly-part")
    finally:
        layer.rollBack()


def test_cadastral_shared_node_move_still_works(editor_keep,
                                                cadastral_layer):
    """Регрессия на кадастре (мультиполигон + дыра): общий узел (20,20)
    двигается, дальняя часть мультиполигона стоит на месте."""
    layer, info = cadastral_layer
    tags = info["tags"]
    ed = editor_keep(layer)
    rep = ed.move_vertex(_pt(20, 20), _pt(22, 22))
    assert not rep.get("error"), rep
    assert rep["moved"] == 2, rep
    assert_topology_ok(layer, holes_before=1, msg="cadastr-node")
    far = [f.geometry() for f in layer.getFeatures()
           if f.id() == tags[(2, 1)]][0]
    pts = far.asMultiPolygon()[1][0]
    xs = sorted(set(p.x() for p in pts))
    assert xs == [50.0, 55.0], "дальняя часть уехала"


# ---------------------------------------------------------------------------
# Приёмка 5: правило NoTJunction — warning, не блокирует
# ---------------------------------------------------------------------------

def test_no_t_junction_rule_warns_not_blocks(tj_layer):
    """Узел остался на ребре соседа без вершины: код t_junction_no_node,
    серьёзность warning, blocking() пуст."""
    layer, tags = tj_layer
    g_a = _geom_by_tag(layer, "A")
    # узел (7,10) лежит на верхнем ребре A (0,10)-(20,10) без вершины
    violations = validate_operation(
        {}, {}, [(tags["A"], g_a)],
        moved_points=[_pt(7, 10)])
    codes = [v.code for v in violations]
    assert CODE_T_JUNCTION in codes, codes
    v = [x for x in violations if x.code == CODE_T_JUNCTION][0]
    assert v.severity == SEV_WARNING
    assert v.point is not None and abs(v.point.x() - 7.0) < 1e-9
    assert not blocking(violations), "warning не должен блокировать"
    # совпадение с ВЕРШИНОЙ соседа — не нарушение
    ok = validate_operation({}, {}, [(tags["A"], g_a)],
                            moved_points=[_pt(0, 10)])
    assert not ok, ok
    # точка вне рёбер соседа — не нарушение
    none_ = validate_operation({}, {}, [(tags["A"], g_a)],
                               moved_points=[_pt(7, 15)])
    assert not none_, none_


# ---------------------------------------------------------------------------
# Приёмка 6: пассажир — чужая T-вершина на перемещаемом ребре
# ---------------------------------------------------------------------------

def test_edge_move_moves_rider_t_vertex(editor_keep):
    """Сбор переноса ребра: чужая T-вершина C на перемещаемом ребре A
    переезжает проекцией на новое ребро (п. 6 требований).

    Проверяется этап СБОРА плана (_collect_edge_moved): полная запись
    с такой геометрией (нижнее ребро C наклоняется) остаётся на контроле
    валидатора — как и для любой перехлёстной/щелевой ситуации."""
    layer = _layer_from_polys("t_rider", [
        ("A", _poly([(0, 0), (30, 0), (30, 20), (0, 20)])),
        ("C", _poly([(26, 20), (38, 20), (38, 26), (26, 26)])),
    ])
    layer.startEditing()
    try:
        ed = editor_keep(layer)
        anchors = [_pt(30, 20), _pt(0, 20)]  # верхнее ребро A
        entries = ed._collect_edge_moved(anchors, (0.0, 2.0))
        by_fid = {e["fid"]: e for e in entries}
        assert len(by_fid) == 2, "ожидались A и C: %s" % list(by_fid)
        # A: ребро переехало
        pa = by_fid[_fid_of(layer, "A")]
        assert (contains_vertex(pa["polys"], _pt(0, 22), 1e-12) and
                contains_vertex(pa["polys"], _pt(30, 22), 1e-12))
        # C: пассажир (26,20) -> (26,22), остальные вершины на месте
        pc = by_fid[_fid_of(layer, "C")]
        assert contains_vertex(pc["polys"], _pt(26, 22), 1e-12), \
            "пассажир не переехал на новое ребро"
        assert not contains_vertex(pc["polys"], _pt(26, 20), 1e-12), \
            "пассажир остался на старом месте"
        assert (contains_vertex(pc["polys"], _pt(38, 20), 1e-12) and
                contains_vertex(pc["polys"], _pt(26, 26), 1e-12))
    finally:
        layer.rollBack()


def _fid_of(layer, tag):
    for f in layer.getFeatures():
        if f["id"] == tag:
            return f.id()
    raise AssertionError("фича {} не найдена".format(tag))


# ---------------------------------------------------------------------------
# Регрессия: общий узел сетки не ломается залатыванием
# ---------------------------------------------------------------------------

def test_shared_node_move_no_t_side_effects(editor_keep, grid_layer):
    """Перенос общего узла сетки: залатывание не добавляет лишних
    вершин соседям (все копии и так едут вместе)."""
    layer, _tags = grid_layer
    ed = editor_keep(layer)
    rep = ed.move_vertex(_pt(10, 10), _pt(12, 11))
    assert not rep.get("error"), rep
    assert rep["moved"] == 4, rep
    assert rep.get("t_inserted", 0) == 0, rep
    assert rep.get("warnings") in (None, []), rep.get("warnings")
    assert_topology_ok(layer, holes_before=0, msg="grid-node")
