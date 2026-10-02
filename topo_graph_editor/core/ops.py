"""Операции над графом (чистый Python, без QGIS).

plan_vertex_move() ничего не меняет: он строит план переноса вершины,
считает новые кольца, изменение площадей и список нарушений.
Если нарушения есть, правку применять нельзя.

Проверяются только отрезки, которые реально изменились (инцидентные
переносимой вершине): любое новое пересечение обязательно затрагивает хотя бы
один из них. Это делает проверку достаточно быстрой для предпросмотра.

Допуск (tolerance) играет роль единственного эпсилон: касания ближе допуска
считаются касаниями.
"""
import math
from collections import Counter, defaultdict, namedtuple
from dataclasses import dataclass, field
from itertools import chain

# p, q - координаты концов; vp, vq - id вершин (None для внешних объектов);
# key = (face_id, номер кольца); idx - индекс первой точки отрезка в кольце;
# moved - отрезок инцидентен переносимой вершине (его геометрия изменилась)
Seg = namedtuple("Seg", "p q vp vq key idx moved")

SELF_INTERSECTION = "self_intersection"
CROSSES_OTHER_FACE = "crosses_other_face"
MERGE_SAME_FACE = "merge_same_face"  # слияние вершин одного и того же участка


@dataclass
class Violation:
    kind: str          # SELF_INTERSECTION | CROSSES_OTHER_FACE
    detail: str        # 'cross' | 'touch' | 'overlap'
    point: tuple       # где именно возникает нарушение
    segments: tuple    # ((p, q), (r, s)) в новой геометрии
    faces: tuple       # (грань изменяемого отрезка, грань второго отрезка)


@dataclass
class MovePlan:
    vertex: int
    new_xy: tuple
    point_inserts: list = field(default_factory=list)  # [(face_id, ring_idx, point_idx)]
    point_moves: list = field(default_factory=list)    # [(face_id, ring_idx, point_idx)]
    new_rings: dict = field(default_factory=dict)    # (face_id, ring_idx) -> кольцо
    areas: dict = field(default_factory=dict)        # face_id -> (старая, новая)
    violations: list = field(default_factory=list)
    merge_into: object = None   # vid вершины, с которой сливается переносимая

    @property
    def ok(self):
        return bool(self.point_moves or self.point_inserts) and not self.violations


# ---------------------------------------------------------------- геометрия
def ring_area(ring):
    if len(ring) < 3:
        return 0.0
    ox, oy = ring[0]  # локальное начало: меньше потеря точности на больших координатах
    s = 0.0
    n = len(ring)
    for i in range(n):
        x1, y1 = ring[i][0] - ox, ring[i][1] - oy
        x2, y2 = ring[(i + 1) % n][0] - ox, ring[(i + 1) % n][1] - oy
        s += x1 * y2 - x2 * y1
    return abs(s) / 2.0


def face_area(rings):
    if not rings:
        return 0.0
    return ring_area(rings[0]) - sum(ring_area(r) for r in rings[1:])


def _orient(a, b, c):
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _pt_seg_dist(p, a, b):
    dx, dy = b[0] - a[0], b[1] - a[1]
    l2 = dx * dx + dy * dy
    if l2 == 0.0:
        return math.hypot(p[0] - a[0], p[1] - a[1])
    t = max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / l2))
    return math.hypot(p[0] - (a[0] + t * dx), p[1] - (a[1] + t * dy))


