# -*- coding: utf-8 -*-
"""Общие фикстуры QGIS-тестов TopoPolyEdit (этап 0 ТЗ).

Запуск:  QT_QPA_PLATFORM=offscreen python3 -m pytest tests/qgis -v
(в CI — см. .github/workflows/ci.yml; приложение QGIS поднимает
pytest-qgis автоматически).
"""
import os
import sys

import pytest

# корень репозитория — для импорта пакета topopolyedit
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from fixtures_data import (  # noqa: E402,F401
    make_cadastral_layer,
    make_grid_layer,
    make_grid_other_crs,
    make_tjunction_layer,
)


class _StubMsgBar(object):
    def pushMessage(self, *args, **kwargs):
        pass


class StubIface(object):
    """Минимальный QgisInterface: только messageBar и mapCanvas."""

    def __init__(self, canvas=None):
        self._canvas = canvas

    def messageBar(self):
        return _StubMsgBar()

    def mapCanvas(self):
        if self._canvas is None:
            raise RuntimeError(
                "mapCanvas не задан — для инструментов используйте "
                "фикстуру iface_canvas")
        return self._canvas


@pytest.fixture
def stub_iface():
    """Заглушка интерфейса для TopoEditor (без холста)."""
    return StubIface()


@pytest.fixture
def iface_canvas(qgis_canvas):
    """Заглушка интерфейса с настоящим холстом pytest-qgis."""
    return StubIface(qgis_canvas)


def _editing(layer):
    layer.startEditing()
    return layer


@pytest.fixture
def grid_layer():
    """Сетка 2x2 в режиме правки; откат после теста."""
    layer, tags = make_grid_layer(2, 2)
    _editing(layer)
    yield layer, tags
    layer.rollBack()


@pytest.fixture
def cadastral_layer():
    """«Кадастр» 3x2 в режиме правки; откат после теста."""
    layer, info = make_cadastral_layer()
    _editing(layer)
    yield layer, info
    layer.rollBack()


@pytest.fixture
def editor_keep(monkeypatch):
    """TopoEditor с режимом перехлёстов «оставить» (без диалогов).

    Общая фикстура тестов инструментов и валидатора (этап 1)."""
    from topopolyedit import settings as tp_settings
    import topopolyedit.topo_editor as te_module

    monkeypatch.setattr(
        te_module.settings, "overlap_mode",
        lambda: tp_settings.OVERLAP_KEEP)

    def _make(layer):
        return te_module.TopoEditor(stub_iface(), layer)

    return _make
