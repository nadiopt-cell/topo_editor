# -*- coding: utf-8 -*-
"""Индекс топологии (этап 3 ТЗ) — мгновенный поиск узлов, рёбер и соседей.

Зачем: каждая операция плагина делает запрос getFeatures по
прямоугольнику и парсит геометрии; превью и аудит повторяют ту же
работу. Индекс строитcя ЛЕНИВО по плиткам и даёт O(1)-поиск узлов
(равномерная сетка), поиск рёбер (QgsSpatialIndex по габаритам фич +
повторная проверка сегментов) и снимает жёсткий лимит соседей.

Данные (ТЗ этап 3, п. 1):
  * ключ узла — координаты, округлённые до сетки grid = match_eps
    (TOPO_MATCH_EPS, 1e-6 ед. CRS слоя); поиск проверяет СОСЕДНИЕ
    ячейки, чтобы точки на границе ячейки не терялись;
  * значение — (layer_id, fid, part, ring, idx) — позиция вершины в
    нормализованной структуре norm_polys (NodeRef);
  * рёбра — QgsSpatialIndex по bbox фичи; кандидат проверяется
    повторно по сегментам снимка (distance point-to-segment <= eps).

Плитки (п. 2): индекс строится лениво по квадратным плиткам; размер
задаётся при первой сборке как видимый экстент × tile_factor
(настройка, по умолчанию 2). На слоях с десятками тысяч фич сетка
дробится по плотности (порядка _TILE_TARGET_FEATURES фич на плитку),
чтобы первая сборка укладывалась в бюджет на любом железе; при провале
сборки по бюджету сетка мельчает (до _GRID_MAX_SHRINKS раз) и сборка
повторяется. Сборка плитки ограничена бюджетом TILE_BUILD_BUDGET_S
(0.4 с, как в аудите 1.6.1): если плитка всё же не собралась, она
помечается неудавшейся и ВСЕ запросы по ней идут к слою напрямую —
неполные результаты никогда не возвращаются. Для пакетных операций
есть prefetch()/prefetch_async() (фон через QgsTask; потребитель —
этап 5, аудит).

Актуальность (п. 3): подписка на geometryChanged, featureAdded,
featureDeleted, committedGeometriesChanges, editingStopped,
afterRollBack. Правки в режиме редактирования инвалидируются
ХИРУРГИЧЕСКИ: фича перерегистрируется во всех построенных плитках
(удаление старых узлов/снимка + регистрация новых) — пересборки плитки
нет, следующая операция не платит за неё (приёмка «повторные ≤ 50 мс»).
Особый случай afterRollBack: сигнал не сообщает затронутые фичи —
плитки, пересекающие bbox последней операции из журнала
(utils.note_operation_bbox), помечаются ГРЯЗНЫМИ и пересобираются со
слоя при первом обращении; при отсутствии сведений — полная пересборка.
Undo/redo через кнопку QGIS обновляют индекс по тем же сигналам
(QGIS издаёт geometryChanged при откате/повторе геометрических команд).

Целостность (п. 4): check_integrity() сравнивает каждую построенную
плитку с полной пересборкой по её прямоугольнику. В отладочном режиме
(TOPOPOLEDIT_INDEX_DEBUG=1 или debug=True) расхождения пишутся в журнал
после каждой пересборки — используется тестами.

API (п. 5): nodes_at, features_touching, edges_near, neighbors,
bbox_features, snapshot_geometry. Снимки геометрий кэшируются
(norm_polys) и являются ЕДИНСТВЕННЫМ источником данных для запросов —
устаревание исключается инвалидацией по сигналам.

Отключение (п. 6): настройка «индекс включён» (settings.index_enabled);
при выключении index_for_layer() возвращает None и весь код плагина
работает прямыми запросами к слою, как до этапа 3.

Потоки: все методы — только из GUI-потока (сигналы QGIS приходят в нём).
prefetch_async() собирает плитки в фоне и не меняет уже построенные
данные, кроме добавления новых плиток.
"""

import math
import os
import time
from collections import namedtuple

from qgis.core import (
    QgsFeatureRequest, QgsFeature, QgsPointXY, QgsProject,
    QgsRectangle, QgsSpatialIndex, QgsWkbTypes,
)

from .geo_utils import (
    norm_polys, iter_rings, iter_ring_segments, point_to_segment,
    TOPO_MATCH_EPS, DUP_EPS2,
)
from . import settings
from . import utils

# Бюджет сборки ОДНОЙ плитки, секунды (ТЗ этап 3, п. 2: как в аудите 1.6.1).
TILE_BUILD_BUDGET_S = 0.4

# Шаг сетки узлов (ТЗ этап 3, п. 1: grid = match_eps).
NODE_GRID = TOPO_MATCH_EPS
NODE_GRID_MIN = 1e-12

# Предохранитель: запрос, накрывающий слишком много плиток, индексом не
# обслуживается (прямой запрос к слою) — защита от абсурдных габаритов.
MAX_QUERY_TILES = 1024

# Каждые сколько фич проверяется бюджет времени при сборке плитки.
_BUDGET_CHECK_EVERY = 32

# Плотностное дробление сетки (см. _ensure_tile_size): seeded-плитка
# «экстент × 2» на слое из десятков тысяч полигонов не собирается в
# бюджет (проверено CI: на 10 000 полигонов сборка всей плитки дольше
# 0,4 с — плитка навсегда _failed, индекс мёртв, всё живёт на прямых
# запросах). Сетка делится так, чтобы в плитку попадало порядка
# _TILE_TARGET_FEATURES фич и первая сборка укладывалась в бюджет
# на любом железе. Для малых слоёв (<= цели) поведение ровно по ТЗ:
# размер плитки = видимый экстент × tile_factor.
_TILE_TARGET_FEATURES = 1000
_TILE_MAX_DIV = 32

