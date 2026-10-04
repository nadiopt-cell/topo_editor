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
    QDialogButtonBox,
)
from qgis.core import QgsProject, QgsWkbTypes, QgsVectorLayer

from . import settings

_UNITS_LABELS = [
    (u"пикселей экрана", settings.UNITS_PX),
    (u"единиц карты", settings.UNITS_MAP),
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
        btn_all.clicked.connect(self._check_all)
        btn_none.clicked.connect(self._uncheck_all)
        row.addWidget(btn_all)
        row.addWidget(btn_none)
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
        lay.addWidget(grp_tol)

        # --------------------------------------------------------------
        # Кнопки
        # --------------------------------------------------------------
        row_btn = QHBoxLayout()
        self.btn_reset = QPushButton(u"По умолчанию", self)
        self.btn_reset.setToolTip(
            u"Вернуть значения по умолчанию (10 px рёбра, 15 px узлы,\n"
            u"прилипать ко всем слоям). Затем нажмите OK.")
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

    # ------------------------------------------------------------------
    # Слои
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Применение
    # ------------------------------------------------------------------

    def _reset_defaults(self):
        self.cmb_units.setCurrentIndex(0)
        self.spn_edge.setValue(settings.DEFAULT_EDGE_TOL)
        self.spn_vertex.setValue(settings.DEFAULT_VERTEX_TOL)
        self._set_all_checked(True)

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
        }

    def apply(self):
        """Сохраняет настройки; движок подхватит их при следующем снэпе."""
        return settings.save_settings(self.current_config())

    def accept(self):
        self.apply()
        super(SettingsDialog, self).accept()
