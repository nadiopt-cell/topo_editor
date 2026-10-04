# -*- coding: utf-8 -*-
"""Настройки прилипания TopoPolyEdit (QgsSettings + значения по умолчанию).

Хранит:
  * допуск прилипания к рёбрам (edge) и к узлам (vertex, «строгий» снэп);
  * единицы допуска: "px" (пиксели экрана) или "map" (единицы карты);
  * ЧЁРНЫЙ список слоёв-исключений: идентификаторы слоёв, к которым
    прилипать НЕ надо. Все остальные слои участвуют, как и раньше
    (включенные в панели слоёв + редактируемый принудительно).

Выбран чёрный список, а не белый, чтобы новые слои проекта по умолчанию
участвовали в прилипании — поведение до появления настроек сохраняется.

Хранение: QgsSettings (раздел плагина). Движок прилипания и инструменты
читают настройки при КАЖДОМ поиске привязки, поэтому изменения из
диалога настроек действуют сразу, без перезапуска QGIS.

Все функции устойчивы к ошибкам чтения/хранения: при любом сбое
возвращаются значения по умолчанию — прилипание не может «сломаться»
из-за испорченных настроек.
"""

# Раздел и ключи QgsSettings (QSettings-путь: «раздел/подраздел/ключ»)
SNAP_SECTION = "plugins/topopolyedit"
KEY_EDGE_TOL = SNAP_SECTION + "/snap_edge_tol"
KEY_VERTEX_TOL = SNAP_SECTION + "/snap_vertex_tol"
KEY_UNITS = SNAP_SECTION + "/snap_units"
KEY_DISABLED = SNAP_SECTION + "/snap_disabled_layers"

# Единицы допуска
UNITS_PX = "px"      # пиксели экрана
UNITS_MAP = "map"    # единицы CRS карты

# Значения по умолчанию (совпадают с прежними константами движка)
DEFAULT_EDGE_TOL = 10.0    # рёбра, px
DEFAULT_VERTEX_TOL = 15.0  # узлы (строгий снэп), px

# Границы допуска (в любых единицах)
MIN_TOL = 0.1
MAX_TOL = 100000.0


def _qsettings():
    """QgsSettings текущего сеанса QGIS или None (вне QGIS/ошибка)."""
    try:
        from qgis.core import QgsSettings
        return QgsSettings()
    except Exception:
        return None


def parse_layer_ids(raw):
    """Список id слоёв из строки «id1,id2,...».

    Пробелы по краям частей и пустые части отбрасываются.
    None / пустая строка -> [].
    """
    if raw is None:
        return []
    if not isinstance(raw, str):
        try:
            raw = str(raw)
        except Exception:
            return []
    out = []
    for part in raw.split(","):
        part = part.strip()
        if part:
            out.append(part)
    return out


def _clamp_tol(value, default):
    """Число в [MIN_TOL, MAX_TOL]; мусор/NaN/бесконечность -> default."""
    try:
        value = float(value)
    except Exception:
        return default
    if value != value:  # NaN
        return default
    if value in (float("inf"), float("-inf")):
        return default
    if value < MIN_TOL:
        return MIN_TOL
    if value > MAX_TOL:
        return MAX_TOL
    return value


def _clamp_units(value):
    """Только UNITS_PX / UNITS_MAP; всё прочее -> UNITS_PX."""
    if value == UNITS_MAP:
        return UNITS_MAP
    return UNITS_PX


