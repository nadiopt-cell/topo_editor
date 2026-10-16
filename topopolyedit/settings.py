# -*- coding: utf-8 -*-
"""Настройки прилипания TopoPolyEdit (QgsSettings + значения по умолчанию).

Хранит:
  * режим обработки перехлёста с объектами ТОГО ЖЕ слоя (спросить /
    всегда обрезать / не обрезать) и флаг подсветки вершин слоёв прилипания;
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
KEY_OVERLAP_MODE = SNAP_SECTION + "/same_layer_overlap"
KEY_SHOW_HINTS = SNAP_SECTION + "/show_snap_hints"
KEY_AUDIT_ON = SNAP_SECTION + "/node_audit_on"
# пороги валидатора (ТЗ, «Пороги по умолчанию», этап 1)
KEY_SPIKE_ANGLE = SNAP_SECTION + "/spike_min_angle_deg"
KEY_SPIKE_SEVERITY = SNAP_SECTION + "/spike_severity"
KEY_SEAM_RATIO = SNAP_SECTION + "/seam_min_ratio"
KEY_SHARED_SEVERITY = SNAP_SECTION + "/shared_boundary_severity"
KEY_OFFER_MAKEVALID = SNAP_SECTION + "/offer_makevalid"
KEY_EDGE_SLIDE = SNAP_SECTION + "/edge_slide_on_neighbors"

# топологический допуск — константа geo_utils (без импорта модуля,
# чтобы избежать циклической зависимости: geo_utils не знает о settings)
TOPO_MATCH_EPS_CONST = 1e-6
# индекс топологии (этап 3 ТЗ)
KEY_INDEX_ON = SNAP_SECTION + "/topology_index_on"
KEY_MAX_NEIGHBORS = SNAP_SECTION + "/max_overlap_neighbors"
KEY_TILE_FACTOR = SNAP_SECTION + "/index_tile_factor"
# правка нескольких слоёв одной операцией (этап 4 ТЗ)
KEY_EDIT_SCOPE = SNAP_SECTION + "/edit_scope"
KEY_EDIT_DISABLED = SNAP_SECTION + "/edit_disabled_layers"
KEY_CLIP_MODES = SNAP_SECTION + "/clip_layer_modes"

# Единицы допуска
UNITS_PX = "px"      # пиксели экрана
UNITS_MAP = "map"    # единицы CRS карты

# Область правки (этап 4 ТЗ, п. 1): какие слои участвуют в операции
EDIT_SCOPE_ALL = "all_editing"      # все слои в режиме правки (по умолчанию)
EDIT_SCOPE_ACTIVE = "active_only"   # только активный слой (как до этапа 4)
EDIT_SCOPES = (EDIT_SCOPE_ALL, EDIT_SCOPE_ACTIVE)

# Per-layer режимы отсечения перехлёстов (этап 4 ТЗ, п. 7)
CLIP_CLIP = "clip"      # автоотсечение, как раньше (по умолчанию)
CLIP_ASK = "ask"        # спрашивать пользователя при перехлёсте
CLIP_IGNORE = "ignore"  # не отсекать и не спрашивать
CLIP_MODES = (CLIP_CLIP, CLIP_ASK, CLIP_IGNORE)

# Протокол решения clip_decider (ответ на вопрос про перехлёст со слоем
# в режиме «спрашивать»)
CLIP_CHOICE_CLIP = "clip"    # обрезать перемещаемые по слою
CLIP_CHOICE_KEEP = "keep"    # оставить перехлёст (не отсекать)
CLIP_CHOICE_CANCEL = "cancel"  # отменить операцию целиком

# Длина дуги 1° в метрах — перевод топологического допуска для слоёв
# в градусных СК (этап 4 ТЗ, п. 2: допуск пропорционален погрешности
# преобразования, которая масштабируется единицей СК)
DEGREE_METERS = 111320.0

# Что делать, если перемещаемые полигоны перекрывают другие объекты ТОГО ЖЕ
# редактируемого слоя (перекрытия с эталонами других слоёв отсекаются как
# раньше, автоматически):
OVERLAP_ASK = "ask"    # спросить пользователя при отпускании кнопки мыши
OVERLAP_CLIP = "clip"  # всегда обрезать перемещаемые полигоны по соседям
OVERLAP_KEEP = "keep"  # ничего не обрезать, перехлёст оставить
OVERLAP_MODES = (OVERLAP_ASK, OVERLAP_CLIP, OVERLAP_KEEP)

# Значения по умолчанию (совпадают с прежними константами движка)
DEFAULT_EDGE_TOL = 10.0    # рёбра, px
DEFAULT_VERTEX_TOL = 15.0  # узлы (строгий снэп), px
DEFAULT_OVERLAP_MODE = OVERLAP_ASK
DEFAULT_SHOW_HINTS = True  # подсвечивать вершины слоёв прилипания
DEFAULT_AUDIT_ON = False   # режим «Аудит узлов» по умолчанию выключен

# Пороги валидатора (ТЗ этап 1)
DEFAULT_SPIKE_ANGLE = 0.5            # угол «шипа», градусы
DEFAULT_SEAM_RATIO = 1e-8            # щель-«шов» стыковки игнорируется
SEV_ERROR = "error"                  # блокирует запись
SEV_WARNING = "warning"              # видно в сообщении, не блокирует
SEVERITIES = (SEV_ERROR, SEV_WARNING)
DEFAULT_SPIKE_SEVERITY = SEV_WARNING   # шипы — настраиваемо (ТЗ этап 1)
DEFAULT_SHARED_SEVERITY = SEV_WARNING  # потеря общих узлов
DEFAULT_OFFER_MAKEVALID = False     # авторемонт makeValid — выкл. (ТЗ 1.6)
DEFAULT_EDGE_SLIDE = True   # скользить по соседнему ребру (ТЗ этап 2)

# Индекс топологии (ТЗ этап 3)
DEFAULT_INDEX_ON = True         # индекс включён; выкл. = работа как раньше
DEFAULT_MAX_NEIGHBORS = 5000    # лимит соседей того же слоя (был 500)
DEFAULT_TILE_FACTOR = 2.0       # размер плитки = видимый экстент × фактор
MIN_MAX_NEIGHBORS = 100
MAX_MAX_NEIGHBORS = 1000000
MIN_TILE_FACTOR = 0.5
MAX_TILE_FACTOR = 20.0

# Правка нескольких слоёв (ТЗ этап 4)
DEFAULT_EDIT_SCOPE = EDIT_SCOPE_ALL   # по умолчанию двигаются ВСЕ слои в правке

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


def _clamp_overlap_mode(value):
    """Только OVERLAP_*; всё прочее -> OVERLAP_ASK."""
    if value in OVERLAP_MODES:
        return value
    return DEFAULT_OVERLAP_MODE


def _as_bool(value, default):
    """Булево из значения QgsSettings (бывает bool, 'true'/'false', 0/1)."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("true", "1", "yes", "on"):
            return True
        if v in ("false", "0", "no", "off"):
            return False
        return default
    try:
        return bool(int(value))
    except Exception:
        return default


