# -*- coding: utf-8 -*-
"""Диалог настроек прилипания TopoPolyEdit.

Позволяет:
  * выбрать слои-эталоны: галочка напротив каждого векторного слоя
    проекта — участвует ли он в прилипании (узлы и рёбра);
  * задать допуск прилипания к рёбрам и к узлам («строгий» снэп)
    и его единицы: пиксели экрана или единицы карты.

Настройки сохраняются в QgsSettings и применяются СРАЗУ всем
инструментам плагина: движок прилипания читает их при каждом поиске
привязки (см. settings.py). Видимость слоя в панели слоёв остаётся
отдельным условием: скрытый слой не участвует в прилипании, даже если
здесь отмечен.
"""

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QGridLayout, QGroupBox, QLabel,
    QListWidget, QListWidgetItem, QPushButton, QComboBox, QDoubleSpinBox,
    QSpinBox, QDialogButtonBox, QCheckBox, QRadioButton, QWidget,
)
from qgis.core import QgsProject, QgsWkbTypes, QgsVectorLayer

from . import settings

_UNITS_LABELS = [
    (u"пикселей экрана", settings.UNITS_PX),
    (u"единиц карты", settings.UNITS_MAP),
]

_OVERLAP_LABELS = [
    (u"Спрашивать при отпускании кнопки мыши", settings.OVERLAP_ASK),
    (u"Всегда обрезать перемещаемые полигоны по соседям",
     settings.OVERLAP_CLIP),
    (u"Не обрезать (оставлять перехлёст)", settings.OVERLAP_KEEP),
]


