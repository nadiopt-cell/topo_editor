# -*- coding: utf-8 -*-
"""Приёмка этапа 4 ТЗ: правка нескольких слоёв одной операцией.

Проверяются все четыре пункта приёмки ТЗ:
  * два слоя в правке с общим узлом: перемещение меняет оба; одна
    отмена откатывает оба (OperationLog);
  * сбой записи во втором слое (мок changeGeometry) -> оба слоя битово
    неизменены (MultiLayerCommitter, destroyEditCommand на всех);
  * слой в другой СК (с пересчётом): узлы остаются совмещёнными в
    пределах допуска (единая точка карты + обратное преобразование);
  * оба слоя в одном GeoPackage с transaction mode: одна операция,
    один откат, без рассинхронизации (транзакционные группы QGIS).

Плюс: чёрный список правки и режим «только активный слой».
"""
import pytest
from qgis.core import (
    QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsGeometry,
    QgsPointXY, QgsProject, QgsVectorLayer,
)

from fixtures_data import make_grid_layer

from topopolyedit import operation_log as oplog
from topopolyedit import settings
from topopolyedit.multi_editor import MultiLayerEditor


@pytest.fixture(autouse=True)
def _clean_journal():
    oplog.clear()
    yield
    oplog.clear()


def _project():
    """Текущий проект QGIS (pytest-qgis не даёт фикстуру qgis_project)."""
    return QgsProject.instance()


@pytest.fixture
def project_crs_3857():
    """Проектный CRS = EPSG:3857 (единая точка операции — метры)."""
    _project().setCrs(QgsCoordinateReferenceSystem("EPSG:3857"))
    yield _project()
    # вернуть режим транзакций, если тест его менял
    try:
        _project().setTransactionMode(_transaction_mode_disabled())
    except Exception:
        pass


@pytest.fixture
def two_shared_node_layers():
    """Два memory-слоя в правке с общими узлами сетки 2x1."""
    from topopolyedit import topology_index as tindex
    from topopolyedit import snapping_engine as snap_eng
    layer_a, _tags_a = make_grid_layer(2, 1, name="multi_a")
    layer_b, _tags_b = make_grid_layer(2, 1, name="multi_b")
    for lyr in (layer_a, layer_b):
        _project().addMapLayer(lyr)
        lyr.startEditing()
    yield layer_a, layer_b
    for lyr in (layer_a, layer_b):
        try:
            lyr.rollBack()
        except Exception:
            pass
    # снять индексы и кэш слоёв-эталонов ДО удаления слоёв: TTL-кэш
    # (0,75 с) иначе отдаёт удалённые слои следующим тестам
    for lyr in (layer_a, layer_b):
        try:
            tindex.drop_index(lyr)
        except Exception:
            pass
    try:
        snap_eng.invalidate_snap_layers_cache()
    except Exception:
        pass
    for lyr in (layer_a, layer_b):
        _project().removeMapLayer(lyr.id())


def _ring_pts(layer, fid):
    """Первая точка первой части первого кольца фичи."""
    for feat in layer.getFeatures():
        if feat.id() == fid:
            g = feat.geometry()
            parts = g.asPolygon() if not g.isMultipart() else \
                g.asMultiPolygon()[0]
            return parts[0][0]
    raise AssertionError("фича #{} не найдена".format(fid))


def _vertex_present(layer, pt, eps=1e-4):
    """True — вершина pt есть в геометриях слоя (с допуском eps)."""
    eps2 = eps * eps
    for feat in layer.getFeatures():
        g = feat.geometry()
        polys = [g.asPolygon()] if not g.isMultipart() else g.asMultiPolygon()
        for part in polys:
            for ring in part:
                for v in ring:
                    if v.sqrDist(QgsPointXY(pt)) <= eps2:
                        return True
    return False


# ---------------------------------------------------------------------------
# Приёмка 1: два слоя с общим узлом — двигаются оба, одна отмена
# ---------------------------------------------------------------------------

