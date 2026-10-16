# -*- coding: utf-8 -*-
"""Вспомогательные функции: выбор редактируемого слоя, допуски, сообщения."""

from qgis.core import (
    QgsProject, QgsVectorLayer, QgsWkbTypes, QgsPointXY,
    QgsCoordinateTransform, QgsUnitTypes, Qgis,
)
from qgis.PyQt.QtWidgets import QMessageBox

from . import settings

TOLERANCE_PX = settings.DEFAULT_EDGE_TOL  # прежний допуск, px (совместимость)

LOG_TAG = u"TopoPolyEdit"


def log(message, level=None):
    """Пишет сообщение в панель «Журнал сообщений» (Log Messages Panel),
    вкладка «TopoPolyEdit».

    Сюда попадают ошибки, которые НЕ показываются в message bar, потому
    что их обработка предусмотрена кодом (тихие except в критических
    путях: преобразование CRS, difference, перетаскивание). Без этого
    такие сбои выглядели бы как «ничего не произошло» и не поддавались
    диагностике. Уровень по умолчанию — Warning.

    Безопасно вне QGIS: при любой ошибке запись просто пропускается.
    """
    try:
        from qgis.core import QgsMessageLog
        if level is None:
            level = Qgis.Warning
        QgsMessageLog.logMessage(str(message), LOG_TAG, level)
    except Exception:
        pass


def enum_member(owner, enum_name, member):
    """Член enum Qt/QGIS для PyQt5 и PyQt6 одним вызовом.

    PyQt5/QGIS 3: ``QgsRubberBand.ICON_BOX``; PyQt6/QGIS 4 требует
    ``QgsRubberBand.IconType.ICON_BOX``. Сначала пробуется «с областью»
    (работает там, где она поддержана), затем прежний плоский доступ.

    :param owner: класс-владелец (QgsRubberBand, QMessageBox, ...)
    :param enum_name: имя enum («IconType», «ButtonRole», ...)
    :param member: имя члена («ICON_BOX», «AcceptRole», ...)
    """
    scope = getattr(owner, enum_name, None)
    if scope is not None and hasattr(scope, member):
        return getattr(scope, member)
    return getattr(owner, member)


def is_polygon_layer(layer):
    """True — это корректный полигональный векторный слой."""
    return (isinstance(layer, QgsVectorLayer) and layer.isValid()
            and layer.geometryType() == QgsWkbTypes.PolygonGeometry)


