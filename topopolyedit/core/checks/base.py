# -*- coding: utf-8 -*-
"""База проверок аудита слоя (этап 5 ТЗ «полностью топологический»).

Finding — одна найденная ошибка: код, слой, fid, точка, площадь, текст,
флаг «исправимо автоматически» и payload — данные, достаточные для
исправления без повторного поиска (core/checks/fixes.py).

Проверки (по одной на класс в своём модуле) получают контекст ctx:

  geoms      — [(fid, QgsGeometry)] полигональные непустые фичи слоя;
  polys      — {fid: norm_polys} нормализованные структуры (geo_utils);
  index      — QgsSpatialIndex по габаритам фич или None (офлайн-тесты:
               тогда проверки перебирают фичи перебором);
  opts       — пороги: gap_max_area, near_node_tol, sliver_ratio,
               seam_min_ratio, spike_min_angle_deg, match_eps;
  progress   — callable(frac, message), frac 0..1 или None;
  cancelled  — callable() -> bool или None.

Модуль зависит только от qgis.core — без qgis.gui.
"""

from ...geo_utils import norm_polys, TOPO_MATCH_EPS

# --- коды ошибок (ТЗ этап 5, таблица проверок) ---------------------------

CODE_INVALID = u"INVALID"        # невалидная геометрия
CODE_GAP = u"GAP"                # щель между полигонами
CODE_OVERLAP = u"OVERLAP"        # перехлёст двух полигонов
CODE_DUP_NODE = u"DUP_NODE"      # две подряд идущие вершины в допуске
CODE_NEAR_NODE = u"NEAR_NODE"    # узлы разных полигонов почти совпали
CODE_T_JUNCTION = u"T_JUNCTION"  # узел на чужом ребре без вершины
CODE_SPIKE = u"SPIKE"            # «шип»: угол меньше порога
CODE_SLIVER = u"SLIVER"          # сливер: площадь / периметр² ниже порога

# русские названия кодов (таблица результатов, фильтр)
CHECK_TITLES = {
    CODE_INVALID: u"Невалидная геометрия",
    CODE_GAP: u"Щель между полигонами",
    CODE_OVERLAP: u"Перехлёст полигонов",
    CODE_DUP_NODE: u"Дубль вершины",
    CODE_NEAR_NODE: u"Почти совпавшие узлы",
    CODE_T_JUNCTION: u"T-примыкание без узла",
    CODE_SPIKE: u"Шип",
    CODE_SLIVER: u"Сливер",
}

# коды кнопки «Исправить все (безопасные)» (ТЗ этап 5, п. 3)
SAFE_FIX_CODES = (CODE_DUP_NODE, CODE_T_JUNCTION, CODE_NEAR_NODE, CODE_GAP)

# порядок применения исправлений внутри одной пачки: чистка дублей ->
# слив почти совпавших узлов -> вставка вершин на чужие рёбра -> клей щелей
FIX_ORDER = (CODE_DUP_NODE, CODE_NEAR_NODE, CODE_T_JUNCTION, CODE_GAP)

# ключи opts (пороги по умолчанию — ТЗ, «Пороги по умолчанию»)
OPT_GAP_MAX_AREA = "gap_max_area"        # 0.01 (ед. СК слоя в квадрате)
OPT_NEAR_TOL = "near_node_tol"           # 0.01
OPT_SLIVER_RATIO = "sliver_ratio"        # 1e-4
OPT_SEAM_RATIO = "seam_min_ratio"        # 1e-8 (общий с валидатором)
OPT_SPIKE_ANGLE = "spike_min_angle_deg"  # 0.5 (общий с валидатором)
OPT_MATCH_EPS = "match_eps"              # TOPO_MATCH_EPS слоя

DEFAULT_OPTS = {
    OPT_GAP_MAX_AREA: 0.01,
    OPT_NEAR_TOL: 0.01,
    OPT_SLIVER_RATIO: 1e-4,
    OPT_SEAM_RATIO: 1e-8,
    OPT_SPIKE_ANGLE: 0.5,
    OPT_MATCH_EPS: TOPO_MATCH_EPS,
    "overlap_min_ratio": 1e-8,   # OVERLAP_MIN_RATIO (общий с валидатором)
}


