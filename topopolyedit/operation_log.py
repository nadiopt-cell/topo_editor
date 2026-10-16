# -*- coding: utf-8 -*-
"""OperationLog — журнал многослойных операций плагина (этап 4 ТЗ, п. 4–5).

Каждая операция плагина регистрируется ПОСЛЕ успешного коммита: запись
содержит список затронутых слоёв (layer_id, текст команды, index стека
отмены сразу после операции) и признак транзакционной группы.

Кнопка «Отменить правку» и Ctrl+Z:
  * обычная операция — undoStack().undo() на ВСЕХ слоях записи подряд,
    в порядке, обратном записи (ТЗ п. 4);
  * операция в транзакционной группе QGIS (оба слоя в одном GeoPackage
    с режимом транзакций) — ОДИН undo() группового стека (ТЗ п. 5),
    потому что QGIS сам зеркалирует команды на все слои группы и
    повторный undo по слоям дал бы двойную отмену.

Защита от рассинхронизации: если ПОСЛЕ операции пользователь правил
какой-то из затронутых слоёв вручную (index стека вырос) или уже
откатывал (index упал) — отмена ОТКАЗЫВАЕТСЯ с объяснением, слои
остаются согласованными (ТЗ п. 4).

Модуль не импортирует QGIS на верхнем уровне: используется в офлайн-
тестах с фейковыми стеками (у стека достаточно методов index(),
canUndo(), text(), undo()).
"""

# Все записи за сеанс; последняя операция — последняя в списке.
# Ссылок на слои НЕ храним (только id) — слой resolve() при отмене.
_operations = []

# Максимальная глубина журнала: старые записи вытесняются. Отмена
# поддерживает только ПОСЛЕДНЮЮ операцию, глубина нужна для диагностики.
MAX_OPERATIONS = 200

_SEQ = [0]


def note_operation(entries, transaction_group=False):
    """Регистрирует завершённую операцию плагина.

    :param entries: [(layer_id, command_text, index_after), ...] —
                    затронутые слои в ПОРЯДКЕ ЗАПИСИ (порядок
                    beginEditCommand); index_after — undoStack().index()
                    сразу после endEditCommand
    :param transaction_group: True — все затронутые слои в одной
                    транзакционной группе QGIS (откат одним undo)
    :return: seq операции (int) или None при пустом списке
    """
    clean = []
    for entry in entries or ():
        try:
            lid, text, idx = entry
        except Exception:
            continue
        if not lid:
            continue
        clean.append((str(lid), text or u"", int(idx)))
    if not clean:
        return None
    _SEQ[0] += 1
    _operations.append({
        "seq": _SEQ[0],
        "layers": clean,
        "transaction_group": bool(transaction_group),
    })
    del _operations[:-MAX_OPERATIONS]
    return _SEQ[0]


def last_operation():
    """Последняя незакрытая запись журнала или None."""
    return _operations[-1] if _operations else None


def drop_last_operation():
    """Удаляет последнюю запись (после успешного undo)."""
    if _operations:
        _operations.pop()


def clear():
    """Полная очистка журнала (тесты, выгрузка плагина)."""
    del _operations[:]


def _check_layer(stack, text, index_after):
    """Проверка одного слоя перед откатом.

    :return: (True, "") или (False, причина для пользователя)
    """
    try:
        idx = stack.index()
        if idx > index_after:
            return (False,
                    u"после операции слой был изменён вручную "
                    u"(нажали undo/redo или правили инструментами QGIS)")
        if idx < index_after:
            return (False,
                    u"часть операции на слое уже отменена")
        if not stack.canUndo():
            return (False,
                    u"в стеке отмены слоя нет правок для отката")
        if idx > 0:
            cur = stack.text(idx - 1) or u""
            if text and cur != text:
                return (False,
                        u"после операции слой был изменён вручную "
                        u"(верхняя команда стека — «{}», ожидалась "
                        u"«{}»)".format(cur, text))
    except Exception as exc:
        return (False, u"стек отмены недоступен: {}".format(exc))
    return (True, u"")