def segment_contact(p1, p2, p3, p4, eps):
    """Контакт отрезков p1p2 и p3p4: ('cross'|'touch'|'overlap', точка) или None."""
    if (max(p1[0], p2[0]) + eps < min(p3[0], p4[0]) or max(p3[0], p4[0]) + eps < min(p1[0], p2[0])
            or max(p1[1], p2[1]) + eps < min(p3[1], p4[1])
            or max(p3[1], p4[1]) + eps < min(p1[1], p2[1])):
        return None

    touches = []
    for pt, a, b in ((p1, p3, p4), (p2, p3, p4), (p3, p1, p2), (p4, p1, p2)):
        if _pt_seg_dist(pt, a, b) <= eps:
            touches.append(pt)
    if touches:
        first = touches[0]
        for t in touches[1:]:
            if math.hypot(t[0] - first[0], t[1] - first[1]) > eps:
                return "overlap", ((first[0] + t[0]) / 2.0, (first[1] + t[1]) / 2.0)
        return "touch", first

    d1, d2 = _orient(p3, p4, p1), _orient(p3, p4, p2)
    d3, d4 = _orient(p1, p2, p3), _orient(p1, p2, p4)
    if d1 * d2 < 0 and d3 * d4 < 0:
        t = d1 / (d1 - d2)
        return "cross", (p1[0] + t * (p2[0] - p1[0]), p1[1] + t * (p2[1] - p1[1]))
    return None


def _pair_key(s):
    if s.vp is None or s.vq is None:
        return None
    return (s.vp, s.vq) if s.vp < s.vq else (s.vq, s.vp)


def _contact(s, t, eps):
    """Нарушение между отрезками s и t (отрезки не идентичны по вершинам)."""
    shared = {s.vp, s.vq} & {t.vp, t.vq}
    shared.discard(None)
    if shared:
        # отрезки сходятся в общей вершине - это нормально, если не накладываются
        v = next(iter(shared))
        s_pos, s_other = (s.p, s.q) if s.vp == v else (s.q, s.p)
        t_other = t.q if t.vp == v else t.p
        if _pt_seg_dist(s_other, t.p, t.q) <= eps or _pt_seg_dist(t_other, s.p, s.q) <= eps:
            return "overlap", s_pos
        return None
    return segment_contact(s.p, s.q, t.p, t.q, eps)


def _ring_segments(face_id, ri, ring, vids, moved_idx=()):
    """Отрезки кольца. vids=None для внешних объектов (вершины неизвестны).

    moved_idx - индексы точек, которые переносятся (для признака Seg.moved).
    """
    n = len(ring)
    if n < 2:
        return []
    if vids is None:
        closed = ring[0] == ring[-1]
    else:
        closed = vids[0] == vids[-1]
    count = n - 1 if closed else n
    segs = []
    for i in range(count):
        j = (i + 1) % n
        vp = vids[i] if vids else None
        vq = vids[j] if vids else None
        if vids and vp == vq:
            continue  # нулевая длина (дубль точки)
        moved = i in moved_idx or j in moved_idx
        segs.append(Seg(ring[i], ring[j], vp, vq, (face_id, ri), i, moved))
    return segs


# ------------------------------------------------------------------ операции
def nearest_vertex(graph, xy, radius):
    """id ближайшей вершины графа в радиусе или None."""
    best, best_d = None, None
    r2 = radius * radius
    for vid in graph.vertex_refs:
        x, y = graph.coords[vid]
        d = (x - xy[0]) ** 2 + (y - xy[1]) ** 2
        if d <= r2 and (best_d is None or d < best_d):
            best, best_d = vid, d
    return best


def _first_seg_idx(pv, vid):
    """Индекс первого сегмента кольца, начинающегося в вершине vid.

    Сегмент i - это отрезок pv[i] -> pv[i+1] (для замкнутого кольца замыкающая
    копия не считается отдельным сегментом). Возвращает None, если ни один
    сегмент не выходит из vid.
    """
    n = len(pv)
    closed = pv[0] == pv[-1]
    count = n - 1 if closed else n
    for i in range(count):
        if pv[i] == vid and pv[(i + 1) % n] != vid:
            return i
    return None


def _shared_pairs_at(graph, vid):
    """Пары вершин {(a, b)}, которые являются ОБЩИМ ребром двух и более граней
    и выходят из вершины vid (a == vid или b == vid)."""
    pairs = set()
    for key, pv in graph.point_vids.items():
        face = key[0]
        n = len(pv)
        closed = pv[0] == pv[-1]
        count = n - 1 if closed else n
        for i in range(count):
            a, b = pv[i], pv[(i + 1) % n]
            if a == b or vid not in (a, b):
                continue
            pk = (a, b) if a < b else (b, a)
            pairs.add((pk, face))
    shared = set()
    by_pair = {}
    for pk, face in pairs:
        by_pair.setdefault(pk, set()).add(face)
    for pk, faces in by_pair.items():
        if len(faces) >= 2:
            shared.add(pk)
    return shared


