# -*- coding: utf-8 -*-
"""Валидатор топологических операций (этап 1 ТЗ «полностью топологический»).

Каждое правило — класс с методом check(...), возвращающим список
Violation. Оркестратор validate_operation() прогоняет правила по ВСЕМ
затронутым полигонам: перемещаемым и соседям (включая изменённые
обрезкой).

Уровни серьёзности:
  error   — блокирует запись: самопересечение, новая щель, перехлёст
            без выбора «оставить»;
  warning — видно в сообщении, не блокирует: шипы и потеря общих узлов
            (настраивается в QgsSettings).

makeValid из конвейера записи убран (этап 1): невалидная геометрия —
ошибка с точкой места нарушения, а не тихая починка формы.

Модуль зависит только от qgis.core — без qgis.gui (тесты, будущие
операции, серверное использование).
"""

import math
from collections import defaultdict

from qgis.core import QgsGeometry, QgsPointXY, QgsWkbTypes

# --- уровни серьёзности -------------------------------------------------

SEV_ERROR = "error"      # блокирует запись
SEV_WARNING = "warning"  # видно в сообщении, не блокирует

# --- коды нарушений ------------------------------------------------------

CODE_SELF_INTERSECTION = u"self_intersection"   # самопересечение колец
CODE_INVALID = u"invalid_geometry"             # прочие GEOS-ошибки
CODE_SPIKE = u"spike"                          # шип (угол меньше порога)
CODE_DUP_VERTEX = u"dup_vertex"                # две подряд совпадающие вершины
CODE_OVERLAP = u"same_layer_overlap"           # перехлёст с соседом слоя
CODE_NEW_GAP = u"new_gap"                      # новая щель
CODE_SHARED_BOUNDARY = u"shared_boundary_lost" # общий узел потерян

# --- пороги по умолчанию (ТЗ, «Пороги по умолчанию»; переопределяются
#     в QgsSettings через topopolyedit.settings) --------------------------

DEFAULT_OVERLAP_MIN_RATIO = 1e-8  # как find_overlaps(min_ratio) v1.5.0
DEFAULT_SEAM_MIN_RATIO = 1e-8     # щель-«шов» стыковки игнорируется
DEFAULT_SPIKE_MIN_ANGLE_DEG = 0.5
DEFAULT_SPIKE_SEVERITY = SEV_WARNING
DEFAULT_SHARED_SEVERITY = SEV_WARNING
DEFAULT_NDIGITS = 6  # округление узлов при сравнении «узел -> фичи»


class Violation(object):
    """Одно нарушение: код, серьёзность, фича, точка, геометрия, текст.

    point — QgsPointXY в CRS слоя (для «Показать место»); geom —
    QgsGeometry полигона перехлёста/щели (для красной заливки). В отчёт
    операции попадает облегчённый to_dict() без геометрии.
    """

    __slots__ = ("code", "severity", "fid", "message", "point", "geom")

    def __init__(self, code, severity, fid=None, message=u"",
                 point=None, geom=None):
        self.code = code
        self.severity = severity
        self.fid = fid
        self.message = message
        self.point = point
        self.geom = geom

    def to_dict(self):
        pt = None
        if self.point is not None:
            try:
                pt = (self.point.x(), self.point.y())
            except Exception:
                pt = None
        return {"code": self.code, "severity": self.severity,
                "fid": self.fid, "message": self.message, "point": pt}


def blocking(violations):
    """Нарушения уровня error (блокируют запись)."""
    return [v for v in violations if v.severity == SEV_ERROR]


# -----------------------------------------------------------------------
# Правила по одной геометрии (атрибут PER_GEOM = True)
# -----------------------------------------------------------------------