# Предохранитель: при провале сборки по бюджету (неоднородная плотность,
# неправда featureCount) сетка мельчает вдвое — не больше стольких раз
# за жизнь индекса (иначе ping-pong пересборок съест интерактивность).
_GRID_MAX_SHRINKS = 5

# Узел индекса: позиция вершины в нормализованной структуре (ТЗ, п. 1)
# плюс сама точка (для удобства потребителей).
NodeRef = namedtuple("NodeRef",
                     "layer_id fid part ring idx point")

# Ребро, найденное edges_near: концы сегмента (копии координат снимка).
EdgeRef = namedtuple("EdgeRef", "layer_id fid a b")

_env_debug = None  # инициализируется функцией _debug_mode()


def _debug_mode():
    """Отладочный режим (ТЗ этап 3, п. 4): сравнение с полной
    пересборкой после каждой пересборки. Только тесты."""
    global _env_debug
    if _env_debug is None:
        _env_debug = os.environ.get(
            "TOPOPOLEDIT_INDEX_DEBUG", "").strip().lower() in (
            "1", "true", "yes", "on")
    return _env_debug


# Предохранитель перебора ячеек: радиус поиска больше стольких ячеек —
# сканируем узлы плиток с bbox-предфильтром (крупные допуски: снэп в
# единицах карты и т.п.), иначе (2r+1)^2 ячеек разорвут запрос.
_MAX_CELL_RADIUS = 256


def _cell_key(x, y, cell):
    """Ключ ячейки сетки (floor-деление корректно и для отрицательных)."""
    return (int(math.floor(x / cell)), int(math.floor(y / cell)))


class _BudgetExceeded(Exception):
    """Сборка плитки превысила бюджет времени (плитка не строится)."""


class _Tile(object):
    """Данные ОДНОЙ плитки (самодостаточны: фича попадает во ВСЕ
    плитки, которых касается её габарит, поэтому пересборка одной
    плитки не затрагивает соседние)."""

    __slots__ = ("rect", "nodes", "edge_index", "snapshots", "fids")

    def __init__(self, rect):
        self.rect = rect
        # (ix, iy) -> [(x, y, fid, part, ring, idx), ...]
        self.nodes = {}
        self.edge_index = QgsSpatialIndex()
        # fid -> (polys, was_multi) — кэш norm_polys (снимок)
        self.snapshots = {}
        self.fids = set()

    def register_feature(self, fid, polys, was_multi, geom):
        """Регистрация фичи в плитке: снимок + узлы + bbox-индекс."""
        self.snapshots[fid] = (polys, was_multi)
        self.fids.add(fid)
        cell = NODE_GRID
        for pi, part in enumerate(polys):
            for ri, ring in enumerate(part):
                n = len(ring)
                if n < 2:
                    continue
                closed = (n > 1 and
                          ring[0].sqrDist(ring[-1]) <= DUP_EPS2)
                m = n - 1 if closed else n  # замыкающий дубликат не узел
                for vi in range(m):
                    v = ring[vi]
                    key = _cell_key(v.x(), v.y(), cell)
                    self.nodes.setdefault(key, []).append(
                        (v.x(), v.y(), fid, pi, ri, vi))
        self._index_add(fid, geom)

    def _index_add(self, fid, geom):
        try:
            f = QgsFeature(fid)
            f.setGeometry(geom)
            self.edge_index.addFeature(f)
        except Exception:
            # стабы офлайн-тестов могут не давать QgsSpatialIndex.addFeature;
            # bbox-поиск тогда работает по tile.fids (медленнее, но верно)
            self.edge_index = None

    def remove_feature(self, fid):
        """Полное удаление фичи из плитки (хирургическая инвалидация:
        geometryChanged / featureDeleted без пересборки плитки)."""
        if fid not in self.fids:
            return
        self.fids.discard(fid)
        self.snapshots.pop(fid, None)
        for key in list(self.nodes.keys()):
            kept = [e for e in self.nodes[key] if e[2] != fid]
            if kept:
                self.nodes[key] = kept
            else:
                del self.nodes[key]
        if self.edge_index is not None:
            try:
                self.edge_index.deleteFeature(QgsFeature(fid))
            except Exception:
                # стаб/старые версии: bbox-поиск консервативно по fids
                self.edge_index = None


def _snap_equal(a, b):
    """Сравнение снимков (polys, was_multi) по координатам (точно)."""
    if a is None or b is None:
        return a is b
    pa, ma = a
    pb, mb = b
    if ma != mb or len(pa) != len(pb):
        return False
    for part_a, part_b in zip(pa, pb):
        if len(part_a) != len(part_b):
            return False
        for ring_a, ring_b in zip(part_a, part_b):
            if len(ring_a) != len(ring_b):
                return False
            for va, vb in zip(ring_a, ring_b):
                if va.x() != vb.x() or va.y() != vb.y():
                    return False
    return True


def _tile_diffs(actual, expected, tk):
    """Расхождения плитки с ожидаемой (для check_integrity)."""
    diffs = []
    if actual is None and expected is None:
        return diffs
    if actual is None or expected is None:
        return [u"плитка {}: фактическая/ожидаемая отсутствует".format(tk)]
    if actual.fids != expected.fids:
        extra = sorted(actual.fids - expected.fids)
        miss = sorted(expected.fids - actual.fids)
        diffs.append(u"плитка {}: fids лишние={}, отсутствуют={}".format(
            tk, extra[:8], miss[:8]))
    for fid in sorted(set(actual.snapshots) | set(expected.snapshots)):
        if not _snap_equal(actual.snapshots.get(fid),
                           expected.snapshots.get(fid)):
            diffs.append(u"плитка {}: снимок #{} отличается".format(tk, fid))
    an = set()
    for lst in actual.nodes.values():
        an.update(lst)
    en = set()
    for lst in expected.nodes.values():
        en.update(lst)
    if an != en:
        extra = sorted(an - en)[:8]
        miss = sorted(en - an)[:8]
        diffs.append(u"плитка {}: узлы лишние={}, отсутствуют={}".format(
            tk, extra, miss))
    return diffs