def _choose_insert_segments(graph, vid):
    """Кольца и позиции для вставки новой точки на рёбра вершины vid.

    Возвращает {ключ_кольца: point_idx}, где point_idx - индекс точки, ПОСЛЕ
    которой вставляется новая вершина. Приоритет: сегменты, являющиеся общим
    ребром двух участков (щель гарантированно не появится). Если общих рёбер
    у вершины нет (внешний угол/изолированный участок), берётся первый
    сегмент каждого кольца, выходящий из vid.
    """
    shared = _shared_pairs_at(graph, vid)
    chosen = {}
    if shared:
        # сначала общие рёбра: в каждое кольцо, содержащее общее ребро,
        # вставляем точку именно на сторону этого ребра
        for key, pv in graph.point_vids.items():
            if key in chosen:
                continue
            n = len(pv)
            closed = pv[0] == pv[-1]
            count = n - 1 if closed else n
            for i in range(count):
                a, b = pv[i], pv[(i + 1) % n]
                if a != vid:
                    continue
                pk = (a, b) if a < b else (b, a)
                if pk in shared:
                    chosen[key] = i
                    break
        # дополняем кольцами, где vid участвует, но общего ребра нет
        for key, _idxs in graph.vertex_refs.get(vid, ()):
            if key in chosen:
                continue
            pv = graph.point_vids.get(key)
            if pv is None:
                continue
            idx = _first_seg_idx(pv, vid)
            if idx is not None:
                chosen[key] = idx
    else:
        for key, _idxs in graph.vertex_refs.get(vid, ()):
            if key in chosen:
                continue
            pv = graph.point_vids.get(key)
            if pv is None:
                continue
            idx = _first_seg_idx(pv, vid)
            if idx is not None:
                chosen[key] = idx
    return chosen or None



def find_snap_target(graph, vid, xy, radius):
    """Вершина графа для слияния: ближайшая к xy в радиусе, не принадлежащая
    ни одному из участков переносимой вершины (такое слияние не поддерживается,
    поэтому вершины своего участка не предлагаются)."""
    refs = graph.vertex_refs.get(vid)
    if not refs:
        return None
    own_faces = {k[0] for k, _ in refs}
    best, best_d = None, None
    r2 = radius * radius
    for w, wrefs in graph.vertex_refs.items():
        if w == vid:
            continue
        x, y = graph.coords[w]
        d = (x - xy[0]) ** 2 + (y - xy[1]) ** 2
        if d > r2 or (best_d is not None and d >= best_d):
            continue
        if any(k[0] in own_faces for k, _ in wrefs):
            continue
        best, best_d = w, d
    return best