class GeosValidity(object):
    """И1: isGeosValid() каждой затронутой геометрии; всегда error.

    Точки нарушений берутся из QgsGeometry.validateGeometry() (метод
    GEOS по умолчанию). Код self_intersection ставится по эвристике
    «intersect» в сообщении GEOS (сообщения локализованы — эвристика
    лучшее, что доступно без парсинга WKB); прочие ошибки — invalid_
    geometry с текстом GEOS.
    """

    NAME = u"геометрия"
    PER_GEOM = True

    @staticmethod
    def _validator_errors(g):
        """[(сообщение, QgsPointXY|None), ...] по validateGeometry()."""
        try:
            method = getattr(QgsGeometry, "ValidatorGEOS", None)
            if method is not None:
                errs = g.validateGeometry(method=method)
            else:
                errs = g.validateGeometry()
        except Exception:
            return []
        out = []
        for err in errs or []:
            msg = u""
            f = getattr(err, "message", None)
            if callable(f):
                try:
                    msg = f()
                except Exception:
                    msg = u""
            pt = None
            try:
                if err.hasWhere():
                    pt = err.where()
            except Exception:
                pt = None
            out.append((msg, pt))
        return out

    def check(self, fid, g, ctx=None):
        if g is None or g.isNull() or g.isEmpty():
            return [Violation(CODE_INVALID, SEV_ERROR, fid,
                              u"пустая геометрия")]
        if g.isGeosValid():
            return []
        errs = self._validator_errors(g)
        if not errs:
            return [Violation(CODE_INVALID, SEV_ERROR, fid,
                              u"геометрия невалидна (GEOS)")]
        out = []
        for msg, pt in errs:
            low = (msg or u"").lower()
            code = CODE_SELF_INTERSECTION if u"intersect" in low \
                else CODE_INVALID
            out.append(Violation(
                code, SEV_ERROR, fid,
                msg or u"геометрия невалидна (GEOS)", point=pt))
        return out


class NoSpikes(object):
    """Шипы (угол при вершине меньше SPIKE_MIN_ANGLE_DEG) и две подряд
    идущие совпадающие вершины. Серьёзность — настройка (по умолчанию
    warning). Кольца рассматриваются все: оболочки и дыры."""

    NAME = u"шипы"
    PER_GEOM = True

    def check(self, fid, g, ctx=None):
        ctx = ctx or {}
        severity = ctx.get("spike_severity", DEFAULT_SPIKE_SEVERITY)
        min_angle = ctx.get("spike_min_angle_deg",
                            DEFAULT_SPIKE_MIN_ANGLE_DEG)
        if g is None or g.isNull() or g.isEmpty():
            return []
        if g.type() != QgsWkbTypes.PolygonGeometry:
            return []
        try:
            polys = g.asMultiPolygon() if g.isMultipart() else [g.asPolygon()]
        except Exception:
            return []
        out = []
        for poly in polys:
            for ring in poly:
                out.extend(self._check_ring(fid, ring, min_angle, severity))
        return out

    @staticmethod
    def _dist(a, b):
        return math.hypot(a.x() - b.x(), a.y() - b.y())

    @classmethod
    def _check_ring(cls, fid, ring, min_angle_deg, severity):
        """Проверка одного замкнутого кольца (первая точка == последняя)."""
        pts = list(ring)
        if len(pts) > 1 and pts[0].x() == pts[-1].x() \
                and pts[0].y() == pts[-1].y():
            pts = pts[:-1]
        n = len(pts)
        if n < 3:
            return []
        # масштаб кольца: диагональ габарита (для допуска «совпадения»)
        xs = [p.x() for p in pts]
        ys = [p.y() for p in pts]
        diag = math.hypot(max(xs) - min(xs), max(ys) - min(ys))
        dup_eps = max(1e-12, 1e-9 * diag)
        out = []
        for i in range(n):
            prev_p = pts[i - 1]
            cur = pts[i]
            next_p = pts[(i + 1) % n]
            d_prev = cls._dist(cur, prev_p)
            d_next = cls._dist(cur, next_p)
            if d_prev <= dup_eps or d_next <= dup_eps:
                # совпадающие подряд вершины: жалоба один раз на пару
                if d_prev <= dup_eps:
                    out.append(Violation(
                        CODE_DUP_VERTEX, severity, fid,
                        u"две подряд идущие совпадающие вершины",
                        point=QgsPointXY(cur)))
                continue  # угол на нулевом отрезке не считается
            v1x, v1y = prev_p.x() - cur.x(), prev_p.y() - cur.y()
            v2x, v2y = next_p.x() - cur.x(), next_p.y() - cur.y()
            cosang = (v1x * v2x + v1y * v2y) / (d_prev * d_next)
            cosang = max(-1.0, min(1.0, cosang))
            ang = math.degrees(math.acos(cosang))
            if ang < min_angle_deg:
                out.append(Violation(
                    CODE_SPIKE, severity, fid,
                    u"шип: угол {:.4g}° меньше порога {:.4g}°".format(
                        ang, min_angle_deg),
                    point=QgsPointXY(cur)))
        return out