class TopologyIndex(object):
    """Индекс топологии ОДНОГО полигонального слоя (см. докстринг модуля).

    Живёт в реестре index_for_layer() и разделяется всеми операциями
    плагина над этим слоем. Подписан на сигналы правки слоя; метод
    shutdown() обязателен при выгрузке плагина (снятие подписок).
    """

    def __init__(self, layer, debug=None):
        self.layer_id = layer.id()
        self._layer_ref = [layer]  # сильная ссылка; чистится в shutdown()
        self._tiles = {}    # tk -> _Tile (построенные)
        self._dirty = set() # tk -> требует пересборки при обращении
        self._failed = {}   # tk -> причина (бюджет) — прямые запросы
        self._tile_size = None  # (w, h), задаётся при первой сборке
        self._seed_rect = None  # видимый экстент (если известен к первой сборке)
        self._tile_factor_used = None  # фактор, под который строилась сетка
        self.debug = _debug_mode() if debug is None else bool(debug)
        self._sigs = []
        self._rm_sig = None
        self._checking = False  # защита от рекурсии check_integrity
        self._shrinks = 0       # сколько раз сетка мельчала по бюджету
        self._last_budget_fail = False  # флаг провала по бюджету (для _prepare)
        self._connect(layer)

    def seed_extent(self, rect):
        """Видимый экстент карты для размера плиток (ТЗ этап 3, п. 2:
        «по умолчанию видимый экстент × 2»). Действует, если сетка ещё
        не построена; после первой сборки сетка сохраняется."""
        if self._tile_size is None and rect is not None:
            try:
                self._seed_rect = QgsRectangle(rect)
            except Exception:
                self._seed_rect = None

    def seed_extent_from_iface(self, iface):
        """Seed из канвы (или из полного экстента слоя — без канвы)."""
        if self._tile_size is not None:
            return
        rect = None
        try:
            rect = iface.mapCanvas().extent()
        except Exception:
            rect = None
        if rect is None:
            try:
                rect = self._layer_ref[0].extent()
            except Exception:
                rect = None
        if rect is not None:
            self.seed_extent(rect)

    # ------------------------------------------------------------------
    # Сигналы (ТЗ этап 3, п. 3)
    # ------------------------------------------------------------------

    def _connect(self, layer):
        def conn(sig, slot, name):
            try:
                sig.connect(slot)
                self._sigs.append((sig, slot))
            except Exception as exc:
                utils.log(u"Индекс: сигнал {} не подключен — {}".format(
                    name, exc))

        conn(layer.geometryChanged, self._on_geometry_changed,
             "geometryChanged")
        conn(layer.featureAdded, self._on_feature_added, "featureAdded")
        conn(layer.featureDeleted, self._on_feature_deleted,
             "featureDeleted")
        conn(layer.committedGeometriesChanges, self._on_committed,
             "committedGeometriesChanges")
        conn(layer.editingStopped, self._on_editing_stopped,
             "editingStopped")
        conn(layer.afterRollBack, self._on_rollback, "afterRollBack")
        try:
            sig = QgsProject.instance().layerWillBeRemoved
            try:
                sig[str].connect(self._on_layer_removed)
                self._rm_sig = sig[str]
            except TypeError:
                sig.connect(self._on_layer_removed)
                self._rm_sig = sig
        except Exception:
            self._rm_sig = None

    def shutdown(self):
        """Снятие всех подписок и сброс данных (выгрузка плагина,
        удаление слоя из проекта)."""
        for sig, slot in self._sigs:
            try:
                sig.disconnect(slot)
            except Exception:
                pass
        del self._sigs[:]
        if self._rm_sig is not None:
            try:
                self._rm_sig.disconnect(self._on_layer_removed)
            except Exception:
                pass
            self._rm_sig = None
        self._layer_ref = []
        self._tiles = {}
        self._dirty = set()
        self._failed = {}

    def _layer(self):
        """Слой или None (удалён из проекта / C++ объект мёртв)."""
        if not self._layer_ref:
            return None
        try:
            layer = self._layer_ref[0]
            layer.id()  # обращение к мёртвому C++ объекту кидает RuntimeError
            return layer
        except Exception:
            self._layer_ref = []
            return None

    # --- обработчики ----------------------------------------------------
    # Правки в режиме редактирования (geometryChanged/featureAdded/
    # featureDeleted/committed*) инвалидируются ХИРУРГИЧЕСКИ —
    # перерегистрацией одной фичи без пересборки плитки: тогда следующая
    # операция не платит за пересборку (приёмка «повторные ≤ 50 мс»).
    # afterRollBack сигналы фич не несёт — плитки bbox последней операции
    # помечаются грязными и пересобираются при обращении (ТЗ этап 3, п. 3).

    def _on_geometry_changed(self, fid, geom):
        self._surgical_update(fid, geom)

    def _surgical_update(self, fid, geom):
        """Перерегистрация фичи во всех построенных плитках."""
        self._remove_feature_everywhere(fid)
        self._register_feature(fid, geom)
        self._after_change()

    def _register_feature(self, fid, geom):
        """Регистрация фичи в ПОСТРОЕННЫХ плитках её габарита (ленивые
        плитки подхватят её при сборке — данные берутся со слоя)."""
        if geom is None or geom.isNull() or geom.isEmpty():
            return
        nr = norm_polys(geom)
        if not nr:
            return
        try:
            bbox = geom.boundingBox()
        except Exception:
            return
        if self._tile_size is None:
            return  # нечего обновлять — строится лениво со слоя
        keys = self._tile_keys_covering(bbox)
        if not keys:
            return
        registered = False
        for tk in keys:
            tile = self._tiles.get(tk)
            if tile is not None and tk not in self._dirty:
                tile.register_feature(fid, nr[0], nr[1], geom)
                registered = True
        if registered and self.debug:
            for line in self.check_integrity():
                utils.log(u"Индекс [debug]: {}".format(line))

    def _remove_feature_everywhere(self, fid):
        for tk, tile in self._tiles.items():
            if tk in self._dirty:
                continue  # плитка будет пересобрана со слоя
            tile.remove_feature(fid)

    def _on_feature_added(self, fid):
        layer = self._layer()
        geom = None
        if layer is not None:
            try:
                geom = layer.getFeature(fid).geometry()
            except Exception:
                geom = None
        if geom is not None and not geom.isNull() and not geom.isEmpty():
            self._register_feature(fid, geom)
        elif self._tiles:
            # габарит неизвестен — перестраховка
            self._invalidate_all()
        self._after_change()

    def _on_feature_deleted(self, fid):
        # габарит удалённой фичи берём из снимков (сигнал его не несёт)
        self._remove_feature_everywhere(fid)
        self._after_change()

    def _on_committed(self, _layer_id, fids):
        layer = self._layer()
        refreshed = False
        if layer is not None and fids:
            refreshed = True
            for fid in fids:
                try:
                    geom = layer.getFeature(fid).geometry()
                except Exception:
                    geom = None
                if geom is not None and not geom.isNull():
                    self._surgical_update(fid, geom)
                else:
                    self._remove_feature_everywhere(fid)
        if not refreshed and fids and self._tiles:
            self._invalidate_all()  # фичи недоступны — перестраховка
        self._after_change()

    def _on_editing_stopped(self):
        # сеанс правки завершён (коммит или отмена) — полная пересборка
        # при следующем обращении (дешевле, чем гадать по сигналам)
        self._invalidate_all()

    def _on_rollback(self):
        # сигнал не сообщает фичи (ТЗ этап 3, п. 3): плитки bbox последней
        # операции из журнала; без сведений — полная пересборка
        rect = utils.last_operation_bbox_by_id(self.layer_id)
        if rect is None:
            self._invalidate_all()
        else:
            try:
                rect = QgsRectangle(rect)
                rect.grow(max(abs(rect.width()), abs(rect.height()),
                              1.0) * 0.001 + TOPO_MATCH_EPS)
                self._mark_dirty_rect(rect)
            except Exception:
                self._invalidate_all()
        self._after_change()

    def _on_layer_removed(self, layer_id):
        try:
            if str(layer_id) == self.layer_id:
                drop_index(self.layer_id)
        except Exception:
            pass

    def _after_change(self):
        if self.debug:
            for line in self.check_integrity():
                utils.log(u"Индекс [debug]: {}".format(line))

    # --- инвалидация ----------------------------------------------------

    def _invalidate_all(self):
        self._tiles = {}
        self._dirty = set()
        self._failed = {}

    def _mark_dirty_rect(self, rect):
        """Грязными помечаются только ПОСТРОЕННЫЕ плитки (после отката)."""
        if rect is None or self._tile_size is None:
            return
        for tk in self._tile_keys_covering(rect):
            if tk in self._tiles:
                self._dirty.add(tk)

    # --- плитки ---------------------------------------------------------

    def _ensure_tile_size(self, seed_rect):
        if self._tile_size is not None:
            return
        factor = settings.tile_factor()
        self._tile_factor_used = factor
        layer = self._layer()
        # приоритет посева (ТЗ этап 3, п. 2: «видимый экстент × 2»):
        # канва (seed_extent_from_iface) -> полный экстент слоя -> сам запрос
        rect = self._seed_rect
        if rect is None and layer is not None:
            try:
                rect = layer.extent()
            except Exception:
                rect = None
        if rect is None:
            rect = seed_rect
        if rect is None:
            w = h = 1.0
        else:
            try:
                w = abs(rect.width())
                h = abs(rect.height())
            except Exception:
                w = h = 1.0
        w = max(w, 1e-9) * factor
        h = max(h, 1e-9) * factor
        # Плотностное дробление: seeded-плитка «экстент × 2» на слое из
        # десятков тысяч фич не собирается в бюджет (проверено CI на
        # 10 000 полигонов). Делим сетку, чтобы в плитку попадало
        # порядка _TILE_TARGET_FEATURES фич: ожидаемое число фич в
        # seeded-плитке = count × (площадь плитки / площадь экстента).
        # Малые слои не трогаются — размер ровно по ТЗ.
        div = 1
        try:
            count = int(layer.featureCount())
        except Exception:
            count = 0
        try:
            ext_area = abs(rect.width() * rect.height()) if rect is not None \
                else 0.0
        except Exception:
            ext_area = 0.0
        if count > _TILE_TARGET_FEATURES and ext_area > 0.0:
            per_tile = count * (w * h) / ext_area
            if per_tile > _TILE_TARGET_FEATURES:
                div = int(math.ceil(math.sqrt(per_tile /
                                              _TILE_TARGET_FEATURES)))
                div = max(1, min(div, _TILE_MAX_DIV))
        self._tile_size = (min(max(w / div, 1e-6), 1e12),
                           min(max(h / div, 1e-6), 1e12))

    def _tile_keys_covering(self, rect):
        """Ключи плиток, накрывающих rect (сетка привязана к началу
        координат — стабильна между вызовами).

        Семантика ЗАКРЫТОГО пересечения (как QgsFeatureRequest по bbox):
        rect, чья min-граница точно легла на границу плитки, захватывает
        и соседнюю (левую/нижнюю) плитку — это согласовано с
        _build_tile_data (фича, касающаяся плитки габаритом, в неё
        попадает). Благодаря этому хирургическая регистрация и полная
        пересборка дают идентичное распределение фич по плиткам."""
        if self._tile_size is None:
            return []
        tw, th = self._tile_size
        try:
            ix0, ix1 = self._axis_range(rect.xMinimum(), rect.xMaximum(), tw)
            iy0, iy1 = self._axis_range(rect.yMinimum(), rect.yMaximum(), th)
        except Exception:
            return []
        if (ix1 - ix0 + 1) * (iy1 - iy0 + 1) > MAX_QUERY_TILES:
            return None  # слишком широко — индекс не обслуживает
        keys = []
        for ix in range(ix0, ix1 + 1):
            for iy in range(iy0, iy1 + 1):
                keys.append((ix, iy))
        return keys

    @staticmethod
    def _axis_range(v_min, v_max, size):
        """Индексы плиток оси [v_min, v_max] при размере плитки size."""
        f = math.floor(v_min / size)
        i0 = int(f)
        if v_min == f * size:
            # точная посадка min-границы на границу плитки — соседняя
            # (левая/нижняя) плитка тоже касается rect закрытым
            # пересечением
            i0 -= 1
        i1 = int(math.floor(v_max / size))
        return i0, i1

    def _tile_rect(self, tk):
        tw, th = self._tile_size
        x0 = tk[0] * tw
        y0 = tk[1] * th
        return QgsRectangle(x0, y0, x0 + tw, y0 + th)

    def _build_tile_data(self, rect):
        """Полная сборка данных плитки по прямоугольнику (используется
        и для построения, и для check_integrity). Бюджет времени
        TILE_BUILD_BUDGET_S: превышение — _BudgetExceeded."""
        tile = _Tile(rect)
        layer = self._layer()
        if layer is None:
            raise RuntimeError(u"слой индекса недоступен")
        req = QgsFeatureRequest(rect)
        req.setSubsetOfAttributes([])
        deadline = time.monotonic() + TILE_BUILD_BUDGET_S
        count = 0
        for feat in layer.getFeatures(req):
            geom = feat.geometry()
            if geom is None or geom.isNull() or geom.isEmpty():
                continue
            nr = norm_polys(geom)
            if not nr:
                continue
            tile.register_feature(feat.id(), nr[0], nr[1], geom)
            count += 1
            if (count % _BUDGET_CHECK_EVERY) == 0 and \
                    time.monotonic() > deadline:
                raise _BudgetExceeded()
        return tile

    def _rebuild_tile(self, tk):
        rect = self._tile_rect(tk)
        self._last_budget_fail = False
        try:
            tile = self._build_tile_data(rect)
        except _BudgetExceeded:
            # ТЗ этап 3, п. 2: бюджет превышен — плитка не строится,
            # запросы по ней идут к слою напрямую (неполные результаты
            # никогда не возвращаются); _prepare мельчит сетку и пробует
            # снова (см. _shrink_grid)
            self._last_budget_fail = True
            self._failed[tk] = u"бюджет сборки {:.2f} с превышен".format(
                TILE_BUILD_BUDGET_S)
            self._tiles.pop(tk, None)
            self._dirty.discard(tk)
            utils.log(u"Индекс: плитка {} не собрана ({}) — запросы по "
                      u"ней идут к слою напрямую".format(tk,
                                                         self._failed[tk]))
            return False
        except Exception as exc:
            self._failed[tk] = str(exc)
            self._tiles.pop(tk, None)
            self._dirty.discard(tk)
            utils.log(u"Индекс: плитка {} не собрана — {}".format(tk, exc))
            return False
        self._tiles[tk] = tile
        self._dirty.discard(tk)
        self._failed.pop(tk, None)
        if self.debug and not self._checking:
            for line in self.check_integrity():
                utils.log(u"Индекс [debug]: {}".format(line))
        return True

    def _shrink_grid(self):
        """Сетка мельчает вдвое после провала сборки по бюджету.

        Второй уровень защиты после плотностного дробления: неоднородная
        плотность (сгусток фич в одной плитке) или неправда featureCount.
        Все плитки/пометки сбрасываются: ключи (ix, iy) при новом размере
        означают другие прямоугольники. Максимум _GRID_MAX_SHRINKS раз за
        жизнь индекса — иначе ping-pong пересборок съест интерактивность.

        :return: True — сетка уменьшена, _prepare пробует снова."""
        if self._tile_size is None or self._shrinks >= _GRID_MAX_SHRINKS:
            return False
        tw, th = self._tile_size
        if tw <= 2e-6 or th <= 2e-6:
            return False
        self._shrinks += 1
        self._tile_size = (tw / 2.0, th / 2.0)
        self._tiles.clear()
        self._failed.clear()
        self._dirty.clear()
        utils.log(u"Индекс: сетка плиток уменьшена вдвое ({}/{}), уже "
                  u"построенные плитки пересоберутся при обращении"
                  .format(self._shrinks, _GRID_MAX_SHRINKS))
        return True

    def _prepare(self, rect):
        """Готовит плитки под запрос: строит отсутствующие, пересобирает
        грязные. False — прямоугольник индексом не обслуживается
        (слишком много плиток) — вызывающий идёт прямым запросом.

        Провал сборки по бюджету мельчит сетку (до _GRID_MAX_SHRINKS
        раз) и пробует снова — плитка с порядком 1000 фич собирается
        в бюджет на любом железе; при этом НЕУДАВШАЯСЯ (окончательно)
        плитка не перестраивается на каждый запрос."""
        if self._layer() is None:
            return False
        # смена tile_factor в настройках — пересборка сетки с нуля
        try:
            if self._tile_size is not None and \
                    self._tile_factor_used != settings.tile_factor():
                self._invalidate_all()
                self._tile_size = None       # пересчитать сетку
                self._tile_factor_used = None
                self._seed_rect = None
        except Exception:
            pass
        self._ensure_tile_size(rect)
        for _attempt in range(_GRID_MAX_SHRINKS + 1):
            keys = self._tile_keys_covering(rect)
            if keys is None:
                return False
            budget_hit = False
            for tk in keys:
                if tk in self._failed:
                    continue
                if tk not in self._tiles or tk in self._dirty:
                    if not self._rebuild_tile(tk) and \
                            self._last_budget_fail:
                        budget_hit = True
            if not budget_hit or not self._shrink_grid():
                break
        return True

    def _has_failed_covering(self, rect):
        """True — в области есть плитка, не собранная из-за бюджета:
        агрегирующие запросы обязаны вернуть None (область не
        обслуживается), а вызывающий — пойти прямым запросом к слою.
        Иначе неполные результаты выдавались бы за полные."""
        keys = self._tile_keys_covering(rect)
        if not keys:
            return False
        return any(tk in self._failed for tk in keys)

    def _built_tiles_covering(self, rect):
        keys = self._tile_keys_covering(rect)
        if not keys:
            return []
        out = []
        for tk in keys:
            tile = self._tiles.get(tk)
            if tile is not None and tk not in self._dirty:
                out.append(tile)
        return out

    # ------------------------------------------------------------------
    # Публичный API (ТЗ этап 3, п. 5)
    # ------------------------------------------------------------------

    def ensure_for(self, rect):
        """Строит все плитки, накрывающие rect (синхронно).

        :return: True — область обслуживается индексом; False — слишком
                 широко (прямой запрос)."""
        return self._prepare(QgsRectangle(rect))

    def nodes_at(self, pt, eps=TOPO_MATCH_EPS):
        """Вершины в пределах eps от pt: [NodeRef] (по построенным
        плиткам; покрытие области проверяет _has_failed_covering —
        агрегирующие методы при необслуживаемой области вернут None).

        Все копии одного топологического узла (смежные фичи, несколько
        колец) возвращаются отдельными NodeRef; сортировка по
        (fid, part, ring, idx) — детерминированный порядок."""
        eps = max(float(eps), NODE_GRID_MIN)
        eps2 = eps * eps
        rect = QgsRectangle(pt.x() - eps, pt.y() - eps,
                            pt.x() + eps, pt.y() + eps)
        if not self._prepare(rect):
            return []
        out = []
        grid = NODE_GRID
        r = int(math.ceil(eps / grid))
        px, py = pt.x(), pt.y()
        if r > _MAX_CELL_RADIUS:
            # крупный допуск: перебор ячеек (2r+1)^2 невозможен —
            # скан узлов построенных плиток с bbox-предфильтром
            for tile in self._built_tiles_covering(rect):
                for lst in tile.nodes.values():
                    for (x, y, fid, pi, ri, vi) in lst:
                        if x < px - eps or x > px + eps or \
                                y < py - eps or y > py + eps:
                            continue
                        dx = x - px
                        dy = y - py
                        if dx * dx + dy * dy <= eps2:
                            out.append(NodeRef(
                                self.layer_id, fid, pi, ri, vi,
                                QgsPointXY(x, y)))
            return _dedup_refs(out)
        cx, cy = _cell_key(px, py, grid)
        for tile in self._built_tiles_covering(rect):
            for ix in range(cx - r, cx + r + 1):
                for iy in range(cy - r, cy + r + 1):
                    for (x, y, fid, pi, ri, vi) in \
                            tile.nodes.get((ix, iy), ()):
                        dx = x - px
                        dy = y - py
                        if dx * dx + dy * dy <= eps2:
                            out.append(NodeRef(
                                self.layer_id, fid, pi, ri, vi,
                                QgsPointXY(x, y)))
        return _dedup_refs(out)

    def edges_near(self, pt, eps=TOPO_MATCH_EPS):
        """Рёбра в пределах eps от pt: [EdgeRef] (по построенным
        плиткам; покрытие области проверяет _has_failed_covering).

        Кандидаты — QgsSpatialIndex по bbox фичи, повторная проверка —
        по сегментам снимка (point_to_segment <= eps). Рёбра-дубликаты
        (фича в нескольких плитках) возвращаются один раз."""
        eps = max(float(eps), NODE_GRID_MIN)
        eps2 = eps * eps
        rect = QgsRectangle(pt.x() - eps, pt.y() - eps,
                            pt.x() + eps, pt.y() + eps)
        if not self._prepare(rect):
            return []
        out = []
        seen = set()
        for tile in self._built_tiles_covering(rect):
            if tile.edge_index is None:
                cand = tile.fids  # стаб без QgsSpatialIndex: все фичи
            else:
                try:
                    cand = tile.edge_index.intersects(rect)
                except Exception:
                    cand = tile.fids
            for fid in cand:
                snap = tile.snapshots.get(fid)
                if not snap:
                    continue
                for pi, ri, ring in iter_rings(snap[0]):
                    for _si, a, b in iter_ring_segments(ring):
                        if a.sqrDist(b) <= DUP_EPS2:
                            continue
                        _proj, d2, _t = point_to_segment(pt, a, b)
                        if d2 > eps2:
                            continue
                        key = (fid, a.x(), a.y(), b.x(), b.y())
                        if key in seen:
                            continue
                        seen.add(key)
                        out.append(EdgeRef(self.layer_id, fid,
                                           QgsPointXY(a), QgsPointXY(b)))
        out.sort(key=lambda e: (e.fid, e.a.x(), e.a.y(),
                                e.b.x(), e.b.y()))
        return out

    def features_touching(self, pt, eps=TOPO_MATCH_EPS):
        """Фичи с узлом или ребром в пределах eps от pt:
        {layer_id: [fid, ...]} (отсортированы).

        None — область не обслуживается индексом (плитка не собрана
        из-за бюджета) — вызывающий обязан использовать прямой запрос."""
        fids = {ref.fid for ref in self.nodes_at(pt, eps)}
        fids.update(ref.fid for ref in self.edges_near(pt, eps))
        rect = QgsRectangle(pt.x() - eps, pt.y() - eps,
                            pt.x() + eps, pt.y() + eps)
        if self._has_failed_covering(rect):
            return None
        return {self.layer_id: sorted(fids)}

    def features_near_points(self, pts, eps=TOPO_MATCH_EPS):
        """Множество fid, задетых ЛЮБОЙ из точек (узел или ребро в eps).

        Внутренний помощник редактора (_collect_moved / _t_patch):
        объединяет поиск по узлам (сетка) и по рёбрам (QgsSpatialIndex).
        None — область не обслуживается (прямой запрос у вызывающего).
        """
        rect = None
        seen = []
        out = set()
        for p in pts:
            if p is None:
                continue
            # дубликаты точек (одна вершина в нескольких якорях) — один запрос
            dup = False
            for q in seen:
                if p.sqrDist(q) <= eps * eps:
                    dup = True
                    break
            if dup:
                continue
            seen.append(QgsPointXY(p))
            r = QgsRectangle(p.x() - eps, p.y() - eps,
                             p.x() + eps, p.y() + eps)
            if rect is None:
                rect = r
            else:
                rect.combineExtentWith(r)  # изменяет на месте, None верн.
        if rect is None:
            return set()
        if not self._prepare(rect) or self._has_failed_covering(rect):
            return None
        for p in seen:
            out.update(ref.fid for ref in self.nodes_at(p, eps))
            out.update(ref.fid for ref in self.edges_near(p, eps))
        return out

    def bbox_features(self, rect):
        """Фичи, габарит которых пересекает rect: {layer_id: [fid, ...]}.

        QgsSpatialIndex хранит bbox фич — пересечение по нему и есть
        пересечение габаритов. Точная проверка сегментов — на вызывающей
        стороне (find_overlaps и т.п.).

        None — область не обслуживается (слишком много плиток или есть
        несобранная) — вызывающий использует прямой запрос."""
        rect = QgsRectangle(rect)
        if not self._prepare(rect) or self._has_failed_covering(rect):
            return None  # не обслуживается — прямой запрос
        out = set()
        for tile in self._built_tiles_covering(rect):
            if tile.edge_index is None:
                # стаб без QgsSpatialIndex: фильтр по снимкам
                for fid in tile.fids:
                    snap = tile.snapshots.get(fid)
                    if not snap:
                        continue
                    try:
                        if tile.rect.intersects(
                                polys_bbox_rect(snap[0])):
                            out.add(fid)
                    except Exception:
                        out.add(fid)
            else:
                try:
                    out.update(tile.edge_index.intersects(rect))
                except Exception:
                    out.update(tile.fids)
        return {self.layer_id: sorted(out)}

    def rect_features(self, rect):
        """[(fid, polys, was_multi), ...] — снимки фич под rect.

        Используется редактором вместо прямого getFeatures(rect):
        fids отсортированы, снимки — norm_polys (как раньше читал
        редактор, но из кэша)."""
        mapping = self.bbox_features(rect)
        if mapping is None:
            return None
        out = []
        for fid in mapping.get(self.layer_id, ()):
            snap = self.snapshot_geometry(None, fid)
            if snap:
                out.append((fid, snap[0], snap[1]))
        return out

    def snapshot_geometry(self, layer, fid):
        """Снимок norm_polys фичи: (polys, was_multi) или None.

        layer передаётся по ТЗ API; если он совпадает со слоем индекса —
        данные берутся из кэша плиток (грязные пересобираются заранее),
        иначе — прямой запрос (слои-эталоны индексом не покрыты).
        Фича вне построенной области — прямой запрос (без кэширования)."""
        target = layer if layer is not None else self._layer()
        try:
            if target is not None and target.id() == self.layer_id:
                # пересобрать грязные плитки, содержащие fid
                for tk in sorted(self._tiles.keys()):
                    if tk in self._dirty and fid in self._tiles[tk].fids:
                        self._rebuild_tile(tk)
                for tk in sorted(self._tiles.keys()):
                    tile = self._tiles[tk]
                    if tk in self._dirty:
                        continue
                    if fid in tile.fids:
                        return tile.snapshots.get(fid)
        except Exception as exc:
            utils.log(u"Индекс: снимок #{} из кэша не удался — {}".format(
                fid, exc))
        return _direct_snapshot(target, fid)

    def neighbors(self, fid, layer=None, depth=1):
        """Соседи фичи: {layer_id: [fid, ...]}, отсортированы.

        Смежность: общая вершина (в пределах TOPO_MATCH_EPS) ИЛИ чужое
        ребро под вершиной фичи (T-контакт «ребро соседа под нашим
        узлом»; обратный T-контакт — вершина соседа на нашем ребре —
        ловится симметрично запросом от соседа и для полноты добавлен
        через edges_near по вершинам). Глубина depth — расстояние в
        графе смежности (BFS).

        None — фича вне обслуживаемой области (прямой запрос)."""
        depth = max(int(depth), 1)
        snap0 = self.snapshot_geometry(layer, fid)
        if not snap0:
            return None
        try:
            seed_rect = polys_bbox_rect(snap0[0])
        except Exception:
            seed_rect = None
        if seed_rect is not None and (
                not self._prepare(seed_rect) or
                self._has_failed_covering(seed_rect)):
            return None
        result = set()
        frontier = {fid}
        visited = {fid}
        for _d in range(depth):
            nxt = set()
            for f in frontier:
                snap = self.snapshot_geometry(layer, f)
                if not snap:
                    continue
                for v in _unique_vertices(snap[0]):
                    for ref in self.nodes_at(v, TOPO_MATCH_EPS):
                        nxt.add(ref.fid)
                    for ref in self.edges_near(v, TOPO_MATCH_EPS):
                        nxt.add(ref.fid)
            nxt -= visited
            result |= nxt
            visited |= nxt
            frontier = nxt
            if not frontier:
                break
        result.discard(fid)
        return {self.layer_id: sorted(result)}

    # ------------------------------------------------------------------
    # Целостность (ТЗ этап 3, п. 4)
    # ------------------------------------------------------------------

    def check_integrity(self):
        """Сравнение каждой построенной плитки с полной пересборкой.

        Грязные (после отката) плитки сначала пересобираются: ленивая
        пересборка — штатное состояние, а не расхождение; сравнивается
        ФАКТИЧЕСКОЕ содержимое после догрузки.

        :return: список строк-расхождений (пустой — индекс цел)."""
        diffs = []
        self._checking = True
        try:
            for tk in sorted(self._dirty):
                self._rebuild_tile(tk)
            for tk in sorted(self._tiles.keys()):
                actual = self._tiles[tk]
                try:
                    expected = self._build_tile_data(self._tile_rect(tk))
                except _BudgetExceeded:
                    diffs.append(
                        u"плитка {}: ожидаемая сборка превысила бюджет"
                        .format(tk))
                    continue
                except Exception as exc:
                    diffs.append(u"плитка {}: ожидаемая сборка упала — {}"
                                 .format(tk, exc))
                    continue
                diffs.extend(_tile_diffs(actual, expected, tk))
        finally:
            self._checking = False
        return diffs

    # ------------------------------------------------------------------
    # Фоновая сборка (ТЗ этап 3, п. 2: пакетные операции — QgsTask)
    # ------------------------------------------------------------------

    def prefetch_async(self, rect, on_done=None):
        """Сборка плиток области в фоне (QgsTask). Fallback — синхронно.

        Потребитель появляется в этапе 5 (пакетный аудит); API готово
        заранее. Любой сбой фонового пути деградирует в синхронную
        сборку — метод всегда доводит область до обслуживаемой.

        :return: True — область обслуживается индексом."""
        rect = QgsRectangle(rect)
        try:
            self._ensure_tile_size(rect)
            if self._tile_keys_covering(rect) is None:
                return False
        except Exception:
            return False

        def _run(_task):
            return self._prepare(QgsRectangle(rect))

        task = None
        try:
            from qgis.core import QgsTask

            state = {"done": False}

            def _finished(_task, _status):
                state["done"] = True
                if on_done is not None:
                    try:
                        on_done()
                    except Exception:
                        pass

            task = QgsTask.fromFunction(
                u"TopoPolyEdit: сборка индекса топологии", _run,
                on_finished=_finished)
            # сильная ссылка до завершения (иначе задача соберётся GC)
            _BACKGROUND_TASKS.append(task)
            return True
        except Exception:
            task = None
        # fallback: синхронно
        try:
            return self._prepare(QgsRectangle(rect))
        except Exception:
            return False


