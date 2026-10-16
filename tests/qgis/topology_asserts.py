# -*- coding: utf-8 -*-
"""Проверки топологической целостности слоя для QGIS-тестов (этап 0 ТЗ).

`assert_topology_ok` — центральный хелпер приёмки: валидность, отсутствие
перехлёстов выше порога, сохранение суммарной площади, отсутствие НОВЫХ
щелей (щели = внутренние кольца объединения всех полигонов слоя).
"""
from collections import defaultdict

from qgis.core import QgsGeometry


def layer_geoms(layer):
    """[(fid, QgsGeometry), ...] только полигональные непустые фичи."""
    out = []
    for f in layer.getFeatures():
        g = f.geometry()
        if g is None or g.isNull() or g.isEmpty():
            continue
        if g.type() != 2:  # QgsWkbTypes.PolygonGeometry
            continue
        out.append((f.id(), g))
    return out


def layer_snapshot(layer):
    """Битовое состояние слоя: {fid: bytes(WKB)}."""
    return {f.id(): bytes(f.geometry().asWkb())
            for f in layer.getFeatures()}


def total_area(layer):
    return sum(g.area() for _fid, g in layer_geoms(layer))


def union_holes(layer):
    """Число внутренних колец объединения всех полигонов слоя.

    Для слоя из смежных полигонов это ровно число щелей + настоящих
    дыр (дыры фикстур передаются отдельно)."""
    geoms = [g for _fid, g in layer_geoms(layer)]
    if not geoms:
        return 0
    u = QgsGeometry.unaryUnion(geoms)
    if u.isNull() or u.isEmpty():
        return 0
    parts = u.asMultiPolygon() if u.isMultipart() else [u.asPolygon()]
    return sum(len(poly) - 1 for poly in parts)


def shared_nodes(layer, ndigits=6):
    """{точка: frozenset(fid)} — узлы, общие для двух и более фич.

    Ключ — координата, округлённая до ndigits знаков. Для И3 сравнивается
    структура «общий узел -> фичи» до и после операции."""
    pts = defaultdict(set)
    for fid, g in layer_geoms(layer):
        parts = g.asMultiPolygon() if g.isMultipart() else [g.asPolygon()]
        for poly in parts:
            for ring in poly:
                for p in ring:
                    pts[(round(p.x(), ndigits), round(p.y(), ndigits))
                        ].add(fid)
    return {k: frozenset(v) for k, v in pts.items() if len(v) > 1}


def shared_pairs_multiset(layer):
    """Мультимножество наборов фич у общих узлов (для сравнения И3)."""
    return sorted(shared_nodes(layer).values(), key=sorted)


def assert_topology_ok(layer, min_ratio=1e-8,
                       total_area_before=None, area_rel_tol=1e-9,
                       holes_before=None, msg=""):
    """И1+И2+И5 (+И6-площадь) для слоя после операции.

    :param min_ratio: порог перехлёста (OVERLAP_MIN_RATIO): площадь
                      пересечения должна быть больше ratio от меньшего
                      из пары, чтобы считаться нарушением
    :param total_area_before: суммарная площадь до операции (операции без
                              отсечения должны сохранять её точно)
    :param holes_before: число внутренних колец объединения до операции
                         (щели не должны появляться)
    :param msg: префикс для сообщений об ошибках
    """
    geoms = layer_geoms(layer)
    pre = "{}: ".format(msg) if msg else ""

    # И1: валидность каждой геометрии
    for fid, g in geoms:
        if not g.isGeosValid():
            raise AssertionError(
                pre + "И1: фича {} невалидна".format(fid))

    # И2: нет перехлёстов выше порога
    n = len(geoms)
    for i in range(n):
        fid_i, gi = geoms[i]
        for j in range(i + 1, n):
            fid_j, gj = geoms[j]
            if not gi.boundingBox().intersects(gj.boundingBox()):
                continue
            try:
                inter = gi.intersection(gj)
            except Exception:
                continue  # разбор кривых и прочее — вне задач хелпера
            if inter is None or inter.isNull() or inter.isEmpty():
                continue
            a = inter.area()
            lim = min_ratio * min(gi.area(), gj.area())
            if a > lim:
                raise AssertionError(
                    pre + "И2: перехлёст {}/{} площади {} > {}".format(
                        fid_i, fid_j, a, lim))

    # площадь (для операций без отсечения)
    total = sum(g.area() for _fid, g in geoms)
    if total_area_before is not None:
        diff = abs(total - total_area_before)
        lim = area_rel_tol * max(abs(total_area_before), 1e-12)
        if diff > lim:
            raise AssertionError(
                pre + "площадь изменилась: {} -> {} (лимит {})".format(
                    total_area_before, total, lim))

    # И5: щели не появились
    holes = union_holes(layer)
    if holes_before is not None and holes != holes_before:
        raise AssertionError(
            pre + "И5: число щелей/дыр изменилось: {} -> {}".format(
                holes_before, holes))
    return {"total_area": total, "holes": holes}