# -----------------------------------------------------------------------
# Правила по контексту операции
# -----------------------------------------------------------------------

class NoSameLayerOverlap(object):
    """И2: перехлёст новых геометрий с соседями ТОГО ЖЕ слоя; всегда
    error. Переиспользует find_overlaps(min_ratio) из topo_editor.
    Перехлёст, который пользователь явно выбрал «оставить», не нарушение:
    вызывающий код ставит ctx["overlap_allowed"] = True."""

    NAME = u"перехлёсты"

    def check(self, ctx):
        if ctx.get("overlap_allowed"):
            return []
        new_geoms = ctx.get("new_geoms") or {}
        neighbors = ctx.get("neighbors") or []
        if not new_geoms or not neighbors:
            return []
        ratio = ctx.get("overlap_min_ratio", DEFAULT_OVERLAP_MIN_RATIO)
        from ..topo_editor import find_overlaps  # лениво: цикл импорта
        overlaps = find_overlaps(sorted(new_geoms.items()), neighbors,
                                 min_ratio=ratio)
        out = []
        for o in overlaps:
            out.append(Violation(
                CODE_OVERLAP, SEV_ERROR, o["moved"],
                u"перехлёст с объектом #{} (площадь {:g})".format(
                    o["neighbor"], o["area"]),
                geom=o["geom"]))
        return out


class NoNewGaps(object):
    """И5: новая щель — область, покрытая полигонами слоя до операции и
    не покрытая после: old_union.difference(new_union) в bbox операции.
    Всегда error. Щель-«шов» меньше SEAM_MIN_RATIO × площадь меньшего
    СТАРОГО затронутого полигона игнорируется (шум float на стыках).
    До этапа 3 набор соседей тот же, что у перехлёстов (лимит 500)."""

    NAME = u"новые щели"

    def check(self, ctx):
        old_geoms = ctx.get("old_geoms") or {}
        new_geoms = ctx.get("new_geoms") or {}
        neighbors = ctx.get("neighbors") or []
        if not old_geoms or not new_geoms:
            return []
        ratio = ctx.get("seam_min_ratio", DEFAULT_SEAM_MIN_RATIO)
        try:
            min_area = min(g.area() for g in old_geoms.values())
        except Exception:
            min_area = 0.0
        min_area = max(min_area, 1e-30)
        try:
            from ..topo_editor import polygon_only  # лениво
            old_all = list(old_geoms.values()) + [g for _f, g in neighbors]
            new_all = list(new_geoms.values()) + [g for _f, g in neighbors]
            uo = QgsGeometry.unaryUnion(old_all)
            un = QgsGeometry.unaryUnion(new_all)
            if uo is None or uo.isNull() or un is None or un.isNull():
                return [Violation(
                    CODE_NEW_GAP, SEV_WARNING, None,
                    u"не удалось вычислить объединение для проверки щелей")]
            gap = polygon_only(uo.difference(un))
            if gap is None or gap.isNull() or gap.isEmpty():
                return []
            area = gap.area()
            if area > ratio * min_area:
                return [Violation(
                    CODE_NEW_GAP, SEV_ERROR, None,
                    u"новая щель площадью {:g} (ед. слоя в квадрате)"
                    .format(area),
                    geom=gap)]
            return []
        except Exception as exc:
            # сбой вычисления не должен блокировать операцию «вслепую»:
            # предупреждение вместо ошибки (щели проверит аудит этапа 5)
            return [Violation(
                CODE_NEW_GAP, SEV_WARNING, None,
                u"щели не проверены: {}".format(exc))]