class SettingsDialog(QDialog):
    """Диалог «Настройки прилипания» (немодальный/модальный — на выбор)."""

    def __init__(self, iface, parent=None):
        super(SettingsDialog, self).__init__(parent)
        self.iface = iface
        self.setWindowTitle(
            u"Настройки прилипания — Топологическое редактирование")
        self.setMinimumWidth(440)

        cfg = settings.load_settings()
        lay = QVBoxLayout(self)

        # --------------------------------------------------------------
        # Слои прилипания
        # --------------------------------------------------------------
        grp_layers = QGroupBox(u"Слои прилипания", self)
        v = QVBoxLayout(grp_layers)
        v.addWidget(QLabel(
            u"Отмеченные слои участвуют в прилипании (узлы и рёбра).\n"
            u"Скрытый в панели слоёв слой не участвует независимо от этой "
            u"галочки.\nРедактируемый слой тоже можно исключить — тогда "
            u"прилипания к самому себе не будет.", grp_layers))
        self.lst_layers = QListWidget(grp_layers)
        self.lst_layers.setToolTip(
            u"Галочка = прилипать к узлам и рёбрам этого слоя.\n"
            u"Настройка сохраняется по идентификатору слоя и остаётся "
            u"даже после перезапуска QGIS.")
        self._fill_layers(set(cfg["disabled"]))
        v.addWidget(self.lst_layers)
        row = QHBoxLayout()
        btn_all = QPushButton(u"Отметить все", grp_layers)
        btn_none = QPushButton(u"Снять все", grp_layers)
        btn_refresh = QPushButton(u"Обновить", grp_layers)
        btn_refresh.setToolTip(
            u"Перечитать список слоёв проекта: свежедобавленные слои "
            u"появятся в списке, снятые галочки сохранятся.")
        btn_all.clicked.connect(self._check_all)
        btn_none.clicked.connect(self._uncheck_all)
        btn_refresh.clicked.connect(self._refresh_layers)
        row.addWidget(btn_all)
        row.addWidget(btn_none)
        row.addWidget(btn_refresh)
        row.addStretch(1)
        v.addLayout(row)
        lay.addWidget(grp_layers)

        # --------------------------------------------------------------
        # Допуск прилипания
        # --------------------------------------------------------------
        grp_tol = QGroupBox(u"Допуск прилипания", self)
        grid = QGridLayout(grp_tol)

        grid.addWidget(QLabel(u"Единицы допуска:", grp_tol), 0, 0)
        self.cmb_units = QComboBox(grp_tol)
        for label, data in _UNITS_LABELS:
            self.cmb_units.addItem(label, data)
        self.cmb_units.setCurrentIndex(
            1 if cfg["units"] == settings.UNITS_MAP else 0)
        self.cmb_units.currentIndexChanged.connect(self._update_suffixes)
        grid.addWidget(self.cmb_units, 0, 1)

        grid.addWidget(QLabel(u"Рёбра:", grp_tol), 1, 0)
        self.spn_edge = QDoubleSpinBox(grp_tol)
        self._setup_spin(self.spn_edge, cfg["edge_tol"])
        grid.addWidget(self.spn_edge, 1, 1)

        grid.addWidget(QLabel(u"Узлы (строгое прилипание):", grp_tol), 2, 0)
        self.spn_vertex = QDoubleSpinBox(grp_tol)
        self._setup_spin(self.spn_vertex, cfg["vertex_tol"])
        grid.addWidget(self.spn_vertex, 2, 1)

        hint = QLabel(u"Узлы — радиус прилипания к вершинам эталонных "
                      u"слоёв; он применяется и к захвату узла при "
                      u"перетаскивании. Рекомендуется держать его больше "
                      u"радиуса рёбер — в узел легче попасть.", grp_tol)
        hint.setWordWrap(True)
        grid.addWidget(hint, 3, 0, 1, 2)

        self.lbl_warn = QLabel(
            u"Внимание: радиус узлов меньше радиуса рёбер — узлы будут "
            u"захватываться меньшим радиусом, чем рёбра.", grp_tol)
        self.lbl_warn.setWordWrap(True)
        self.lbl_warn.setStyleSheet("color: #b8860b;")
        self.lbl_warn.hide()
        grid.addWidget(self.lbl_warn, 4, 0, 1, 2)
        self.spn_edge.valueChanged.connect(self._update_tol_warning)
        self.spn_vertex.valueChanged.connect(self._update_tol_warning)
        lay.addWidget(grp_tol)

        # --------------------------------------------------------------
        # Подсветка и перехлёсты
        # --------------------------------------------------------------
        grp_vis = QGroupBox(u"Подсветка и перехлёсты", self)
        vis = QVBoxLayout(grp_vis)
        self.chk_hints = QCheckBox(
            u"Подсвечивать вершины слоёв прилипания при перетаскивании",
            grp_vis)
        self.chk_hints.setToolTip(
            u"Бирюзовые квадраты — вершины слоёв-эталонов рядом с курсором;\n"
            u"фиолетовые кружки — все вершины объекта, к которому\n"
            u"происходит прилипание.")
        self.chk_hints.setChecked(bool(cfg.get("show_hints", True)))
        vis.addWidget(self.chk_hints)

        self.chk_slide = QCheckBox(
            u"Скользить по соседнему ребру (T-примыкание концом ребра)",
            grp_vis)
        self.chk_slide.setToolTip(
            u"Конец перемещаемого ребра, лежащий на ребре соседа без узла,\n"
            u"двигается ВДОЛЬ этого ребра вместо параллельного переноса —\n"
            u"примыкание не рвётся (этап 2). По умолчанию включено.")
        self.chk_slide.setChecked(bool(cfg.get(
            "edge_slide", settings.DEFAULT_EDGE_SLIDE)))
        vis.addWidget(self.chk_slide)

        row_ov = QHBoxLayout()
        row_ov.addWidget(QLabel(
            u"Перехлёст с объектами редактируемого слоя:", grp_vis))
        self.cmb_overlap = QComboBox(grp_vis)
        for label, data in _OVERLAP_LABELS:
            self.cmb_overlap.addItem(label, data)
        self._select_overlap(cfg.get("overlap_mode"))
        row_ov.addWidget(self.cmb_overlap, 1)
        vis.addLayout(row_ov)
        ov_hint = QLabel(
            u"Если перемещаемый полигон наезжает на другой объект этого же "
            u"слоя, он показывается красной областью, а при отпускании "
            u"кнопки можно выбрать: обрезать перемещаемые полигоны, "
            u"обрезать соседей, оставить как есть или отменить правку. "
            u"Перекрытия с полигонами других слоёв отсекаются автоматически "
            u"(как раньше).", grp_vis)
        ov_hint.setWordWrap(True)
        vis.addWidget(ov_hint)
        lay.addWidget(grp_vis)

        # --------------------------------------------------------------
        # Индекс топологии (этап 3 ТЗ)
        # --------------------------------------------------------------
        grp_idx = QGroupBox(u"Индекс топологии", self)
        ix = QVBoxLayout(grp_idx)
        self.chk_index = QCheckBox(
            u"Использовать индекс топологии (рекомендуется)", grp_idx)
        self.chk_index.setToolTip(
            u"Индекс строитcя лениво по плиткам: поиск узлов, рёбер и\n"
            u"соседей становится мгновенным, лимит соседей растёт до 5000.\n"
            u"При выключении все запросы идут к слою напрямую (как раньше)\n"
            u"— откат при странном поведении.")
        self.chk_index.setChecked(bool(cfg.get("index_on",
                                               settings.DEFAULT_INDEX_ON)))
        ix.addWidget(self.chk_index)

        row_nb = QHBoxLayout()
        row_nb.addWidget(QLabel(
            u"Лимит соседей того же слоя:", grp_idx))
        self.spn_neighbors = QSpinBox(grp_idx)
        self.spn_neighbors.setRange(settings.MIN_MAX_NEIGHBORS,
                                    settings.MAX_MAX_NEIGHBORS)
        self.spn_neighbors.setSingleStep(500)
        self.spn_neighbors.setValue(int(cfg.get(
            "max_neighbors", settings.DEFAULT_MAX_NEIGHBORS)))
        self.spn_neighbors.setToolTip(
            u"При превышении операция отменяется с явным сообщением\n"
            u"(защита от частичного отсечения). По умолчанию 5000.")
        row_nb.addWidget(self.spn_neighbors, 1)
        ix.addLayout(row_nb)

        row_tf = QHBoxLayout()
        row_tf.addWidget(QLabel(
            u"Размер плитки (× видимый экстент):", grp_idx))
        self.spn_tile = QDoubleSpinBox(grp_idx)
        self.spn_tile.setRange(settings.MIN_TILE_FACTOR,
                               settings.MAX_TILE_FACTOR)
        self.spn_tile.setDecimals(1)
        self.spn_tile.setSingleStep(0.5)
        self.spn_tile.setValue(float(cfg.get("tile_factor",
                                             settings.DEFAULT_TILE_FACTOR)))
        self.spn_tile.setToolTip(
            u"Индекс строится по плиткам размером видимый экстент × это\n"
            u"число (по умолчанию 2). Меньше — быстрее первая сборка,\n"
            u"больше — меньше пересборок при перемещении по карте.")
        row_tf.addWidget(self.spn_tile, 1)
        ix.addLayout(row_tf)

        idx_hint = QLabel(
            u"Первая операция в новой области строит плитку (бюджет 0,4 с;\n"
            u"при превышении запросы по ней идут к слою напрямую),\n"
            u"повторные — мгновенны. Сборка плиток в фоне (QgsTask)\n"
            u"используется пакетными операциями.", grp_idx)
        idx_hint.setWordWrap(True)
        ix.addWidget(idx_hint)
        lay.addWidget(grp_idx)

        # --------------------------------------------------------------
        # Правка нескольких слоёв (этап 4 ТЗ)
        # --------------------------------------------------------------
        grp_multi = QGroupBox(u"Правка нескольких слоёв", self)
        mx = QVBoxLayout(grp_multi)

        self.rb_scope_all = QRadioButton(
            u"Двигать ВСЕ слои в правке одной операцией (рекомендуется)",
            grp_multi)
        self.rb_scope_all.setToolTip(
            u"Перемещение узла/ребра меняет все полигональные слои в\n"
            u"режиме правки, у которых есть этот узел или ребро —\n"
            u"одной атомарной операцией, с общей отменой. Слои между\n"
            u"собой не рассинхронизируются.")
        self.rb_scope_active = QRadioButton(
            u"Только активный слой (поведение прежних версий)", grp_multi)
        self.rb_scope_active.setToolTip(
            u"Операция меняет только активный слой; остальные слои в\n"
            u"правке остаются эталонами (их полигоны не двигаются).")
        scope = settings.edit_scope()
        if scope == settings.EDIT_SCOPE_ACTIVE:
            self.rb_scope_active.setChecked(True)
        else:
            self.rb_scope_all.setChecked(True)
        mx.addWidget(self.rb_scope_all)
        mx.addWidget(self.rb_scope_active)

        mx.addWidget(QLabel(
            u"Слои, которые операции плагина НЕ двигают (правятся "
            u"вручную):", grp_multi))
        self.lst_edit_excl = QListWidget(grp_multi)
        self.lst_edit_excl.setMaximumHeight(96)
        self.lst_edit_excl.setToolTip(
            u"Отмеченный слой исключается из многослойной операции: его\n"
            u"полигоны остаются эталонами, а правятся только вручную.")
        self._fill_edit_exclusions()
        mx.addWidget(self.lst_edit_excl)

        mx.addWidget(QLabel(
            u"Отсечение перехлёстов по слоям ВНЕ правки:", grp_multi))
        self.lst_clip_modes = QListWidget(grp_multi)
        self.lst_clip_modes.setMaximumHeight(96)
        self.lst_clip_modes.setToolTip(
            u"«Отсекать» — перемещаемые полигоны обрезаются по объектам\n"
            u"слоя автоматически (как раньше). «Спрашивать» — вопрос при\n"
            u"перехлёсте. «Не трогать» — перехлёст остаётся.")
        self._fill_clip_modes()
        mx.addWidget(self.lst_clip_modes)
        lay.addWidget(grp_multi)

        # --------------------------------------------------------------
        # Аудит слоя (этап 5 ТЗ): пороги проверок и исправлений
        # --------------------------------------------------------------
        grp_audit = QGroupBox(u"Аудит слоя (пороги проверок)", self)
        ax = QVBoxLayout(grp_audit)

        row_gap = QHBoxLayout()
        row_gap.addWidget(QLabel(
            u"Исправимая щель до (ед. СК²):", grp_audit))
        self.spn_gap_area = QDoubleSpinBox(grp_audit)
        self.spn_gap_area.setDecimals(4)
        self.spn_gap_area.setRange(settings.MIN_GAP_MAX_AREA,
                                   settings.MAX_GAP_MAX_AREA)
        self.spn_gap_area.setSingleStep(0.01)
        self.spn_gap_area.setValue(settings.gap_max_area())
        self.spn_gap_area.setToolTip(
            u"GAP_MAX_AREA: щели с площадью до этого порога исправимы\n"
            u"(«приклеить» к соседу), больше — только показ. По\n"
            u"умолчанию 0,01 (100 см² в метрических СК). До этапа 7 —\n"
            u"единицы СК слоя.")
        row_gap.addWidget(self.spn_gap_area, 1)
        ax.addLayout(row_gap)

        row_near = QHBoxLayout()
        row_near.addWidget(QLabel(
            u"Допуск «почти совпавших» узлов:", grp_audit))
        self.spn_near_tol = QDoubleSpinBox(grp_audit)
        self.spn_near_tol.setDecimals(4)
        self.spn_near_tol.setRange(settings.MIN_NEAR_TOL,
                                   settings.MAX_NEAR_TOL)
        self.spn_near_tol.setSingleStep(0.005)
        self.spn_near_tol.setValue(settings.near_node_tol())
        self.spn_near_tol.setToolTip(
            u"NEAR_NODE_TOL: узлы разных полигонов ближе этого допуска\n"
            u"(но не совпадающие) — находка NEAR_NODE; слив в центроид\n"
            u"только если рёбра короткие или коллинеарные. По умолчанию\n"
            u"0,01 (1 см в метрических СК).")
        row_near.addWidget(self.spn_near_tol, 1)
        ax.addLayout(row_near)

        row_slv = QHBoxLayout()
        row_slv.addWidget(QLabel(u"Порог сливера (площадь/периметр²):",
                                 grp_audit))
        self.spn_sliver = QDoubleSpinBox(grp_audit)
        self.spn_sliver.setDecimals(8)
        self.spn_sliver.setRange(settings.MIN_SLIVER_RATIO,
                                 settings.MAX_SLIVER_RATIO)
        self.spn_sliver.setSingleStep(0.00001)
        self.spn_sliver.setValue(settings.sliver_ratio())
        self.spn_sliver.setToolTip(
            u"SLIVER_RATIO: у вытянутого «лоскута» площадь/периметр²\n"
            u"ниже порога (по умолчанию 0,0001). У квадрата 0,0625.\n"
            u"Исправления нет — только показ.")
        row_slv.addWidget(self.spn_sliver, 1)
        ax.addLayout(row_slv)

        row_pct = QHBoxLayout()
        row_pct.addWidget(QLabel(
            u"Порог изменения площади при исправлении, %:", grp_audit))
        self.spn_fix_pct = QDoubleSpinBox(grp_audit)
        self.spn_fix_pct.setDecimals(4)
        self.spn_fix_pct.setRange(settings.MIN_FIX_AREA_PCT,
                                  settings.MAX_FIX_AREA_PCT)
        self.spn_fix_pct.setSingleStep(0.001)
        self.spn_fix_pct.setValue(settings.fix_area_change_pct())
        self.spn_fix_pct.setToolTip(
            u"Если пачка исправлений меняет суммарную площадь слоя\n"
            u"больше этого процента — пачка отказывается целиком\n"
            u"(по умолчанию 0,001 % — клейка мелких щелей проходит,\n"
            u"крупные ошибки формы требуют ручной работы).")
        row_pct.addWidget(self.spn_fix_pct, 1)
        ax.addLayout(row_pct)

        audit_hint = QLabel(
            u"Проверка всего слоя идёт в фоне (QgsTask) с прогрессом и\n"
            u"отменой; результаты — в панели «Аудит слоя…» с таблицей,\n"
            u"переходом на место и экспортом в точечный слой.", grp_audit)
        audit_hint.setWordWrap(True)
        ax.addWidget(audit_hint)
        lay.addWidget(grp_audit)

        # --------------------------------------------------------------
        # Кнопки
        # --------------------------------------------------------------
        row_btn = QHBoxLayout()
        self.btn_reset = QPushButton(u"По умолчанию", self)
        self.btn_reset.setToolTip(
            u"Вернуть значения по умолчанию (10 px рёбра, 15 px узлы,\n"
            u"прилипать ко всем слоям, подсветка вершин включена,\n"
            u"перехлёст — спрашивать). Затем нажмите OK.")
        self.btn_reset.clicked.connect(self._reset_defaults)
        row_btn.addWidget(self.btn_reset)
        row_btn.addStretch(1)
        self.btn_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Cancel, self)
        self.btn_box.accepted.connect(self.accept)
        self.btn_box.rejected.connect(self.reject)
        row_btn.addWidget(self.btn_box)
        lay.addLayout(row_btn)

        self._update_suffixes()
        self._update_tol_warning()

    # ------------------------------------------------------------------
    # Слои
    # ------------------------------------------------------------------

    def _refresh_layers(self):
        """Перечитывает слои проекта, сохраняя снятые галочки.

        Диалог строит список слоёв один раз при открытии; если проект
        пополнился, пока диалог открыт, новые слои появятся только после
        этой кнопки. Уже снятые пользователем галочки сохраняются.
        """
        self._fill_layers(set(self._disabled_ids()))

    def _fill_layers(self, disabled):
        """Список векторных слоёв проекта с галочками участия."""
        self.lst_layers.clear()
        layers = []
        try:
            for lyr in QgsProject.instance().mapLayers().values():
                if not isinstance(lyr, QgsVectorLayer) or not lyr.isValid():
                    continue
                if lyr.geometryType() == QgsWkbTypes.NullGeometry:
                    continue
                layers.append(lyr)
        except Exception:
            pass
        try:
            layers.sort(key=lambda l: l.name().lower())
        except Exception:
            pass
        for lyr in layers:
            item = QListWidgetItem(lyr.name(), self.lst_layers)
            item.setData(Qt.ItemDataRole.UserRole, lyr.id())
            item.setFlags(Qt.ItemFlag.ItemIsEnabled |
                          Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(
                Qt.CheckState.Unchecked if lyr.id() in disabled
                else Qt.CheckState.Checked)
            try:
                if not lyr.isVisible():
                    item.setToolTip(
                        u"Слой скрыт в панели слоёв — в прилипании не "
                        u"участвует, пока его видимость не включена.")
            except Exception:
                pass

    def _set_all_checked(self, checked):
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        for i in range(self.lst_layers.count()):
            self.lst_layers.item(i).setCheckState(state)

    def _check_all(self):
        self._set_all_checked(True)

    def _uncheck_all(self):
        self._set_all_checked(False)

    def _disabled_ids(self):
        """id слоёв, снятые галочки которых = чёрный список."""
        out = []
        for i in range(self.lst_layers.count()):
            item = self.lst_layers.item(i)
            if item.checkState() != Qt.CheckState.Checked:
                lid = item.data(Qt.ItemDataRole.UserRole)
                if lid:
                    out.append(str(lid))
        return out

    # ------------------------------------------------------------------
    # Допуск
    # ------------------------------------------------------------------

    def _setup_spin(self, spin, value):
        spin.setRange(settings.MIN_TOL, settings.MAX_TOL)
        spin.setDecimals(1)
        spin.setSingleStep(1.0)
        try:
            spin.setValue(float(value))
        except Exception:
            spin.setValue(settings.DEFAULT_EDGE_TOL)

    def _update_suffixes(self):
        """Суффикс единиц у счётчиков: « px» или « ед. карты»."""
        suffix = (u" ед. карты"
                  if self.cmb_units.currentData() == settings.UNITS_MAP
                  else u" px")
        self.spn_edge.setSuffix(suffix)
        self.spn_vertex.setSuffix(suffix)

    def _select_overlap(self, mode):
        """Выбор режима перехлёста в комбобоксе (мусор -> «спрашивать»)."""
        idx = 0
        for i in range(self.cmb_overlap.count()):
            if self.cmb_overlap.itemData(i) == mode:
                idx = i
                break
        self.cmb_overlap.setCurrentIndex(idx)

    def _update_tol_warning(self):
        """Предупреждение: «строгий» радиус узлов меньше радиуса рёбер."""
        self.lbl_warn.setVisible(
            self.spn_vertex.value() < self.spn_edge.value())

    # ------------------------------------------------------------------
    # Правка нескольких слоёв (этап 4)
    # ------------------------------------------------------------------

    def _polygon_layers(self):
        """Полигональные слои проекта (для списков этапа 4)."""
        out = []
        try:
            for lyr in QgsProject.instance().mapLayers().values():
                if not isinstance(lyr, QgsVectorLayer) or not lyr.isValid():
                    continue
                if lyr.geometryType() != QgsWkbTypes.PolygonGeometry:
                    continue
                out.append(lyr)
        except Exception:
            pass
        try:
            out.sort(key=lambda l: l.name().lower())
        except Exception:
            pass
        return out

    def _fill_edit_exclusions(self):
        """Чёрный список правки: слои, которые операции НЕ двигают."""
        self.lst_edit_excl.clear()
        disabled = settings.edit_disabled_ids()
        for lyr in self._polygon_layers():
            item = QListWidgetItem(lyr.name(), self.lst_edit_excl)
            item.setData(Qt.ItemDataRole.UserRole, lyr.id())
            item.setFlags(Qt.ItemFlag.ItemIsEnabled |
                          Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(
                Qt.CheckState.Checked if lyr.id() in disabled
                else Qt.CheckState.Unchecked)

    def _fill_clip_modes(self):
        """Per-layer режим отсечения: слой + комбобокс (этап 4, п. 7)."""
        self.lst_clip_modes.clear()
        labels = [
            (u"отсекать", settings.CLIP_CLIP),
            (u"спрашивать", settings.CLIP_ASK),
            (u"не трогать", settings.CLIP_IGNORE),
        ]
        for lyr in self._polygon_layers():
            item = QListWidgetItem(lyr.name(), self.lst_clip_modes)
            item.setData(Qt.ItemDataRole.UserRole, lyr.id())
            widget = QWidget(self.lst_clip_modes)
            wlay = QHBoxLayout(widget)
            wlay.setContentsMargins(4, 0, 4, 0)
            wlay.addWidget(QLabel(lyr.name(), widget), 1)
            combo = QComboBox(widget)
            for label, data in labels:
                combo.addItem(label, data)
            cur = settings.clip_mode_for(lyr.id())
            for i in range(combo.count()):
                if combo.itemData(i) == cur:
                    combo.setCurrentIndex(i)
                    break
            combo.currentIndexChanged.connect(self._clip_mode_changed)
            wlay.addWidget(combo)
            item.setSizeHint(widget.sizeHint())
            self.lst_clip_modes.setItemWidget(item, widget)

    def _clip_mode_changed(self, _idx):
        """Сохраняет режим сразу при смене (слои — по id из item data)."""
        lw = self.lst_clip_modes
        for i in range(lw.count()):
            item = lw.item(i)
            widget = lw.itemWidget(item)
            if widget is None:
                continue
            combo = widget.findChild(QComboBox)
            if combo is None:
                continue
            mode = combo.currentData()
            settings.set_clip_mode(str(item.data(Qt.ItemDataRole.UserRole)),
                                   mode)

    def _edit_exclusion_ids(self):
        """id слоёв, отмеченных как исключённые из многослойной правки."""
        out = []
        for i in range(self.lst_edit_excl.count()):
            item = self.lst_edit_excl.item(i)
            if item.checkState() == Qt.CheckState.Checked:
                lid = item.data(Qt.ItemDataRole.UserRole)
                if lid:
                    out.append(str(lid))
        return out

    # ------------------------------------------------------------------
    # Применение
    # ------------------------------------------------------------------

    def _reset_defaults(self):
        self.cmb_units.setCurrentIndex(0)
        self.spn_edge.setValue(settings.DEFAULT_EDGE_TOL)
        self.spn_vertex.setValue(settings.DEFAULT_VERTEX_TOL)
        self._set_all_checked(True)
        self.chk_hints.setChecked(settings.DEFAULT_SHOW_HINTS)
        self.chk_slide.setChecked(settings.DEFAULT_EDGE_SLIDE)
        self._select_overlap(settings.DEFAULT_OVERLAP_MODE)
        self.chk_index.setChecked(settings.DEFAULT_INDEX_ON)
        self.spn_neighbors.setValue(settings.DEFAULT_MAX_NEIGHBORS)
        self.spn_tile.setValue(settings.DEFAULT_TILE_FACTOR)
        # этап 5: пороги аудита
        self.spn_gap_area.setValue(settings.DEFAULT_GAP_MAX_AREA)
        self.spn_near_tol.setValue(settings.DEFAULT_NEAR_NODE_TOL)
        self.spn_sliver.setValue(settings.DEFAULT_SLIVER_RATIO)
        self.spn_fix_pct.setValue(settings.DEFAULT_FIX_AREA_PCT)
        # этап 4: все слои в правке, без исключений, отсечение — как раньше
        self.rb_scope_all.setChecked(True)
        for i in range(self.lst_edit_excl.count()):
            self.lst_edit_excl.item(i).setCheckState(
                Qt.CheckState.Unchecked)
        for i in range(self.lst_clip_modes.count()):
            widget = self.lst_clip_modes.itemWidget(
                self.lst_clip_modes.item(i))
            if widget is not None:
                combo = widget.findChild(QComboBox)
                if combo is not None:
                    combo.setCurrentIndex(0)  # «отсекать»

    def current_config(self):
        """Текущее состояние диалога в виде конфига settings.save_settings."""
        return {
            "edge_tol": self.spn_edge.value(),
            "vertex_tol": self.spn_vertex.value(),
            "units": (self.cmb_units.currentData()
                      if self.cmb_units.currentData() in
                      (settings.UNITS_PX, settings.UNITS_MAP)
                      else settings.UNITS_PX),
            "disabled": self._disabled_ids(),
            "overlap_mode": (self.cmb_overlap.currentData()
                             if self.cmb_overlap.currentData() in
                             settings.OVERLAP_MODES
                             else settings.DEFAULT_OVERLAP_MODE),
            "show_hints": self.chk_hints.isChecked(),
            "edge_slide": self.chk_slide.isChecked(),
            "index_on": self.chk_index.isChecked(),
            "max_neighbors": self.spn_neighbors.value(),
            "tile_factor": self.spn_tile.value(),
        }

    def apply(self):
        """Сохраняет настройки; движок подхватит их при следующем снэпе."""
        ok = settings.save_settings(self.current_config())
        # этап 4: область правки, исключения и per-layer режимы отсечения
        settings.set_edit_scope(
            settings.EDIT_SCOPE_ACTIVE if self.rb_scope_active.isChecked()
            else settings.EDIT_SCOPE_ALL)
        settings.set_edit_disabled(self._edit_exclusion_ids())
        self._clip_mode_changed(0)
        # этап 5: пороги аудита слоя
        settings.set_audit_thresholds(
            gap_max_area=self.spn_gap_area.value(),
            near_node_tol=self.spn_near_tol.value(),
            sliver_ratio=self.spn_sliver.value(),
            fix_area_pct=self.spn_fix_pct.value())
        return ok

    def accept(self):
        self.apply()
        super(SettingsDialog, self).accept()