def plan_vertex_move(graph, vid, new_xy, tolerance, extra_faces_provider=None,
                     merge_into=None, insert=False):
    """План переноса вершины vid в new_xy.

    merge_into - vid вершины, с которой нужно слить переносимую (закрытие щели).
    Тогда new_xy игнорируется, берутся координаты целевой вершины, а в проверках
    обе вершины считаются одной (общие отрезки - это нормальное примыкание,
    а не касание). Слияние вершин одного участка не поддерживается.

    insert=True - вместо переноса существующей вершины во все кольца, проходящие
    через vid, добавляется НОВАЯ вершина в new_xy (добавление вершины на ребро);
    исходная вершина остаётся на месте. merge_into при этом не используется.

    extra_faces_provider(bbox) -> {face_id: rings}: объекты вне графа, с
    которыми тоже нужно проверить пересечение (bbox = (xmin, ymin, xmax, ymax)).
    """
    eps = float(tolerance)
    new_xy = (float(new_xy[0]), float(new_xy[1]))
    plan = MovePlan(vertex=vid, new_xy=new_xy)

    refs = graph.vertex_refs.get(vid)
    if not refs:
        return plan
    if insert:
        merge_into = None
    if merge_into is not None:
        if merge_into == vid or merge_into not in graph.vertex_refs:
            return plan
        new_xy = graph.coords[merge_into]
        plan.new_xy = new_xy
        plan.merge_into = merge_into

    affected = defaultdict(list)
    for key, idx in refs:
        affected[key].append(idx)
    rotated_pv = {}  # для insert: ключ кольца -> point_vids в той же ротации
    if insert:
        # одна и та же вершина может встречаться в кольце несколько раз
        # (замыкающая копия первой точки, многократные визиты в T-узел).
        # В каждое затронутое кольцо новая точка вставляется ровно один раз -
        # в первый сегмент, выходящий из vid; остальные вхождения вершины
        # остаются нетронутыми. Иначе получился бы нулевой участок ("игла")
        # и ложные пересечения.
        chosen = _choose_insert_segments(graph, vid)
        if chosen is None:
            return plan
        affected = {k: [i] for k, i in chosen.items()}
    for key, idxs in affected.items():
        ring = list(graph.faces[key[0]][key[1]])
        pv = graph.point_vids.get(key)
        closed = bool(pv) and pv[0] == pv[-1]
        n = len(ring)
        if insert:
            (chosen_idx,) = idxs
            if closed and chosen_idx == n - 1:
                # у замкнутого кольца последняя точка - копия первой; если
                # выбранный сегмент идёт перед этой копией, разворачиваем
                # кольцо, чтобы вставка осталась внутри нетривиального
                # сегмента и не съехала в замыкающий "нулевой" сегмент
                k = 1
                ring = ring[k:] + ring[:k]
                idxs = [chosen_idx - k]
                affected[key] = idxs
                if pv is not None:
                    rotated_pv[key] = list(pv[k:]) + list(pv[:k])
        for i in sorted(idxs):
            if insert:
                pos = i + 1
                ring.insert(pos, new_xy)
                plan.point_inserts.append((key[0], key[1], pos))
            else:
                ring[i] = new_xy
                plan.point_moves.append((key[0], key[1], i))
        plan.new_rings[key] = ring

    if merge_into is not None:
        faces_v = {k[0] for k in affected}
        faces_w = {k[0] for k, _ in graph.vertex_refs[merge_into]}
        for face in sorted(faces_v & faces_w, key=str):
            plan.violations.append(Violation(MERGE_SAME_FACE, "touch", new_xy, (), (face, face)))
        if plan.violations:
            return plan

    # отрезки всех колец графа в новой геометрии
    all_segs = []
    for key, pv in graph.point_vids.items():
        ring = plan.new_rings.get(key) or graph.faces[key[0]][key[1]]
        moved_idx = affected.get(key, ())
        if key in rotated_pv:
            pv = rotated_pv[key]
        if insert:
            # новая точка ещё не является вершиной графа: её vid неизвестен,
            # чтобы соседние с ней отрезки честно проверялись на пересечения;
            # при этом отрезки между двумя старыми вершинами сохраняют пару
            # вершин и корректно считаются общими рёбрами в обеих гранях.
            # Вставляем None ровно в одну позицию - сразу перед новой точкой.
            ins = [(f, r, p) for f, r, p in plan.point_inserts if (f, r) == key]
            if ins:
                pos = ins[0][2]
                pv = list(pv[:pos]) + [None] + list(pv[pos:])
        if merge_into is not None and key in affected:
            pv = [merge_into if v == vid else v for v in pv]  # обе вершины - одна
        all_segs.extend(_ring_segments(key[0], key[1], ring, pv, moved_idx))

    # изменённые отрезки. При переносе - по одному представителю на ребро
    # (пару вершин); общие рёбра встречаются в нескольких гранях. При вставке
    # меняется только выбранный сегмент: его половины под одной парой вершин
    # хранятся во всех затронутых гранях вместе (иначе нормальное общее ребро
    # соседа ложно считалось бы пересечением), а отрезки с новой точкой
    # (vid неизвестен) - отдельно.
    changed = {}   # pair -> [(seg, faces), ...] (вставка) | pair -> [seg, faces] (перенос)
    unpaired = {}  # (ключ кольца, индекс) -> [seg, {face}] - отрезки с новой точкой
    if insert:
        for s in all_segs:
            if s.key not in affected:
                continue
            pk = _pair_key(s)
            if pk is None:  # отрезок с новой (ещё не вершиной) точкой
                ent = unpaired.setdefault((s.key, s.idx), [s, set()])
                ent[1].add(s.key[0])
            else:
                changed.setdefault(pk, []).append((s, {s.key[0]}))
        # половины одного сегмента принадлежат одним и тем же граням:
        # объединяем наборы граней у одинаковых пар
        merged = {}
        for pk, entries in changed.items():
            faces = set().union(*(f for _, f in entries))
            merged[pk] = [(s, faces) for s, _ in entries]
        changed = merged
        change_items = ([
            (pk, s, faces) for pk, entries in changed.items() for s, faces in entries
        ] + [
            (ukey, s, faces) for ukey, (s, faces) in unpaired.items()
        ])
    else:
        for s in all_segs:
            if s.moved:
                pk = _pair_key(s)
                if pk is None:
                    continue
                ent = changed.setdefault(pk, [s, set()])
                ent[1].add(s.key[0])
        change_items = [(pk, e[0], e[1]) for pk, e in changed.items()]

    # внешние объекты в зоне изменённых отрезков
    extra_segs = []
    if extra_faces_provider is not None and change_items:
        xs = [c for _, s, _ in change_items for c in (s.p[0], s.q[0])]
        ys = [c for _, s, _ in change_items for c in (s.p[1], s.q[1])]
        bbox = (min(xs) - 2 * eps, min(ys) - 2 * eps, max(xs) + 2 * eps, max(ys) + 2 * eps)
        for fid, rings in (extra_faces_provider(bbox) or {}).items():
            for ri, ring in enumerate(rings):
                extra_segs.extend(_ring_segments(fid, ri, ring, None))

    seen = set()

    def add(kind, detail, point, s, t, tkey):
        dedupe = (kind, tkey)
        if dedupe in seen:
            return
        seen.add(dedupe)
        plan.violations.append(
            Violation(kind, detail, point, ((s.p, s.q), (t.p, t.q)), (s.key[0], t.key[0]))
        )

    # «шип»: одно и то же ребро дважды в одном кольце
    occ = Counter()
    for s in all_segs:
        pk = _pair_key(s)
        if pk is not None and pk in changed:
            occ[(s.key[0], pk)] += 1
    for (face, pk), n in occ.items():
        if n > 1:
            first = changed[pk][0]
            s = first[0] if insert else first
            mid = ((s.p[0] + s.q[0]) / 2.0, (s.p[1] + s.q[1]) / 2.0)
            add(SELF_INTERSECTION, "overlap", mid, s, s, ("spike", face, pk))

    # отрезки самих изменённых колец не проверяются между собой: при вставке
    # точки соседние половины старого сегментa легально касаются новой вершины
    affected_keys = set(affected)
    own_keys = {("own", s.key, s.idx) for _, s, _ in change_items}
    for pk, s, faces_of_s in change_items:
        for t in chain(all_segs, extra_segs):
            tk = _pair_key(t)
            if insert:
                if t.key in affected_keys:
                    continue  # тот же контур: пересечь его новой точке негде
                if tk == pk:
                    continue  # то же общее ребро в другой грани - нормально
            elif tk == pk:
                continue  # то же ребро в другой грани - нормально
            res = _contact(s, t, eps)
            if res is None:
                continue
            detail, point = res
            same = t.key[0] in faces_of_s
            kind = SELF_INTERSECTION if same else CROSSES_OTHER_FACE
            tkey = (pk, tk) if tk is not None else (pk, t.key, t.idx)
            add(kind, detail, point, s, t, (tkey, same))

    for face in {k[0] for k in affected}:
        old = face_area(graph.faces[face])
        rings = [plan.new_rings.get((face, ri), r) for ri, r in enumerate(graph.faces[face])]
        plan.areas[face] = (old, face_area(rings))

    return plan