def resolve_edit_layers(iface, offer_start=True, quiet=False):
    """Список редактируемых полигональных слоёв в порядке приоритета.

    Первый элемент — активный слой, если он в правке. Если активный слой
    не в правке, а редактируемые полигональные слои есть — возвращаются
    ВСЕ они: инструмент выберет тот, под курсором которого найден узел
    или ребро (иначе при нескольких слоях в правке инструмент искал
    не в том слое и ошибочно сообщал «нет узла»). Если редактируемых
    слоёв нет — предлагается включить правку активного полигонального
    слоя (кроме quiet-режима).

    :param offer_start: предлагать ли включить редактирование диалогом
    :param quiet: не показывать никаких диалогов и сообщений (для hover)
    :return: [QgsVectorLayer, ...] — может быть пустым
    """
    layer = iface.activeLayer()
    if is_polygon_layer(layer) and layer.isEditable():
        return [layer]

    edited = [l for l in QgsProject.instance().mapLayers().values()
              if is_polygon_layer(l) and l.isEditable()]
    if edited:
        return edited

    if quiet:
        return []

    if is_polygon_layer(layer) and offer_start:
        ans = QMessageBox.question(
            iface.mainWindow(),
            u"Топологическое редактирование",
            u"Слой «{}» не находится в режиме редактирования.\n"
            u"Включить редактирование?".format(layer.name()),
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
        if ans == QMessageBox.Yes:
            layer.startEditing()
            return [layer]
        return []

    iface.messageBar().pushMessage(
        u"Топологическое редактирование",
        u"Включите режим редактирования полигонального слоя "
        u"и сделайте его активным.",
        level=Qgis.Warning, duration=5)
    return []


def resolve_edit_layer(iface, offer_start=True, quiet=False):
    """Первый редактируемый полигональный слой (см. resolve_edit_layers).

    :return: QgsVectorLayer или None
    """
    layers = resolve_edit_layers(iface, offer_start=offer_start,
                                 quiet=quiet)
    return layers[0] if layers else None


# ---------------------------------------------------------------------------
# Кнопка «Отменить» (v1.6.2)
#
# Штатная отмена QGIS (Правка -> Отменить, Ctrl+Z) привязана к АКТИВНОМУ
# слою панели слоёв. Плагин же может править ЛЮБОЙ полигональный слой в
# режиме редактирования (при нескольких слоях в правке инструмент выбирает
# слой под курсором), поэтому штатная отмена часто остаётся серой, хотя
# откатывать есть что. Плагин помнит последний изменённый им слой
# (note_edited_layer вызывается ядром после каждой успешной операции) и
# по нему находит цель отката — см. find_undo_target.
# ---------------------------------------------------------------------------

_last_edited_ref = []

# Журнал ПОСЛЕДНЕЙ операции плагина по слоям (этап 3 ТЗ): layer_id ->
# (QgsRectangle, порядковый номер). Нужен для afterRollBack индекса
# топологии: сигнал отката не сообщает затронутые фичи, и инвалидация
# идёт по плиткам, пересекающим bbox последней операции (ТЗ этап 3, п. 3).
_last_op_bbox = {}
_last_op_seq = [0]


def note_operation_bbox(layer, rect):
    """Запоминает габарит последней операции плагина на слое.

    rect должен накрывать НОВЫЕ и СТАРЫЕ положения затронутых фич
    (тогда откат операции восстановит геометрии в пределах этого
    габарита). Ссылка на rect копируется; слой не хранится (только id).
    """
    try:
        lid = layer.id()
        if rect is None:
            _last_op_bbox.pop(lid, None)
            return
        _last_op_seq[0] += 1
        _last_op_bbox[lid] = (_copy_rect(rect), _last_op_seq[0])
    except Exception:
        pass


def last_operation_bbox_by_id(layer_id):
    """Габарит последней операции плагина на слое (QgsRectangle | None).

    Индекс топологии зовёт этот метод в afterRollBack (ТЗ этап 3, п. 3).
    """
    try:
        entry = _last_op_bbox.get(layer_id)
        if entry is None:
            return None
        rect = _copy_rect(entry[0])
        return rect
    except Exception:
        return None


def reset_operation_journal():
    """Полная очистка журнала операций (тесты, сброс сеанса)."""
    try:
        _last_op_bbox.clear()
        _last_op_seq[0] = 0
    except Exception:
        pass


def _copy_rect(rect):
    """Копия QgsRectangle (grow/combineExtentWith изменяют на месте)."""
    from qgis.core import QgsRectangle as _QR
    return _QR(rect.xMinimum(), rect.yMinimum(),
               rect.xMaximum(), rect.yMaximum())


def note_edited_layer(layer):
    """Запоминает последний слой, изменённый плагином (для «Отменить»).

    Вызывается ядром (TopoEditor) после успешного endEditCommand.
    Хранится одна ссылка: повторный вызов замещает предыдущее значение.
    Ссылка «сильная»: удалённый из проекта слой распознаётся позже по
    сбою обращения к C++ объекту (can_undo/find_undo_target гасят
    RuntimeError и пропускают такой слой).
    """
    try:
        del _last_edited_ref[:]
        if layer is not None:
            _last_edited_ref.append(layer)
    except Exception:
        pass


def last_edited_layer():
    """Последний слой, изменённый плагином, или None."""
    try:
        if _last_edited_ref:
            return _last_edited_ref[0]
    except Exception:
        pass
    return None


def can_undo(layer):
    """True — полигональный слой в правке и в его стеке отмены есть команды.

    Устойчив к любому мусору (удалённый слой, не-слой, сбой C++ объекта):
    в этом случае просто False — кнопка «Отменить» не должна падать.
    """
    try:
        if not is_polygon_layer(layer) or not layer.isEditable():
            return False
        return bool(layer.undoStack().canUndo())
    except Exception:
        return False


def find_undo_target(candidates, last_edited=None, active=None):
    """Выбирает слой для кнопки «Отменить» (или None — откатывать нечего).

    Порядок выбора — по свежести правок:
      1) last_edited — последний слой, изменённый ПЛАГИНОМ (откатывает
         именно последнюю операцию плагина, даже если слой не активен);
      2) active — активный слой (правки сделаны штатными инструментами
         QGIS или плагином на активном слое);
      3) первый подходящий из candidates (произвольный слой в правке).
    Подходящий = полигональный, в режиме редактирования,
    undoStack().canUndo().

    :param candidates: iterable слоёв проекта (обычно mapLayers().values())
    :param last_edited: слой из last_edited_layer() (может быть None)
    :param active: активный слой (может быть None)
    """
    for layer in (last_edited, active):
        if can_undo(layer):
            return layer
    try:
        for layer in candidates:
            if can_undo(layer):
                return layer
    except Exception:
        pass
    return None


def _grab_tolerance_map(canvas, kind):
    """Радиус захвата узла/ребра в единицах карты — из настроек плагина.

    :param kind: "vertex" — радиус строгого снэпа к узлам,
                 "edge" — радиус прилипания к рёбрам
    """
    return settings.tolerance_map_units(canvas, kind)


def layer_tolerance(canvas, layer, map_pt, kind="vertex"):
    """Переводит допуск прилипания в единицы CRS слоя.

    Радиус берётся из настроек плагина: "vertex" — строгий радиус
    узлов (по умолчанию 15 px) — им ищется узел для перетаскивания;
    "edge" — радиус рёбер (по умолчанию 10 px) — им ищется ребро.

    :param canvas: QgsMapCanvas
    :param layer: редактируемый слой
    :param map_pt: точка в CRS карты
    :param kind: "vertex" | "edge"
    :return: (pt_layer, eps_layer, ct_map_to_layer, ct_layer_to_map)
    """
    ms = canvas.mapSettings()
    dest = ms.destinationCrs()
    try:
        tol_map = _grab_tolerance_map(canvas, kind)
    except Exception:
        tol_map = TOLERANCE_PX
    ct_m2l = QgsCoordinateTransform(dest, layer.crs(), QgsProject.instance())
    ct_l2m = QgsCoordinateTransform(layer.crs(), dest, QgsProject.instance())
    pt_layer = ct_m2l.transform(map_pt)
    try:
        shifted = ct_m2l.transform(
            QgsPointXY(map_pt.x() + tol_map, map_pt.y()))
        eps = pt_layer.distance(shifted)
    except Exception:
        eps = tol_map
    if eps <= 0.0:
        eps = tol_map
    return pt_layer, eps, ct_m2l, ct_l2m
