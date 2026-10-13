# -*- coding: utf-8 -*-
"""Аудит узлов на границах слоёв — чистое ядро (БЕЗ импорта QGIS).

Задача (v1.6.0): на стыке двух групп слоёв — редактируемый слой против
слоёв прилипания — показать узлы ОБЕИХ сторон одновременно и подсветить
топологические ошибки: узлы, у которых нет пары на противоположной
стороне общей границы.

Классификация узла группы A (против группы B):
  plain  — узел совпадает (eps_match) хотя бы с одним узлом B, ЛИБО
           вовсе не касается границы B (внутренняя вершина, другой стык);
  error  — узел НЕ совпадает ни с одним узлом B, но лежит (eps_touch)
           на каком-нибудь сегменте границы B.

Интерпретация ошибок:
  * error_a — «лишний» узел редактируемого слоя: граница эталонов
    проходит сквозь него, а узла-пары у эталонов нет;
  * error_b — «недостающий» узел у редактируемого слоя: у эталонов узел
    есть, а на нашей стороне границы узла в этой точке нет.

Пороги согласованы с ядром плагина (geo_utils): eps_match =
TOPO_MATCH_EPS (1e-6) — какие вершины считаются одним узлом;
eps_touch = COLLINEAR_EPS (1e-4) — «узел лежит на чужой границе».
Единицы — CRS КАРТЫ: оверлей собирает геометрию в CRS карты. Зазор
между слоями больше eps_touch ошибкой узлов НЕ считается (это уже
геометрическое расхождение границ, а не лишний/недостающий узел).

Все координаты — кортежи (x, y) из float, модуль не импортирует QGIS
и тестируется офлайн (tests/test_geo_utils.py, секция 25).

Индексация: узлы — равномерная сетка с ячейкой eps (поиск 3x3
гарантирует нахождение совпадения в пределах eps); сегменты — сетка с
ячейкой среднеквадратичной длины сегмента, отрезок регистрируется во
ВСЕ ячейки, которые он пересекает (семплы через полклетки), поэтому
точка в eps_touch от отрезка обязательно найдёт его в своей 3x3
окрестности — O(1) в среднем на точку вместо полного перебора
сегментов (при лимитах MAX_NODES x MAX_SEGS полный перебор занимал бы
десятки секунд).
"""

import math

# Топологическое совпадение узлов (единицы CRS карты) — как TOPO_MATCH_EPS.
EPS_MATCH = 1e-6
# «Узел лежит на границе чужого слоя» (единицы CRS карты) — как COLLINEAR_EPS.
EPS_TOUCH = 1e-4

# Предохранители тяжёлых видимых областей (на группу слоёв). При
# превышении аудит обрезается и оверлей сообщает «приблизьте масштаб».
MAX_NODES = 5000        # уникальных узлов после дедупликации
MAX_SEGS = 15000        # сегментов границы
MAX_RAW_POINTS = 15000  # сырых вершин до дедупликации

_MIN_EPS = 1e-12


def _norm_eps(eps):
    """Допуск >= _MIN_EPS (0/отрицательный/NaN не должен делить сетку)."""
    try:
        eps = float(eps)
    except Exception:
        return _MIN_EPS
    if not (eps > _MIN_EPS):  # отсекает и NaN
        return _MIN_EPS
    return eps


def _cell(x, y, cell):
    """Ключ ячейки сетки (floor-деление — корректно и для отрицательных)."""
    return (int(math.floor(x / cell)), int(math.floor(y / cell)))


def _pt_seg_dist2(px, py, ax, ay, bx, by):
    """Квадрат расстояния от точки (px, py) до отрезка (ax, ay)-(bx, by)."""
    dx = bx - ax
    dy = by - ay
    l2 = dx * dx + dy * dy
    if l2 <= 0.0:
        ex = px - ax
        ey = py - ay
        return ex * ex + ey * ey
    t = ((px - ax) * dx + (py - ay) * dy) / l2
    if t < 0.0:
        t = 0.0
    elif t > 1.0:
        t = 1.0
    ex = px - (ax + t * dx)
    ey = py - (ay + t * dy)
    return ex * ex + ey * ey


def dedup_points(pts, eps=EPS_MATCH):
    """Уникальные точки (взаимное расстояние > eps), порядок сохраняется.

    Смежные полигоны одного слоя дают вершины-двойники — без дедупа они
    удваивали бы и маркеры, и счётчики. Точки, совпадающие в пределах
    eps, считаются ОДНИМ узлом (та же философия, что у TOPO_MATCH_EPS).

    :param pts: iterable кортежей (x, y)
    :return: список кортежей (x, y) float
    """
    eps = _norm_eps(eps)
    eps2 = eps * eps
    grid = {}
    out = []
    for p in pts:
        try:
            x = float(p[0])
            y = float(p[1])
        except Exception:
            continue
        cx, cy = _cell(x, y, eps)
        dup = False
        for ix in (cx - 1, cx, cx + 1):
            for iy in (cy - 1, cy, cy + 1):
                for qx, qy in grid.get((ix, iy), ()):
                    dx = x - qx
                    dy = y - qy
                    if dx * dx + dy * dy <= eps2:
                        dup = True
                        break
                if dup:
                    break
            if dup:
                break
        if dup:
            continue
        out.append((x, y))
        grid.setdefault((cx, cy), []).append((x, y))
    return out