def load_settings():
    """Читает настройки; возвращает словарь с гарантированно годными полями:
      {"edge_tol": float, "vertex_tol": float,
       "units": "px"|"map", "disabled": [str, ...]}
    """
    cfg = {
        "edge_tol": DEFAULT_EDGE_TOL,
        "vertex_tol": DEFAULT_VERTEX_TOL,
        "units": UNITS_PX,
        "disabled": [],
    }
    qs = _qsettings()
    if qs is None:
        return cfg
    try:
        cfg["edge_tol"] = _clamp_tol(
            qs.value(KEY_EDGE_TOL, DEFAULT_EDGE_TOL, type=float),
            DEFAULT_EDGE_TOL)
    except Exception:
        pass
    try:
        cfg["vertex_tol"] = _clamp_tol(
            qs.value(KEY_VERTEX_TOL, DEFAULT_VERTEX_TOL, type=float),
            DEFAULT_VERTEX_TOL)
    except Exception:
        pass
    try:
        cfg["units"] = _clamp_units(
            qs.value(KEY_UNITS, UNITS_PX, type=str))
    except Exception:
        pass
    try:
        cfg["disabled"] = parse_layer_ids(
            qs.value(KEY_DISABLED, "", type=str))
    except Exception:
        pass
    return cfg


def save_settings(cfg):
    """Сохраняет настройки. Негодные значения замещаются по умолчанию.

    :param cfg: словарь как у load_settings() (лишние поля игнорируются)
    :return: True — сохранено; False — QgsSettings недоступен (вне QGIS)
    """
    qs = _qsettings()
    if qs is None:
        return False
    try:
        qs.setValue(KEY_EDGE_TOL, float(
            _clamp_tol(cfg.get("edge_tol", DEFAULT_EDGE_TOL),
                       DEFAULT_EDGE_TOL)))
        qs.setValue(KEY_VERTEX_TOL, float(
            _clamp_tol(cfg.get("vertex_tol", DEFAULT_VERTEX_TOL),
                       DEFAULT_VERTEX_TOL)))
        qs.setValue(KEY_UNITS, _clamp_units(cfg.get("units", UNITS_PX)))
        disabled = cfg.get("disabled", [])
        parts = []
        for lid in disabled:
            try:
                s = str(lid).strip()
            except Exception:
                continue
            if s:
                parts.append(s)
        qs.setValue(KEY_DISABLED, u",".join(parts))
        return True
    except Exception:
        return False


def tolerance_values():
    """(радиус рёбер, радиус узлов, единицы) — текущие допуски.

    Единицы: UNITS_PX (пиксели экрана) или UNITS_MAP (единицы CRS карты).
    """
    cfg = load_settings()
    return cfg["edge_tol"], cfg["vertex_tol"], cfg["units"]


def tolerance_map_units(canvas, kind):
    """Допуск в единицах карты.

    :param canvas: QgsMapCanvas (для перевода пикселей в единицы карты;
                   None допустим — тогда пиксели возвращаются как есть)
    :param kind: "vertex" (строгий снэп к узлам) или "edge" (рёбра)
    """
    edge_v, vertex_v, units = tolerance_values()
    value = vertex_v if kind == "vertex" else edge_v
    if units == UNITS_MAP:
        return value
    try:
        return canvas.mapSettings().convertToMapUnits(
            value, _render_pixels())
    except Exception:
        return value


def _render_pixels():
    """QgsUnitTypes.RenderPixels (отдельно, чтобы не тянуть импорт наверх)."""
    from qgis.core import QgsUnitTypes
    return QgsUnitTypes.RenderPixels


def disabled_layer_ids():
    """Множество id слоёв, исключённых из прилипания настройками."""
    cfg = load_settings()
    return set(cfg["disabled"])


def is_snap_disabled(layer):
    """True — слой исключён из прилипания настройками плагина."""
    if layer is None:
        return False
    try:
        return layer.id() in disabled_layer_ids()
    except Exception:
        return False


def reset_to_defaults():
    """Сбрасывает настройки к значениям по умолчанию.

    :return: True — успех; False — QgsSettings недоступен
    """
    qs = _qsettings()
    if qs is None:
        return False
    try:
        for key in (KEY_EDGE_TOL, KEY_VERTEX_TOL, KEY_UNITS, KEY_DISABLED):
            try:
                qs.remove(key)
            except Exception:
                pass
    except Exception:
        pass
    # гарантируем дефолтные значения даже если remove не сработал
    return save_settings({
        "edge_tol": DEFAULT_EDGE_TOL,
        "vertex_tol": DEFAULT_VERTEX_TOL,
        "units": UNITS_PX,
        "disabled": [],
    })