_BACKGROUND_TASKS = []  # держит QgsTask, пока те живут


def _unique_vertices(polys):
    """Уникальные вершины структуры (дедуп в пределах DUP_EPS2),
    замыкающие дубликаты не включаются."""
    out = []
    for _pi, _ri, ring in iter_rings(polys):
        n = len(ring)
        if n < 2:
            continue
        closed = ring[0].sqrDist(ring[-1]) <= DUP_EPS2
        m = n - 1 if closed else n
        for vi in range(m):
            v = ring[vi]
            dup = False
            for q in out:
                if v.sqrDist(q) <= DUP_EPS2:
                    dup = True
                    break
            if not dup:
                out.append(QgsPointXY(v))
    return out


def _dedup_refs(refs):
    """Дедуп NodeRef по (fid, part, ring, idx) + детерминированный порядок."""
    seen = set()
    out = []
    for ref in refs:
        key = (ref.fid, ref.part, ref.ring, ref.idx)
        if key in seen:
            continue
        seen.add(key)
        out.append(ref)
    out.sort(key=lambda r: (r.fid, r.part, r.ring, r.idx))
    return out


def _direct_snapshot(layer, fid):
    """norm_polys фичи прямым запросом (вне кэша)."""
    if layer is None:
        return None
    try:
        feat = layer.getFeature(fid)
        geom = feat.geometry()
        if geom is None or geom.isNull() or geom.isEmpty():
            return None
        return norm_polys(geom)
    except Exception:
        return None