def test_two_layers_move_and_single_undo(project_crs_3857,
                                         two_shared_node_layers):
    layer_a, layer_b = two_shared_node_layers
    editor = MultiLayerEditor(None, layer_a)
    report = editor.move_vertex(QgsPointXY(10, 10), QgsPointXY(11, 11))
    assert report.get("error") is None, report.get("error")
    assert report.get("moved") == 4, report  # 2 полигона в каждом слое
    assert len(report.get("participants") or []) == 2
    # оба слоя содержат новую вершину и не содержат старую
    for lyr in (layer_a, layer_b):
        assert _vertex_present(lyr, QgsPointXY(11, 11)), lyr.name()
        assert not _vertex_present(lyr, QgsPointXY(10, 10)), lyr.name()

    # одна отмена откатывает ОБОИ слоя (OperationLog, ТЗ п. 4)
    op = oplog.last_operation()
    assert op is not None and len(op["layers"]) == 2
    ok, _undone = oplog.undo_operation(op, _project().mapLayer)
    assert ok
    oplog.drop_last_operation()
    for lyr in (layer_a, layer_b):
        assert _vertex_present(lyr, QgsPointXY(10, 10)), lyr.name()
        assert not _vertex_present(lyr, QgsPointXY(11, 11)), lyr.name()


# ---------------------------------------------------------------------------
# Приёмка 2: сбой записи во втором слое -> оба слоя битово неизменены
# ---------------------------------------------------------------------------

def test_write_failure_leaves_both_layers_untouched(
        project_crs_3857, two_shared_node_layers, monkeypatch):
    layer_a, layer_b = two_shared_node_layers

    def _fail(fid, geom):
        return False

    monkeypatch.setattr(layer_b, "changeGeometry", _fail)
    editor = MultiLayerEditor(None, layer_a)
    report = editor.move_vertex(QgsPointXY(10, 10), QgsPointXY(11, 11))
    assert report.get("error"), report
    assert report.get("moved", 0) == 0
    # битовая целостность ОБОИХ слоёв (destroyEditCommand на всех)
    for lyr in (layer_a, layer_b):
        assert _vertex_present(lyr, QgsPointXY(10, 10)), lyr.name()
        assert not _vertex_present(lyr, QgsPointXY(11, 11)), lyr.name()
    # журнальной записи нет: отмена не обещана
    assert oplog.last_operation() is None


# ---------------------------------------------------------------------------
# Приёмка 3: слой в другой СК — узлы совмещены в пределах допуска
# ---------------------------------------------------------------------------

def _transform_layer_to_4326(source_layer, name):
    """Копия геометрий слоя в EPSG:4326 (узлы совмещены с исходным)."""
    ct = QgsCoordinateTransform(
        source_layer.crs(),
        QgsCoordinateReferenceSystem("EPSG:4326"),
        QgsProject.instance())
    layer = QgsVectorLayer(
        "MultiPolygon?crs=EPSG:4326&index=no", name, "memory")
    assert layer.isValid()
    from qgis.core import QgsFeature, QgsField
    from qgis.PyQt.QtCore import QVariant
    layer.dataProvider().addAttributes([QgsField("id", QVariant.String)])
    layer.updateFields()
    feats = []
    for feat in source_layer.getFeatures():
        g = QgsGeometry(feat.geometry())
        assert g.transform(ct) == 0
        nf = QgsFeature(layer.fields())
        nf.setGeometry(g)
        feats.append(nf)
    layer.dataProvider().addFeatures(feats)
    layer.updateExtents()
    return layer