def can_undo_operation(op, resolve_layer):
    """Можно ли откатить операцию op целиком.

    :param op: запись last_operation()
    :param resolve_layer: callable(layer_id) -> слой | None
    :return: (True, layers в порядке записи, "") или
             (False, None, причина)
    """
    if not op:
        return (False, None, u"журнал операций пуст")
    layers = []
    for lid, text, idx in op["layers"]:
        layer = None
        try:
            layer = resolve_layer(lid)
        except Exception:
            layer = None
        if layer is None:
            return (False, None,
                    u"слой операции больше не в проекте — "
                    u"отмена не может быть выполнена частично")
        ok, reason = _check_layer(layer.undoStack(), text, idx)
        if not ok:
            return (False, None,
                    u"слой «{}»: {}".format(_layer_name(layer), reason))
        layers.append(layer)
    return (True, layers, u"")


def _layer_name(layer):
    try:
        return layer.name()
    except Exception:
        return u"?"


def undo_operation(op, resolve_layer):
    """Откатывает операцию по журналу (ТЗ этап 4, п. 4–5).

    Обычная операция: undo() на всех слоях в ПОРЯДКЕ, ОБРАТНОМ записи
    (последний затронутый слой — первым). Транзакционная группа: ОДИН
    undo() на первом слое записи (групповой стек общий — QGIS зеркалит
    команды на все слои группы, повторный undo дал бы двойную отмену).

    Перед откатом выполняется полная проверка can_undo_operation:
    при ручных правках затронутых слоёв отмена отказывается, журнальная
    запись сохраняется (слои не рассинхронизируются).

    :return: (True, [отменённые тексты команд]) или
             (False, причина)
    """
    ok, layers, reason = can_undo_operation(op, resolve_layer)
    if not ok:
        return (False, reason)
    try:
        if op.get("transaction_group"):
            stack = layers[0].undoStack()
            text = _top_text(stack)
            stack.undo()
            return (True, [text])
        undone = []
        for layer in reversed(layers):
            stack = layer.undoStack()
            undone.append(_top_text(stack))
            stack.undo()
        return (True, undone)
    except Exception as exc:
        return (False, u"сбой отката: {}".format(exc))


def _top_text(stack):
    try:
        idx = stack.index()
        if idx > 0:
            return stack.text(idx - 1) or u""
    except Exception:
        pass
    return u""


# ---------------------------------------------------------------------------
# Транзакционные группы QGIS (этап 4 ТЗ, п. 5)
# ---------------------------------------------------------------------------

def layer_group_key(layer):
    """Ключ транзакционной группы слоя или None.

    Слой принадлежит группе, если проект в режиме транзакций и QGIS
    включил его в QgsTransactionGroup (обе таблицы одного GeoPackage).
    Опирается на project.transactionGroups(); при недоступности API
    (старый QGIS, стабы) — None (слои считаются независимыми).
    """
    if layer is None:
        return None
    try:
        project = _layer_project(layer)
    except Exception:
        return None
    if project is None:
        return None
    try:
        groups = project.transactionGroups()
    except Exception:
        return None
    try:
        lid = layer.id()
    except Exception:
        return None
    try:
        items = list(groups.items())
    except Exception:
        return None
    for _key, group in items:
        try:
            members = group.layers()
        except Exception:
            continue
        try:
            for member in members:
                if member.id() == lid:
                    return _group_conn_key(group)
        except Exception:
            continue
    return None


def _layer_project(layer):
    """Проект слоя ( QgsProject.instance() — у всех слоёв он один)."""
    from qgis.core import QgsProject
    return QgsProject.instance()


def _group_conn_key(group):
    """Стабильный ключ группы: connection string или id объекта."""
    for attr in (u"connString", u"connectionString"):
        try:
            value = getattr(group, attr, None)
            if callable(value):
                value = value()
            if value:
                return str(value)
        except Exception:
            continue
    try:
        return u"__group__{}".format(id(group))
    except Exception:
        return u"__group__"


def same_transaction_group(layers):
    """True — ВСЕ слои в одной транзакционной группе (для одной
    операции это означает: один общий стек отмены, откат одним undo).

    Если хотя бы один слой вне группы или группы разные — False:
    откат идёт по стекам слоёв (п. 4).
    """
    if not layers or len(layers) < 2:
        return False
    keys = []
    for lyr in layers:
        key = layer_group_key(lyr)
        if key is None:
            return False
        keys.append(key)
    return len(set(keys)) == 1
