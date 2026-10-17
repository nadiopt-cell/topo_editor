# -*- coding: utf-8 -*-
"""Реестр проверок аудита слоя (этап 5 ТЗ, п. 1).

run_checks(ctx) прогоняет все проверки и возвращает список Finding;
run_layer_checks(layer, ...) строит контекст из QgsVectorLayer
(только qgis.core: чтение фич + QgsSpatialIndex — годится и для
фонового потока QgsTask) и вызывает run_checks.

Проверки регистрируются списком CHECK_CLASSES — по классу на модуль
(ТЗ). Сбой отдельной проверки пишется в журнал и не убивает аудит
(остальные проверки продолжаются).
"""

from qgis.core import QgsFeature, QgsSpatialIndex, QgsWkbTypes

from ...utils import log
from .base import (  # noqa: F401  (реэкспорт для удобства потребителей)
    Finding, CHECK_TITLES, SAFE_FIX_CODES, FIX_ORDER, DEFAULT_OPTS,
    CODE_INVALID, CODE_GAP, CODE_OVERLAP, CODE_DUP_NODE, CODE_NEAR_NODE,
    CODE_T_JUNCTION, CODE_SPIKE, CODE_SLIVER,
)
from .invalid_check import InvalidCheck
from .gap_check import GapCheck
from .overlap_check import OverlapCheck
from .dup_node_check import DupNodeCheck
from .near_node_check import NearNodeCheck
from .t_junction_check import TJunctionCheck
from .spike_check import SpikeCheck
from .sliver_check import SliverCheck
from . import fixes  # noqa: F401

CHECK_CLASSES = (InvalidCheck(), GapCheck(), OverlapCheck(),
                 DupNodeCheck(), NearNodeCheck(), TJunctionCheck(),
                 SpikeCheck(), SliverCheck())


def run_checks(ctx):
    """Прогоняет все проверки по контексту; возвращает [Finding].

    Сбой отдельной проверки — запись в журнал «TopoPolyEdit», остальные
    проверки продолжаются (аудит не должен умирать целиком)."""
    findings = []
    for check in CHECK_CLASSES:
        if ctx.get("cancelled") and ctx["cancelled"]():
            break
        try:
            findings.extend(check.run(ctx) or [])
        except Exception as exc:
            log(u"Аудит слоя: проверка {} не удалась — {}".format(
                getattr(check, "CODE", check.__class__.__name__), exc))
    findings.sort(key=lambda f: (f.code, f.fid if f.fid is not None
                                 else -1))
    return findings


def build_ctx(layer, opts=None, progress=None, cancelled=None):
    """Контекст проверок из полигонального QgsVectorLayer.

    Читает фичи и строит пространственный индекс габаритов — оба
    действия допустимы из фонового потока QgsTask (слой только
    читается). Словарь polys заполняется лениво, кэш общий.
    """
    merged = dict(DEFAULT_OPTS)
    if opts:
        merged.update(opts)
    geoms = []
    index = None
    try:
        index = QgsSpatialIndex()
    except Exception:
        index = None
    for f in layer.getFeatures():
        g = f.geometry()
        if g is None or g.isNull() or g.isEmpty():
            continue
        try:
            if g.type() != QgsWkbTypes.PolygonGeometry:
                continue
        except Exception:
            continue
        geoms.append((f.id(), g))
        if index is not None:
            try:
                index.addFeature(f)
            except Exception:
                pass
    return {
        "geoms": geoms,
        "polys": {},
        "index": index,
        "opts": merged,
        "progress": progress,
        "cancelled": cancelled,
        "layer_id": layer.id() if layer is not None else u"",
        "layer_name": layer.name() if layer is not None else u"",
    }


def run_layer_checks(layer, opts=None, progress=None, cancelled=None):
    """Полный аудит слоя: контекст + прогон проверок; [Finding]."""
    ctx = build_ctx(layer, opts=opts, progress=progress,
                    cancelled=cancelled)
    return run_checks(ctx)