def test_different_crs_nodes_stay_aligned(project_crs_3857,
                                          two_shared_node_layers):
    layer_a, _unused = two_shared_node_layers
    layer_b = _transform_layer_to_4326(layer_a, "multi_crs")
    _project().addMapLayer(layer_b)
    layer_b.startEditing()
    try:
        # допуск совмещения слоя B (градусная СК -> ~0.11 м на земле)
        eps_b = settings.edit_match_eps(layer_b)
        assert eps_b > 0.05  # пропорционален погрешности преобразования

        editor = MultiLayerEditor(None, layer_a)
        report = editor.move_vertex(QgsPointXY(10, 10),
                                    QgsPointXY(11.5, 11.5))
        assert report.get("error") is None, report.get("error")
        assert len(report.get("participants") or []) == 2

        # узлы СОВМЕЩЕНЫ: вершина слоя B после обратного преобразования
        # в CRS карты совпадает с новой вершиной слоя A в пределах
        # допуска, пропорционального погрешности преобразования
        assert _vertex_present(layer_a, QgsPointXY(11.5, 11.5))
        ct = QgsCoordinateTransform(
            layer_b.crs(), layer_a.crs(), QgsProject.instance())
        best = None
        for feat in layer_b.getFeatures():
            g = feat.geometry()
            polys = g.asMultiPolygon() if g.isMultipart() else [g.asPolygon()]
            for part in polys:
                for ring in part:
                    for v in ring:
                        map_pt = ct.transform(QgsPointXY(v))
                        d = map_pt.sqrDist(QgsPointXY(11.5, 11.5))
                        if best is None or d < best:
                            best = d
        assert best is not None
        assert best ** 0.5 <= eps_b + 0.05  # допуск совмещения узлов
    finally:
        try:
            layer_b.rollBack()
        except Exception:
            pass
        _project().removeMapLayer(layer_b.id())


# ---------------------------------------------------------------------------
# Приёмка 4: оба слоя в одном GeoPackage с transaction mode
# ---------------------------------------------------------------------------

def _make_gpkg_two_layers(tmp_path):
    """GeoPackage с двумя таблицами-сетками (общие узлы)."""
    from qgis.core import QgsVectorFileWriter
    path = str(tmp_path / "topo_multi.gpkg")
    layer_a, _t = make_grid_layer(2, 1, name="gpkg_a")
    layer_b, _t2 = make_grid_layer(2, 1, name="gpkg_b")
    opts = QgsVectorFileWriter.SaveVectorOptions()
    opts.driverName = "GPKG"
    opts.layerName = "t1"
    opts.fileEncoding = "UTF-8"
    err = QgsVectorFileWriter.writeAsVectorFormatV3(
        layer_a, path, layer_a.transformContext(), opts)
    assert err[0] == QgsVectorFileWriter.NoError, err
    opts2 = QgsVectorFileWriter.SaveVectorOptions()
    opts2.driverName = "GPKG"
    opts2.layerName = "t2"
    opts2.fileEncoding = "UTF-8"
    opts2.actionOnExistingFile = QgsVectorFileWriter.CreateOrOverwriteLayer
    err2 = QgsVectorFileWriter.writeAsVectorFormatV3(
        layer_b, path, layer_b.transformContext(), opts2)
    assert err2[0] == QgsVectorFileWriter.NoError, err2
    return path


def test_transaction_group_single_undo(tmp_path, project_crs_3857):
    path = _make_gpkg_two_layers(tmp_path)
    try:
        _project().setTransactionMode(_transaction_mode_automatic())
    except Exception:
        pytest.skip("QGIS без TransactionMode (старая версия)")

    layer_a = QgsVectorLayer("{}|layername=t1".format(path), "t1", "ogr")
    layer_b = QgsVectorLayer("{}|layername=t2".format(path), "t2", "ogr")
    assert layer_a.isValid() and layer_b.isValid()
    _project().addMapLayer(layer_a)
    _project().addMapLayer(layer_b)
    layer_a.startEditing()
    layer_b.startEditing()
    try:
        # слои реально в ОДНОЙ транзакционной группе QGIS
        assert oplog.same_transaction_group([layer_a, layer_b]), \
            "QGIS не сгруппировал таблицы одного GeoPackage"

        editor = MultiLayerEditor(None, layer_a)
        report = editor.move_vertex(QgsPointXY(10, 10), QgsPointXY(11, 11))
        assert report.get("error") is None, report.get("error")
        assert report.get("moved") == 4, report
        for lyr in (layer_a, layer_b):
            assert _vertex_present(lyr, QgsPointXY(11, 11)), lyr.name()

        # в журнале операция помечена transaction_group: откат ОДНИМ undo
        op = oplog.last_operation()
        assert op is not None and op.get("transaction_group") is True
        ok, _undone = oplog.undo_operation(op, _project().mapLayer)
        assert ok
        oplog.drop_last_operation()
        # без рассинхронизации: ОБОИ слоя вернулись
        for lyr in (layer_a, layer_b):
            assert _vertex_present(lyr, QgsPointXY(10, 10)), lyr.name()
            assert not _vertex_present(lyr, QgsPointXY(11, 11)), lyr.name()
    finally:
        for lyr in (layer_a, layer_b):
            try:
                lyr.rollBack()
            except Exception:
                pass
            _project().removeMapLayer(lyr.id())