class SharedBoundaryPreserved(object):
    """И3: полигоны, делившие узел до операции, делят общий узел и после
    (все копии узла переносятся вместе). Сравниваются пары «узел ↔ фичи»
    до и после; узел считается сохранившимся, если после операции есть
    узел с набором фич, покрывающим прежний (все копии переехали вместе).
    Серьёзность — настройка (по умолчанию warning)."""

    NAME = u"общие узлы"

    def __init__(self, ndigits=DEFAULT_NDIGITS):
        self.ndigits = ndigits

    def _node_map(self, geoms_with_fid):
        """{округлённая точка: frozenset(fid)} для узлов с 2+ фичами."""
        pts = defaultdict(set)
        for fid, g in geoms_with_fid:
            if g is None or g.isNull() or g.isEmpty():
                continue
            try:
                polys = (g.asMultiPolygon() if g.isMultipart()
                         else [g.asPolygon()])
            except Exception:
                continue
            for poly in polys:
                for ring in poly:
                    for p in ring:
                        pts[(round(p.x(), self.ndigits),
                             round(p.y(), self.ndigits))].add(fid)
        return {k: frozenset(v) for k, v in pts.items() if len(v) > 1}

    def check(self, ctx):
        old_geoms = ctx.get("old_geoms") or {}
        new_geoms = ctx.get("new_geoms") or {}
        neighbors = ctx.get("neighbors") or []
        if not old_geoms or not new_geoms:
            return []
        severity = ctx.get("shared_severity", DEFAULT_SHARED_SEVERITY)
        old_all = sorted(old_geoms.items()) + list(neighbors)
        new_all = sorted(new_geoms.items()) + list(neighbors)
        before = self._node_map(old_all)
        after = self._node_map(new_all)
        after_sets = set(after.values())
        out = []
        for key in sorted(before.keys()):
            fids = before[key]
            if after.get(key, frozenset()) >= fids:
                continue  # узел на месте, набор фич не убыл
            # узел переехал вместе со всеми копиями? ищем узел после
            # с покрывающим набором фич
            if fids in after_sets or any(s >= fids for s in after_sets):
                continue
            lost = fids - after.get(key, frozenset())
            out.append(Violation(
                CODE_SHARED_BOUNDARY, severity, None,
                u"общий узел ({:.6g}, {:.6g}): потеряна связь с фичей {}"
                .format(key[0], key[1],
                        u", ".join(u"#{}".format(f) for f in sorted(lost))),
                point=QgsPointXY(key[0], key[1])))
        return out


# -----------------------------------------------------------------------
# Оркестратор
# -----------------------------------------------------------------------

def default_rules():
    """Набор правил этапа 1 (все пять из ТЗ)."""
    return (GeosValidity(), NoSpikes(), NoSameLayerOverlap(),
            NoNewGaps(), SharedBoundaryPreserved())


def validate_operation(old_geoms, new_geoms, neighbors, rules=None, **opts):
    """Прогоняет правила по ВСЕМ затронутым полигонам (этап 1 ТЗ).

    :param old_geoms: {fid: QgsGeometry} — геометрии затронутых фич ДО
    :param new_geoms: {fid: QgsGeometry} — кандидаты на запись ПОСЛЕ
    :param neighbors: [(fid, QgsGeometry)] — прочие полигоны области
                      (не изменяются, но участвуют в щелях/узлах/перехлёстах)
    :param rules: правила; по умолчанию default_rules()
    :param opts: пороги и серьёзности (spike_min_angle_deg,
                 spike_severity, seam_min_ratio, overlap_min_ratio,
                 shared_severity, overlap_allowed, node_ndigits)
    :return: [Violation] — все нарушения всех правил
    """
    ctx = {
        "old_geoms": dict(old_geoms or {}),
        "new_geoms": dict(new_geoms or {}),
        "neighbors": list(neighbors or []),
    }
    ctx.update(opts)
    if rules is None:
        rules = default_rules()
    out = []
    for rule in rules:
        try:
            if getattr(rule, "PER_GEOM", False):
                for fid in sorted(new_geoms.keys()):
                    out.extend(rule.check(fid, new_geoms[fid], ctx))
            else:
                out.extend(rule.check(ctx))
        except Exception as exc:
            out.append(Violation(
                CODE_INVALID, SEV_WARNING, None,
                u"правило «{}» не удалось проверить: {}".format(
                    getattr(rule, "NAME", rule.__class__.__name__), exc)))
    return out