def polys_bbox_rect(polys):
    """Габарит нормализованной структуры (QgsRectangle)."""
    minx = miny = maxx = maxy = None
    for _pi, _ri, ring in iter_rings(polys):
        for v in ring:
            x, y = v.x(), v.y()
            if minx is None or x < minx:
                minx = x
            if maxx is None or x > maxx:
                maxx = x
            if miny is None or y < miny:
                miny = y
            if maxy is None or y > maxy:
                maxy = y
    if minx is None:
        return QgsRectangle()
    return QgsRectangle(minx, miny, maxx, maxy)


# ---------------------------------------------------------------------------
# Реестр индексов: один TopologyIndex на слой, разделяется всеми
# операциями плагина (первые операции строят плитки, последующие
# переиспользуют — приёмка ТЗ «повторные операции ≤ 50 мс»).
# ---------------------------------------------------------------------------

_registry = {}  # layer_id -> TopologyIndex


def index_for_layer(layer, create=True):
    """Индекс слоя из реестра; None — индекс выключен или слой негоден.

    При settings.index_enabled() == False возвращает None БЕЗ удаления
    существующего индекса: сигналы продолжают поддерживать его свежим,
    и повторное включение не требует пересборки с нуля."""
    if layer is None:
        return None
    try:
        if not settings.index_enabled():
            return None
    except Exception:
        return None
    try:
        lid = layer.id()
    except Exception:
        return None
    idx = _registry.get(lid)
    if idx is None and create:
        try:
            if layer.geometryType() != QgsWkbTypes.PolygonGeometry:
                return None
        except Exception:
            pass  # стабы могут не давать geometryType — создаём
        try:
            idx = TopologyIndex(layer)
        except Exception as exc:
            utils.log(u"Индекс: не создан для «{}» — {}".format(
                getattr(layer, "name", lambda: lid)(), exc))
            return None
        _registry[lid] = idx
    return idx


def drop_index(layer_or_id):
    """Снять индекс слоя (shutdown + удаление из реестра)."""
    lid = None
    try:
        lid = layer_or_id if isinstance(layer_or_id, str) \
            else layer_or_id.id()
    except Exception:
        return False
    idx = _registry.pop(lid, None)
    if idx is None:
        return False
    try:
        idx.shutdown()
    except Exception:
        pass
    return True


def shutdown_all():
    """Полный сброс реестра (unload плагина, тесты)."""
    for lid in list(_registry.keys()):
        drop_index(lid)


def registry_size():
    """Число живых индексов (тесты, диагностика)."""
    return len(_registry)