def _transaction_mode_automatic():
    """QgsProject.TransactionMode.AutomaticGroups (PyQt5/PyQt6)."""
    scope = getattr(QgsProject, "TransactionMode", None)
    if scope is not None and hasattr(scope, "AutomaticGroups"):
        return scope.AutomaticGroups
    return QgsProject.AutomaticGroups


def _transaction_mode_disabled():
    """TransactionMode.Disabled (значение по умолчанию проекта)."""
    scope = getattr(QgsProject, "TransactionMode", None)
    if scope is not None and hasattr(scope, "Disabled"):
        return scope.Disabled
    return getattr(QgsProject, "TransactionDisabled", 0)


# ---------------------------------------------------------------------------
# Чёрный список правки и режим «только активный слой» (ТЗ п. 1(б))
# ---------------------------------------------------------------------------

def test_edit_blacklist_excludes_layer(project_crs_3857,
                                       two_shared_node_layers,
                                       monkeypatch):
    layer_a, layer_b = two_shared_node_layers
    monkeypatch.setattr(settings, "edit_disabled_ids",
                        lambda: {layer_b.id()})
    # исключённый слой остаётся эталоном отсечения (как до этапа 4),
    # что при общем узле честно режет перемещаемые; для чистой проверки
    # чёрного списка отключаем per-layer отсечение по слою B
    monkeypatch.setattr(settings, "clip_mode_for",
                        lambda lid: settings.CLIP_IGNORE
                        if lid == layer_b.id() else settings.CLIP_CLIP)
    editor = MultiLayerEditor(None, layer_a)
    report = editor.move_vertex(QgsPointXY(10, 10), QgsPointXY(11, 11))
    assert report.get("error") is None, report.get("error")
    assert report.get("moved") == 2, report  # только слой A
    assert [n for n in report["participants"]] == [layer_a.name()]
    assert _vertex_present(layer_a, QgsPointXY(11, 11))
    assert not _vertex_present(layer_b, QgsPointXY(11, 11))
    assert _vertex_present(layer_b, QgsPointXY(10, 10))


def test_active_only_scope(project_crs_3857, two_shared_node_layers,
                           monkeypatch):
    layer_a, layer_b = two_shared_node_layers
    monkeypatch.setattr(settings, "edit_scope",
                        lambda: settings.EDIT_SCOPE_ACTIVE)
    # слой B в правке, но вне области операции: по ТЗ (п. 7) остаётся
    # эталоном отсечения, что честно режет перемещаемые A при общем
    # узле; для чистой проверки ОБЛАСТИ правки отключаем отсечение
    monkeypatch.setattr(settings, "clip_mode_for",
                        lambda lid: settings.CLIP_IGNORE
                        if lid == layer_b.id() else settings.CLIP_CLIP)
    editor = MultiLayerEditor(None, layer_a)
    report = editor.move_vertex(QgsPointXY(10, 10), QgsPointXY(11, 11))
    assert report.get("error") is None, report.get("error")
    assert report.get("moved") == 2, report
    assert len(report["participants"]) == 1
    assert _vertex_present(layer_a, QgsPointXY(11, 11))
    assert not _vertex_present(layer_b, QgsPointXY(11, 11))