def load_settings():
    """Читает настройки; возвращает словарь с гарантированно годными полями:
      {"edge_tol": float, "vertex_tol": float,
       "units": "px"|"map", "disabled": [str, ...],
       "overlap_mode": "ask"|"clip"|"keep", "show_hints": bool,
       "edge_slide": bool}
    """
    cfg = {
        "edge_tol": DEFAULT_EDGE_TOL,
        "vertex_tol": DEFAULT_VERTEX_TOL,
        "units": UNITS_PX,
        "disabled": [],
        "overlap_mode": DEFAULT_OVERLAP_MODE,
        "show_hints": DEFAULT_SHOW_HINTS,
        "edge_slide": DEFAULT_EDGE_SLIDE,
        "index_on": DEFAULT_INDEX_ON,
        "max_neighbors": DEFAULT_MAX_NEIGHBORS,
        "tile_factor": DEFAULT_TILE_FACTOR,
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
    try:
        cfg["overlap_mode"] = _clamp_overlap_mode(
            qs.value(KEY_OVERLAP_MODE, DEFAULT_OVERLAP_MODE, type=str))
    except Exception:
        pass
    try:
        cfg["show_hints"] = _as_bool(
            qs.value(KEY_SHOW_HINTS, DEFAULT_SHOW_HINTS), DEFAULT_SHOW_HINTS)
    except Exception:
        pass
    try:
        cfg["edge_slide"] = _as_bool(
            qs.value(KEY_EDGE_SLIDE, DEFAULT_EDGE_SLIDE), DEFAULT_EDGE_SLIDE)
    except Exception:
        pass
    try:
        cfg["index_on"] = _as_bool(
            qs.value(KEY_INDEX_ON, DEFAULT_INDEX_ON), DEFAULT_INDEX_ON)
    except Exception:
        pass
    try:
        cfg["max_neighbors"] = int(qs.value(
            KEY_MAX_NEIGHBORS, DEFAULT_MAX_NEIGHBORS, type=int))
        cfg["max_neighbors"] = min(
            max(cfg["max_neighbors"], MIN_MAX_NEIGHBORS),
            MAX_MAX_NEIGHBORS)
    except Exception:
        pass
    try:
        cfg["tile_factor"] = float(qs.value(
            KEY_TILE_FACTOR, DEFAULT_TILE_FACTOR, type=float))
        if cfg["tile_factor"] != cfg["tile_factor"] or \
                cfg["tile_factor"] in (float("inf"), float("-inf")):
            cfg["tile_factor"] = DEFAULT_TILE_FACTOR
        cfg["tile_factor"] = min(max(cfg["tile_factor"], MIN_TILE_FACTOR),
                                 MAX_TILE_FACTOR)
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
        qs.setValue(KEY_OVERLAP_MODE, _clamp_overlap_mode(
            cfg.get("overlap_mode", DEFAULT_OVERLAP_MODE)))
        qs.setValue(KEY_SHOW_HINTS, _as_bool(
            cfg.get("show_hints", DEFAULT_SHOW_HINTS), DEFAULT_SHOW_HINTS))
        qs.setValue(KEY_EDGE_SLIDE, _as_bool(
            cfg.get("edge_slide", DEFAULT_EDGE_SLIDE), DEFAULT_EDGE_SLIDE))
        qs.setValue(KEY_INDEX_ON, _as_bool(
            cfg.get("index_on", DEFAULT_INDEX_ON), DEFAULT_INDEX_ON))
        try:
            qs.setValue(KEY_MAX_NEIGHBORS, int(
                min(max(int(cfg.get("max_neighbors",
                                    DEFAULT_MAX_NEIGHBORS)),
                        MIN_MAX_NEIGHBORS), MAX_MAX_NEIGHBORS)))
        except Exception:
            qs.setValue(KEY_MAX_NEIGHBORS, DEFAULT_MAX_NEIGHBORS)
        try:
            tf = float(cfg.get("tile_factor", DEFAULT_TILE_FACTOR))
            if tf != tf or tf in (float("inf"), float("-inf")):
                tf = DEFAULT_TILE_FACTOR
            qs.setValue(KEY_TILE_FACTOR,
                        min(max(tf, MIN_TILE_FACTOR), MAX_TILE_FACTOR))
        except Exception:
            qs.setValue(KEY_TILE_FACTOR, DEFAULT_TILE_FACTOR)
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


def overlap_mode():
    """Режим обработки перехлёста с объектами редактируемого слоя.

    Читает ТОЛЬКО свой ключ (вызывается при перетаскивании часто).
    :return: OVERLAP_ASK | OVERLAP_CLIP | OVERLAP_KEEP
    """
    qs = _qsettings()
    if qs is None:
        return DEFAULT_OVERLAP_MODE
    try:
        return _clamp_overlap_mode(
            qs.value(KEY_OVERLAP_MODE, DEFAULT_OVERLAP_MODE, type=str))
    except Exception:
        return DEFAULT_OVERLAP_MODE


def show_snap_hints():
    """True — подсвечивать вершины слоёв прилипания (читает только свой ключ)."""
    qs = _qsettings()
    if qs is None:
        return DEFAULT_SHOW_HINTS
    try:
        return _as_bool(qs.value(KEY_SHOW_HINTS, DEFAULT_SHOW_HINTS),
                        DEFAULT_SHOW_HINTS)
    except Exception:
        return DEFAULT_SHOW_HINTS


def audit_enabled():
    """True — режим «Аудит узлов» был включён в прошлом сеансе.

    Читает ТОЛЬКО свой ключ: вызывается один раз при загрузке плагина.
    """
    qs = _qsettings()
    if qs is None:
        return DEFAULT_AUDIT_ON
    try:
        return _as_bool(qs.value(KEY_AUDIT_ON, DEFAULT_AUDIT_ON),
                        DEFAULT_AUDIT_ON)
    except Exception:
        return DEFAULT_AUDIT_ON


def set_audit_enabled(on):
    """Сохраняет состояние «Аудита узлов» (восстановится при старте QGIS).

    :return: True — сохранено; False — QgsSettings недоступен (вне QGIS)
    """
    qs = _qsettings()
    if qs is None:
        return False
    try:
        qs.setValue(KEY_AUDIT_ON, bool(on))
        return True
    except Exception:
        return False


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


# ----------------------------------------------------------------------
# Пороги валидатора (этап 1 ТЗ): читаются при каждой операции/превью,
# поэтому — как overlap_mode — только свой ключ.
# ----------------------------------------------------------------------

def _clamp_severity(value, default):
    """Только SEV_ERROR / SEV_WARNING; всё прочее -> default."""
    if value in SEVERITIES:
        return value
    return default


def spike_min_angle_deg():
    """Минимальный угол «шипа», градусы (0.5 по умолчанию)."""
    qs = _qsettings()
    if qs is None:
        return DEFAULT_SPIKE_ANGLE
    try:
        value = float(qs.value(KEY_SPIKE_ANGLE, DEFAULT_SPIKE_ANGLE,
                               type=float))
        if value != value or value in (float("inf"), float("-inf")):
            return DEFAULT_SPIKE_ANGLE
        return min(max(value, 0.01), 89.0)
    except Exception:
        return DEFAULT_SPIKE_ANGLE


def spike_severity():
    """Серьёзность шипов: error (блокирует) или warning (по умолчанию)."""
    qs = _qsettings()
    if qs is None:
        return DEFAULT_SPIKE_SEVERITY
    try:
        return _clamp_severity(
            qs.value(KEY_SPIKE_SEVERITY, DEFAULT_SPIKE_SEVERITY, type=str),
            DEFAULT_SPIKE_SEVERITY)
    except Exception:
        return DEFAULT_SPIKE_SEVERITY


def seam_min_ratio():
    """Порог щели-«шва»: доля площади меньшего смежного полигона."""
    qs = _qsettings()
    if qs is None:
        return DEFAULT_SEAM_RATIO
    try:
        value = float(qs.value(KEY_SEAM_RATIO, DEFAULT_SEAM_RATIO,
                               type=float))
        if value != value or value in (float("inf"), float("-inf")):
            return DEFAULT_SEAM_RATIO
        return min(max(value, 0.0), 1.0)
    except Exception:
        return DEFAULT_SEAM_RATIO


def shared_boundary_severity():
    """Серьёзность потери общих узлов (по умолчанию warning)."""
    qs = _qsettings()
    if qs is None:
        return DEFAULT_SHARED_SEVERITY
    try:
        return _clamp_severity(
            qs.value(KEY_SHARED_SEVERITY, DEFAULT_SHARED_SEVERITY,
                     type=str), DEFAULT_SHARED_SEVERITY)
    except Exception:
        return DEFAULT_SHARED_SEVERITY


def offer_makevalid():
    """True — при самопересечении предлагать ремонт makeValid (диалог).

    По умолчанию выключено: makeValid молча меняет форму полигона (ТЗ
    этап 1: makeValid из конвейера убран)."""
    qs = _qsettings()
    if qs is None:
        return DEFAULT_OFFER_MAKEVALID
    try:
        return _as_bool(qs.value(KEY_OFFER_MAKEVALID,
                                 DEFAULT_OFFER_MAKEVALID),
                        DEFAULT_OFFER_MAKEVALID)
    except Exception:
        return DEFAULT_OFFER_MAKEVALID


def edge_slide_on_neighbors():
    """True — «скользить по соседнему ребру» (этап 2 ТЗ, п. 6).

    Конец перемещаемой цепочки, лежащий на чужом ребре, двигается
    ВДОЛЬ этого ребра (перенос проецируется на его направление)
    вместо параллельного переноса. По умолчанию включено."""
    qs = _qsettings()
    if qs is None:
        return DEFAULT_EDGE_SLIDE
    try:
        return _as_bool(qs.value(KEY_EDGE_SLIDE, DEFAULT_EDGE_SLIDE),
                        DEFAULT_EDGE_SLIDE)
    except Exception:
        return DEFAULT_EDGE_SLIDE


# ----------------------------------------------------------------------
# Индекс топологии (этап 3 ТЗ): читаются при каждой операции — только
# свои ключи, как overlap_mode.
# ----------------------------------------------------------------------

def index_enabled():
    """True — операции используют индекс топологии (этап 3 ТЗ).

    При выключенном индексе все запросы идут к слою напрямую (как до
    этапа 3) — откат при странном поведении индекса."""
    qs = _qsettings()
    if qs is None:
        return DEFAULT_INDEX_ON
    try:
        return _as_bool(qs.value(KEY_INDEX_ON, DEFAULT_INDEX_ON),
                        DEFAULT_INDEX_ON)
    except Exception:
        return DEFAULT_INDEX_ON


def max_overlap_neighbors():
    """Лимит соседей того же слоя для проверки перехлёста (ТЗ этап 3,
    п. 7: лимит 500 заменён на MAX_OVERLAP_NEIGHBORS, по умолчанию
    5000, с явным сообщением при превышении)."""
    qs = _qsettings()
    if qs is None:
        return DEFAULT_MAX_NEIGHBORS
    try:
        value = int(qs.value(KEY_MAX_NEIGHBORS, DEFAULT_MAX_NEIGHBORS,
                             type=int))
    except Exception:
        return DEFAULT_MAX_NEIGHBORS
    if value != value:  # NaN невозможен для int, страховка от мусора
        return DEFAULT_MAX_NEIGHBORS
    return min(max(value, MIN_MAX_NEIGHBORS), MAX_MAX_NEIGHBORS)


def tile_factor():
    """Множитель видимого экстента для размера плитки индекса.

    ТЗ этап 3, п. 2: размер плитки настраиваемый, по умолчанию видимый
    экстент × 2. Изменение применяется к НОВЫМ индексам; у построенного
    индекса сетка плиток сохраняется до его сброса."""
    qs = _qsettings()
    if qs is None:
        return DEFAULT_TILE_FACTOR
    try:
        value = float(qs.value(KEY_TILE_FACTOR, DEFAULT_TILE_FACTOR,
                               type=float))
    except Exception:
        return DEFAULT_TILE_FACTOR
    if value != value or value in (float("inf"), float("-inf")):
        return DEFAULT_TILE_FACTOR
    return min(max(value, MIN_TILE_FACTOR), MAX_TILE_FACTOR)


def clip_mode_for(layer_id):
    """Per-layer режим отсечения перехлёстов (этап 4 ТЗ, п. 7).

    :param layer_id: id слоя (str)
    :return: CLIP_CLIP (автоотсечение, по умолчанию) | CLIP_ASK | CLIP_IGNORE
    """
    raw = _clip_modes_raw()
    return raw.get(str(layer_id or u""), CLIP_CLIP)


def _clip_modes_raw():
    """{layer_id: режим} из настроек (без валидации значений)."""
    qs = _qsettings()
    if qs is None:
        return {}
    try:
        raw = qs.value(KEY_CLIP_MODES, u"", type=str)
    except Exception:
        return {}
    out = {}
    for part in parse_layer_ids(raw):
        # формат «id:режим» — id сам может содержать «:», берём ПОСЛЕДНЕЕ
        sep = part.rfind(u":")
        if sep <= 0:
            continue
        lid = part[:sep]
        mode = part[sep + 1:]
        if lid and mode in CLIP_MODES:
            out[lid] = mode
    return out


def set_clip_mode(layer_id, mode):
    """Сохраняет per-layer режим отсечения (mode в CLIP_MODES или None —
    вернуть к умолчанию «отсекать")."""
    qs = _qsettings()
    if qs is None:
        return False
    raw = _clip_modes_raw()
    lid = str(layer_id or u"")
    if not lid:
        return False
    if mode in CLIP_MODES and mode != CLIP_CLIP:
        raw[lid] = mode
    else:
        raw.pop(lid, None)  # умолчание не храним — настройка компактнее
    parts = [u"{}:{}".format(k, v) for k, v in raw.items()]
    try:
        qs.setValue(KEY_CLIP_MODES, u",".join(parts))
        return True
    except Exception:
        return False


def edit_scope():
    """Область правки (этап 4 ТЗ, п. 1): EDIT_SCOPE_ALL (по умолчанию —
    все слои в правке двигаются одной операцией) или EDIT_SCOPE_ACTIVE
    (только активный слой — поведение до этапа 4)."""
    qs = _qsettings()
    if qs is None:
        return DEFAULT_EDIT_SCOPE
    try:
        value = qs.value(KEY_EDIT_SCOPE, DEFAULT_EDIT_SCOPE, type=str)
    except Exception:
        return DEFAULT_EDIT_SCOPE
    return value if value in EDIT_SCOPES else DEFAULT_EDIT_SCOPE


def set_edit_scope(scope):
    """Сохраняет область правки (EDIT_SCOPE_*)."""
    qs = _qsettings()
    if qs is None:
        return False
    try:
        qs.setValue(KEY_EDIT_SCOPE,
                    scope if scope in EDIT_SCOPES else DEFAULT_EDIT_SCOPE)
        return True
    except Exception:
        return False


def edit_disabled_ids():
    """Множество id слоёв, исключённых из многослойной правки
    (чёрный список этапа 4, п. 1(б): слой в правке, но правится
    только вручную/эталонами — операциями плагина не двигается)."""
    qs = _qsettings()
    if qs is None:
        return set()
    try:
        return set(parse_layer_ids(
            qs.value(KEY_EDIT_DISABLED, u"", type=str)))
    except Exception:
        return set()


def set_edit_disabled(ids):
    """Сохраняет чёрный список правки (iterable id слоёв)."""
    qs = _qsettings()
    if qs is None:
        return False
    parts = []
    for lid in ids or ():
        try:
            s = str(lid).strip()
        except Exception:
            continue
        if s:
            parts.append(s)
    try:
        qs.setValue(KEY_EDIT_DISABLED, u",".join(parts))
        return True
    except Exception:
        return False


def is_edit_disabled(layer):
    """True — слой исключён из многослойной правки настройками."""
    if layer is None:
        return False
    try:
        return layer.id() in edit_disabled_ids()
    except Exception:
        return False


def edit_match_eps(layer):
    """Топологический допуск слоя с поправкой на единицы его СК
    (этап 4 ТЗ, п. 2).

    Совпадение узлов считается после преобразования в СК карты; запись
    идёт обратным преобразованием от единой точки карты — накопленный
    дрейф пропорционален единице СК слоя. Для метрических СК —
    TOPO_MATCH_EPS; для градусных — TOPO_MATCH_EPS * DEGREE_METERS
    (примерно 0,11 м на земле — как 1e-6 м для метрических)."""
    try:
        from qgis.core import QgsUnitTypes
        units = layer.crs().mapUnits()
        if units == QgsUnitTypes.DistanceDegrees:
            return TOPO_MATCH_EPS_CONST * DEGREE_METERS
    except Exception:
        pass
    return TOPO_MATCH_EPS_CONST


def validator_opts():
    """Готовый словарь opts для core.validator.validate_operation()."""
    return {
        "spike_min_angle_deg": spike_min_angle_deg(),
        "spike_severity": spike_severity(),
        "seam_min_ratio": seam_min_ratio(),
        "shared_severity": shared_boundary_severity(),
    }


def reset_to_defaults():
    """Сбрасывает настройки к значениям по умолчанию.

    :return: True — успех; False — QgsSettings недоступен
    """
    qs = _qsettings()
    if qs is None:
        return False
    try:
        for key in (KEY_EDGE_TOL, KEY_VERTEX_TOL, KEY_UNITS, KEY_DISABLED,
                    KEY_OVERLAP_MODE, KEY_SHOW_HINTS, KEY_SPIKE_ANGLE,
                    KEY_SPIKE_SEVERITY, KEY_SEAM_RATIO,
                    KEY_SHARED_SEVERITY, KEY_OFFER_MAKEVALID,
                    KEY_EDGE_SLIDE, KEY_INDEX_ON, KEY_MAX_NEIGHBORS,
                    KEY_TILE_FACTOR):
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
        "overlap_mode": DEFAULT_OVERLAP_MODE,
        "show_hints": DEFAULT_SHOW_HINTS,
    })