def _seg_cells(ax, ay, bx, by, cell):
    """Множество ключей ячеек, которые пересекает отрезок ab.

    Семплы вдоль отрезка с шагом <= cell/2: непрерывный отрезок не может
    «перескочить» ячейку (кроме прохода ровно через угол — на реальных
    данных поглощается допуском eps_touch << cell).
    """
    steps = int(max(abs(bx - ax), abs(by - ay)) / (cell * 0.5)) + 1
    keys = set()
    for i in range(steps + 1):
        t = float(i) / steps
        keys.add(_cell(ax + (bx - ax) * t, ay + (by - ay) * t, cell))
    return keys


class _SegIndex(object):
    """Сеточный индекс сегментов: кандидаты для точки за O(1) в среднем."""

    def __init__(self, segs, eps_touch=EPS_TOUCH):
        eps = _norm_eps(eps_touch)
        self.eps2 = eps * eps
        clean = []
        for s in segs:
            try:
                a = (float(s[0][0]), float(s[0][1]))
                b = (float(s[1][0]), float(s[1][1]))
            except Exception:
                continue
            clean.append((a, b))
        self.segs = clean
        self.grid = {}
        if not clean:
            self.cell = None
            return
        sum_len2 = 0.0
        for (ax, ay), (bx, by) in clean:
            dx = bx - ax
            dy = by - ay
            sum_len2 += dx * dx + dy * dy
        # среднеквадратичная длина: короткие сегменты границы — в 1-2
        # ячейках, редкие длинные регистрируются по всему пути
        self.cell = max(math.sqrt(sum_len2 / len(clean)), eps * 8.0)
        for idx, (a, b) in enumerate(clean):
            for key in _seg_cells(a[0], a[1], b[0], b[1], self.cell):
                self.grid.setdefault(key, []).append(idx)

    def has_near(self, x, y):
        """True — есть сегмент в пределах eps_touch от точки (x, y)."""
        if self.cell is None:
            return False
        cx, cy = _cell(x, y, self.cell)
        seen = set()
        for ix in (cx - 1, cx, cx + 1):
            for iy in (cy - 1, cy, cy + 1):
                for idx in self.grid.get((ix, iy), ()):
                    if idx in seen:
                        continue
                    seen.add(idx)
                    (ax, ay), (bx, by) = self.segs[idx]
                    if _pt_seg_dist2(x, y, ax, ay, bx, by) <= self.eps2:
                        return True
        return False


def _points_grid(pts, eps):
    """Сетка точек с ячейкой eps: ключ -> список индексов."""
    grid = {}
    for i, (x, y) in enumerate(pts):
        grid.setdefault(_cell(x, y, eps), []).append(i)
    return grid


def _has_coincide(grid, pts, x, y, eps):
    """True — в сетке есть точка в пределах eps от (x, y) (поиск 3x3)."""
    eps2 = eps * eps
    cx, cy = _cell(x, y, eps)
    for ix in (cx - 1, cx, cx + 1):
        for iy in (cy - 1, cy, cy + 1):
            for i in grid.get((ix, iy), ()):
                qx, qy = pts[i]
                dx = x - qx
                dy = y - qy
                if dx * dx + dy * dy <= eps2:
                    return True
    return False


def classify_border_nodes(pts_a, segs_a, pts_b, segs_b,
                          eps_match=EPS_MATCH, eps_touch=EPS_TOUCH):
    """Классификация узлов двух групп слоёв (см. докстринг модуля).

    :param pts_a: узлы группы A (например, редактируемый слой), [(x, y)]
    :param segs_a: сегменты границы группы A, [((x1, y1), (x2, y2))]
    :param pts_b: узлы группы B (слои-эталоны), [(x, y)]
    :param segs_b: сегменты границы группы B
    :param eps_match: допуск совпадения узлов (единицы CRS карты)
    :param eps_touch: допуск «узел лежит на чужой границе»
    :return: {"plain_a": [...], "error_a": [...],
              "plain_b": [...], "error_b": [...]}
             — уникальные кортежи (x, y); error_* — узлы на чужой
             границе без пары (топологические ошибки)
    """
    eps_m = _norm_eps(eps_match)
    eps_t = _norm_eps(eps_touch)
    ua = dedup_points(pts_a or [], eps_m)[:MAX_NODES]
    ub = dedup_points(pts_b or [], eps_m)[:MAX_NODES]
    segs_a = [s for s in (segs_a or []) if s][:MAX_SEGS]
    segs_b = [s for s in (segs_b or []) if s][:MAX_SEGS]

    grid_a = _points_grid(ua, eps_m)
    grid_b = _points_grid(ub, eps_m)
    idx_a = _SegIndex(segs_a, eps_t) if segs_a else None
    idx_b = _SegIndex(segs_b, eps_t) if segs_b else None

    plain_a, err_a, plain_b, err_b = [], [], [], []
    for x, y in ua:
        if _has_coincide(grid_b, ub, x, y, eps_m):
            plain_a.append((x, y))
        elif idx_b is not None and idx_b.has_near(x, y):
            err_a.append((x, y))   # лишний узел редактируемого слоя
        else:
            plain_a.append((x, y))
    for x, y in ub:
        if _has_coincide(grid_a, ua, x, y, eps_m):
            plain_b.append((x, y))
        elif idx_a is not None and idx_a.has_near(x, y):
            err_b.append((x, y))   # недостающий узел у редактируемого слоя
        else:
            plain_b.append((x, y))
    return {"plain_a": plain_a, "error_a": err_a,
            "plain_b": plain_b, "error_b": err_b}