class Finding(object):
    """Одна ошибка аудита слоя.

    point   — QgsPointXY в CRS слоя (переход на место, оверлей);
    area    — площадь/числительная характеристика или None (щель —
              площадь щели, перехлёст — площадь перехлёста, сливер —
              площадь фичи, остальные — None);
    fixable — доступно кнопке «Исправить» (и «Исправить все» для
              SAFE_FIX_CODES);
    payload — данные исправления (см. core/checks/fixes.py).
    """

    __slots__ = ("code", "layer_id", "layer_name", "fid", "point", "area",
                 "message", "fixable", "payload")

    def __init__(self, code, fid=None, point=None, area=None,
                 message=u"", fixable=False, payload=None,
                 layer_id=u"", layer_name=u""):
        self.code = code
        self.layer_id = layer_id
        self.layer_name = layer_name
        self.fid = fid
        self.point = point
        self.area = area
        self.message = message
        self.fixable = bool(fixable)
        self.payload = payload or {}

    def key(self):
        """Ключ строки таблицы (стабильный в пределах одного прогона)."""
        pt = self.point
        ptxt = u"{:.6f},{:.6f}".format(pt.x(), pt.y()) if pt else u"-"
        return (self.code, self.layer_id, self.fid, ptxt)


def _opts(ctx, key):
    """Порог из ctx с запасным значением по умолчанию."""
    opts = ctx.get("opts") or {}
    try:
        val = opts.get(key)
    except Exception:
        val = None
    if val is None:
        val = DEFAULT_OPTS.get(key)
    return val


def _cancelled(ctx):
    fn = ctx.get("cancelled")
    if fn is None:
        return False
    try:
        return bool(fn())
    except Exception:
        return False


def _progress(ctx, frac, message):
    fn = ctx.get("progress")
    if fn is None:
        return
    try:
        fn(frac, message)
    except Exception:
        pass


def _geoms(ctx):
    return ctx.get("geoms") or []


def _polys(ctx, fid, g=None):
    """Нормализованная структура фичи (кэш ctx["polys"]).

    norm_polys() возвращает (polys, was_multi); здесь отдаётся только
    список частей — проверки работают с частями/кольцами."""
    cache = ctx.get("polys")
    if cache is not None and fid in cache:
        return cache[fid]
    if g is None:
        for f, gg in _geoms(ctx):
            if f == fid:
                g = gg
                break
    if g is None:
        return []
    try:
        res = norm_polys(g)
    except Exception:
        res = None
    ps = []
    if res is not None:
        try:
            ps = res[0]
        except Exception:
            ps = []
    if cache is not None:
        cache[fid] = ps
    return ps


def candidate_fids(ctx, rect, exclude_fid=None):
    """fid фич с габаритами, пересекающими rect.

    Через пространственный индекс, если он есть; иначе — перебор
    (офлайн-тесты на маленьких наборах)."""
    index = ctx.get("index")
    out = []
    if index is not None:
        try:
            out = list(index.intersects(rect))
        except Exception:
            out = []
    else:
        for f, g in _geoms(ctx):
            try:
                if g.boundingBox().intersects(rect):
                    out.append(f)
            except Exception:
                continue
    if exclude_fid is None:
        return out
    return [f for f in out if f != exclude_fid]


def ring_length(ring):
    """Длина замкнутого/незамкнутого кольца по сегментам."""
    total = 0.0
    for i in range(len(ring) - 1):
        a = ring[i]
        b = ring[i + 1]
        total += ((b.x() - a.x()) ** 2 + (b.y() - a.y()) ** 2) ** 0.5
    return total


def polys_perimeter(polys):
    """Периметр нормализованной структуры: длина всех колец (оболочки
    и дыры — всё boundaries)."""
    return sum(ring_length(ring) for _p, _r, ring in _iter_rings(polys))


def _iter_rings(polys):
    """[(part, ring_idx, ring), ...] — все кольца структуры."""
    for pi, part in enumerate(polys or []):
        for ri, ring in enumerate(part):
            yield pi, ri, ring


def geom_area(g):
    try:
        return float(g.area())
    except Exception:
        return 0.0
