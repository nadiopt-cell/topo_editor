# -*- coding: utf-8 -*-
"""Панель «Аудит слоя» (этап 5 ТЗ, п. 2–3): док-панель с таблицей
ошибок, фильтром по коду, переходом на место (двойной клик — мигание),
исправлением («Исправить», «Исправить все (безопасные)») и выгрузкой в
точечный слой memory: с возможностью сохранить в GeoPackage.

Аудит выполняется в фоне (LayerAuditTask — QgsTask с прогрессом и
отменой); исправления — в главном потоке (FixEngine: валидатор +
атомарная запись + одна команда отмены на пачку). После успешного
исправления аудит перезапускается — таблица всегда показывает
актуальное состояние.

Маркеры ошибок на карте — FindingsOverlay (бюджет, отложенный
пересбор). Все сбои интерфейса не должны ронять QGIS: обёрнуты.
"""

from qgis.PyQt.QtCore import Qt, QVariant
from qgis.PyQt.QtWidgets import (
    QComboBox, QFileDialog, QGroupBox, QHBoxLayout,
    QHeaderView, QLabel, QMessageBox, QProgressBar, QPushButton,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)
from qgis.core import (
    Qgis, QgsField, QgsFeature, QgsGeometry, QgsPointXY,
    QgsProject, QgsVectorFileWriter, QgsVectorLayer, QgsWkbTypes,
)
from qgis.gui import QgsDockWidget

from . import settings
from . import utils
from .core.checks import (
    CHECK_TITLES, SAFE_FIX_CODES, Finding,
    CODE_DUP_NODE, CODE_GAP, CODE_INVALID, CODE_NEAR_NODE, CODE_OVERLAP,
    CODE_SPIKE, CODE_SLIVER, CODE_T_JUNCTION,
)
from .layer_audit import FixEngine, LayerAuditTask
from .findings_overlay import FindingsOverlay, severity_color, \
    transform_to_map

ALL_CODES_ITEM = u"Все коды"


def _exec(dialog):
    """Диалоговый exec: PyQt6 — exec(), PyQt5 — exec_()."""
    try:
        return dialog.exec()
    except AttributeError:
        return dialog.exec_()


def _sbtn(name):
    """QMessageBox.StandardButton.<name> для Qt5/Qt6."""
    try:
        return utils.enum_member(QMessageBox, "StandardButton", name)
    except Exception:
        return None


def _brole(name):
    """QMessageBox.ButtonRole.<name> для Qt5/Qt6."""
    try:
        return utils.enum_member(QMessageBox, "ButtonRole", name)
    except Exception:
        return None

# колонки таблицы
_COLS = [u"Код", u"Слой", u"fid", u"X", u"Y", u"Площадь", u"Сообщение"]


class AuditDock(QgsDockWidget):
    """Док «Аудит слоя»: поиск и безопасное исправление ошибок."""

    def __init__(self, iface, parent=None):
        try:
            super(AuditDock, self).__init__(
                u"Аудит слоя (TopoPolyEdit)", parent)
        except TypeError:
            super(AuditDock, self).__init__(u"Аудит слоя (TopoPolyEdit)")
        self.iface = iface
        self._task = None
        self._audit_running = False
        self._findings = []
        self._overlay = None
        try:
            self._overlay = FindingsOverlay(iface)
        except Exception:
            self._overlay = None
        self._build_ui()
        self._fill_layers()

    # ------------------------------------------------------------------
    # Интерфейс
    # ------------------------------------------------------------------

    def _build_ui(self):
        body = QWidget(self)
        lay = QVBoxLayout(body)
        lay.setContentsMargins(6, 6, 6, 6)

        grp_src = QGroupBox(u"Слой", body)
        h1 = QHBoxLayout(grp_src)
        self.cmb_layer = QComboBox(grp_src)
        self.cmb_layer.setMinimumWidth(160)
        h1.addWidget(self.cmb_layer, 1)
        self.btn_run = QPushButton(u"Проверить", grp_src)
        self.btn_run.clicked.connect(self._on_run)
        h1.addWidget(self.btn_run)
        lay.addWidget(grp_src)

        self.progress = QProgressBar(body)
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        lay.addWidget(self.progress)
        self.lbl_status = QLabel(u"", body)
        self.lbl_status.setWordWrap(True)
        lay.addWidget(self.lbl_status)

        h2 = QHBoxLayout()
        h2.addWidget(QLabel(u"Фильтр:", body))
        self.cmb_filter = QComboBox(body)
        self.cmb_filter.addItem(ALL_CODES_ITEM, "")
        for code in (CODE_INVALID, CODE_GAP, CODE_OVERLAP, CODE_DUP_NODE,
                     CODE_NEAR_NODE, CODE_T_JUNCTION, CODE_SPIKE,
                     CODE_SLIVER):
            self.cmb_filter.addItem(
                u"{} — {}".format(code, CHECK_TITLES.get(code, u"")), code)
        self.cmb_filter.currentIndexChanged.connect(self._on_filter)
        h2.addWidget(self.cmb_filter, 1)
        lay.addLayout(h2)

        self.table = QTableWidget(0, len(_COLS), body)
        self.table.setHorizontalHeaderLabels(_COLS)
        try:
            hdr = self.table.horizontalHeader()
            hdr.setSectionResizeMode(QHeaderView.ResizeToContents)
            hdr.setStretchLastSection(True)
        except Exception:
            pass
        try:
            self.table.setSelectionBehavior(
                utils.enum_member(QTableWidget, "SelectionBehavior",
                                  "SelectRows"))
            self.table.setSelectionMode(
                utils.enum_member(QTableWidget, "SelectionMode",
                                  "SingleSelection"))
            edit_trig = utils.enum_member(QTableWidget, "EditTrigger",
                                          "NoEditTriggers")
            self.table.setEditTriggers(edit_trig)
        except Exception:
            pass
        self.table.itemDoubleClicked.connect(self._on_goto)
        lay.addWidget(self.table, 1)

        grp_fix = QGroupBox(u"Исправление", body)
        h3 = QHBoxLayout(grp_fix)
        self.btn_fix = QPushButton(u"Исправить", grp_fix)
        self.btn_fix.clicked.connect(self._on_fix_selected)
        self.btn_fix_all = QPushButton(u"Исправить все (безопасные)",
                                       grp_fix)
        self.btn_fix_all.clicked.connect(self._on_fix_all)
        self.btn_export = QPushButton(u"Экспорт в слой", grp_fix)
        self.btn_export.clicked.connect(self._on_export)
        h3.addWidget(self.btn_fix)
        h3.addWidget(self.btn_fix_all)
        h3.addWidget(self.btn_export)
        lay.addWidget(grp_fix)

        self.setWidget(body)

    def _fill_layers(self):
        self.cmb_layer.blockSignals(True)
        self.cmb_layer.clear()
        try:
            layers = []
            for lyr in QgsProject.instance().mapLayers().values():
                if not isinstance(lyr, QgsVectorLayer):
                    continue
                try:
                    if not lyr.isValid() \
                            or lyr.geometryType() != QgsWkbTypes.PolygonGeometry:
                        continue
                except Exception:
                    continue
                layers.append(lyr)
            active = None
            try:
                active = self.iface.activeLayer()
            except Exception:
                active = None
            layers.sort(key=lambda l: l.name())
            for lyr in layers:
                self.cmb_layer.addItem(lyr.name(), lyr.id())
            if active is not None:
                idx = self.cmb_layer.findData(active.id())
                if idx >= 0:
                    self.cmb_layer.setCurrentIndex(idx)
        except Exception as exc:
            utils.log(u"Аудит: список слоёв — {}".format(exc))
        self.cmb_layer.blockSignals(False)

    # ------------------------------------------------------------------
    # Запуск аудита
    # ------------------------------------------------------------------

    def current_layer(self):
        lid = self.cmb_layer.currentData()
        if not lid:
            return None
        try:
            lyr = QgsProject.instance().mapLayer(lid)
            return lyr if lyr is not None else None
        except Exception:
            return None

    def _on_run(self):
        layer = self.current_layer()
        if layer is None:
            self._status(u"Выберите полигональный слой.", warning=True)
            return
        if self._audit_running:
            self._status(u"Аудит уже выполняется…", warning=True)
            return
        opts = settings.audit_opts()
        task = LayerAuditTask(layer, opts)
        try:
            task.taskCompleted.connect(self._on_task_completed)
            task.taskTerminated.connect(self._on_task_terminated)
        except Exception as exc:
            # без менеджера задач (офлайн-тесты) — синхронный прогон
            utils.log(u"Аудит: фоновый запуск недоступен ({}), "
                      u"выполняется синхронно".format(exc))
            task.run()
            self._apply_findings(task.findings)
            return
        self._task = task
        self._audit_running = True
        self.progress.setValue(0)
        self._status(u"Аудит слоя «{}» выполняется…".format(layer.name()))
        try:
            QgsProject.instance().taskManager().addTask(task)
        except Exception as exc:
            utils.log(u"Аудит: задача не поставлена ({}), выполняется "
                      u"синхронно".format(exc))
            self._task = None
            self._audit_running = False
            task.run()
            self._apply_findings(task.findings)

    def _on_task_completed(self):
        self._audit_running = False
        task = self._task
        self._task = None
        if task is None:
            return
        self._apply_findings(task.findings)

    def _on_task_terminated(self):
        self._audit_running = False
        task = self._task
        self._task = None
        if task is None:
            return
        self._status(u"Аудит прерван: {}".format(
            task.error or u"отменено"), warning=True)

    def _apply_findings(self, findings):
        self._findings = list(findings or [])
        total = len(self._findings)
        fixable = sum(1 for f in self._findings if f.fixable)
        self.progress.setValue(100)
        self._status(u"Найдено ошибок: {} (исправимо автоматически: {})"
                     .format(total, fixable))
        self._refill()
        self._update_overlay()

    def _update_overlay(self):
        if self._overlay is None:
            return
        layer = self.current_layer()
        pts = []
        for f in self._filtered():
            if f.point is None:
                continue
            pt = transform_to_map(self.iface,
                                  layer.crs() if layer else None, f.point)
            pts.append((pt, severity_color(f)))
        try:
            self._overlay.set_findings(pts)
        except Exception as exc:
            utils.log(u"Аудит: оверлей — {}".format(exc))

    # ------------------------------------------------------------------
    # Таблица
    # ------------------------------------------------------------------

    def _filtered(self):
        code = self.cmb_filter.currentData()
        if not code:
            return self._findings
        return [f for f in self._findings if f.code == code]

    def _on_filter(self):
        self._refill()

    def _refill(self):
        rows = self._filtered()
        # Qt5/Qt6: UserRole и GlobalColor.darkRed — через enum_member
        try:
            user_role = utils.enum_member(Qt, "ItemDataRole", "UserRole")
        except Exception:
            user_role = 256  # Qt.UserRole в Qt5
        try:
            dark_red = utils.enum_member(Qt, "GlobalColor", "darkRed")
        except Exception:
            dark_red = None
        self.table.setRowCount(len(rows))
        for i, f in enumerate(rows):
            vals = [
                f.code,
                f.layer_name or u"",
                u"#" + str(f.fid) if f.fid is not None else u"",
                u"{:.6f}".format(f.point.x()) if f.point else u"",
                u"{:.6f}".format(f.point.y()) if f.point else u"",
                (u"{:.6g}".format(f.area)
                 if f.area is not None else u""),
                (u"✓ исправимо: " if f.fixable else u"") + (f.message
                                                            or u""),
            ]
            for c, v in enumerate(vals):
                item = QTableWidgetItem(v)
                item.setData(user_role, i)
                if c == 0 and not f.fixable and dark_red is not None:
                    try:
                        item.setForeground(dark_red)
                    except Exception:
                        pass
                self.table.setItem(i, c, item)

    def _finding_at_row(self, row):
        rows = self._filtered()
        if 0 <= row < len(rows):
            return rows[row]
        return None

    def _on_goto(self, item):
        """Двойной клик: переход на место с мигающим маркером."""
        f = self._finding_at_row(item.row())
        if f is None or f.point is None:
            return
        layer = self.current_layer()
        pt = f.point
        if layer is not None:
            pt = transform_to_map(self.iface, layer.crs(), f.point)
        self._zoom_and_blink(pt)

    def _zoom_and_blink(self, map_pt):
        """Центрировать карту на ошибке и мигнуть маркером 2 с."""
        try:
            canvas = self.iface.mapCanvas()
            extent = canvas.extent()
            w = max(extent.width(), 1e-9)
            h = max(extent.height(), 1e-9)
            from qgis.core import QgsRectangle
            rect = QgsRectangle(map_pt.x() - w * 0.02,
                                map_pt.y() - h * 0.02,
                                map_pt.x() + w * 0.02,
                                map_pt.y() + h * 0.02)
            canvas.setExtent(rect)
            canvas.refresh()
        except Exception:
            pass
        try:
            from qgis.gui import QgsVertexMarker
            from qgis.PyQt.QtGui import QColor
            from qgis.PyQt.QtCore import QTimer
            marker = QgsVertexMarker(self.iface.mapCanvas())
            marker.setIconType(QgsVertexMarker.ICON_X)
            marker.setColor(QColor(230, 20, 20))
            marker.setPenWidth(3)
            marker.setIconSize(14)
            marker.setCenter(map_pt)
            marker.show()
            QTimer.singleShot(2000, marker.hide)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Исправления
    # ------------------------------------------------------------------

    def _engine(self):
        layer = self.current_layer()
        if layer is None:
            return None
        return FixEngine(self.iface, layer)

    def _on_fix_selected(self):
        row = self.table.currentRow()
        f = self._finding_at_row(row)
        if f is None:
            self._status(u"Выберите ошибку в таблице.", warning=True)
            return
        if not f.fixable:
            self._status(u"Эта ошибка исправляется вручную "
                         u"(двойной клик — перейти на место).",
                         warning=True)
            return
        engine = self._engine()
        if engine is None:
            self._status(u"Выберите слой.", warning=True)
            return
        report = engine.fix_findings([f], u"Аудит: исправление ошибки",
                                     overlap_decider=self._ask_overlap,
                                     makevalid_decider=self._ask_makevalid)
        self._report_to_ui(report)

    def _on_fix_all(self):
        engine = self._engine()
        if engine is None:
            self._status(u"Выберите слой.", warning=True)
            return
        batch = [f for f in self._findings
                 if f.fixable and f.code in SAFE_FIX_CODES]
        if not batch:
            self._status(u"Нет исправимых автоматически ошибок из "
                         u"безопасных кодов ({}).".format(
                             u", ".join(SAFE_FIX_CODES)), warning=True)
            return
        report = engine.fix_findings(
            batch, u"Аудит: исправить все (безопасные)",
            overlap_decider=self._ask_overlap,
            makevalid_decider=self._ask_makevalid)
        self._report_to_ui(report)

    def _report_to_ui(self, report):
        if report.get("error"):
            self._status(report["error"], warning=True)
            try:
                self.iface.messageBar().pushMessage(
                    u"Аудит слоя", report["error"],
                    level=Qgis.Warning, duration=6)
            except Exception:
                pass
            # даже при отказе часть находок могла быть отвергнута
            # payload'ом — обновлять состояние нечем; остаёмся на месте
            return
        refused = report.get("refused") or []
        text = u"Исправлено фич: {}. Отказано: {}.".format(
            report.get("fixed", 0), len(refused))
        if refused:
            text += u" " + u"; ".join(
                u"{} #{}: {}".format(r[0].code, r[0].fid, r[1])
                for r in refused[:4])
        pct = report.get("area_change_pct") or 0.0
        text += u" Изменение площади: {:.4g}%.".format(pct)
        self._status(text)
        try:
            self.iface.messageBar().pushMessage(
                u"Аудит слоя", text, level=Qgis.Info, duration=5)
        except Exception:
            pass
        # перезапуск аудита: таблица показывает актуальное состояние
        self._on_run()

    # - диалоги-решения ------------------------------------------------

    def _ask_overlap(self, finding):
        """Диалог OVERLAP: обрезать меньший / больший / отмена."""
        try:
            box = QMessageBox(self)
            box.setWindowTitle(u"Перехлёст полигонов")
            box.setText(u"Перехлёст #{} и #{} (площадь {:g}). Какой "
                        u"полигон обрезать?".format(
                            finding.payload.get("a_fid"),
                            finding.payload.get("b_fid"),
                            finding.area or 0.0))
            b_small = box.addButton(u"Обрезать меньший",
                                    _brole("YesRole"))
            b_large = box.addButton(u"Обрезать больший",
                                    _brole("NoRole"))
            box.addButton(u"Не исправлять", _brole("RejectRole"))
            _exec(box)
            clicked = box.clickedButton()
            if clicked is b_small:
                return "smaller"
            if clicked is b_large:
                return "larger"
        except Exception:
            pass
        return None

    def _ask_makevalid(self, finding, new_geom, area_before, area_after):
        """Диалог INVALID: подтверждение makeValid с площадями."""
        try:
            box = QMessageBox(self)
            box.setWindowTitle(u"Невалидная геометрия")
            box.setText(u"Полигон #{} невалиден. Исправить через "
                        u"makeValid? Форма изменится автоматически."
                        .format(finding.fid))
            box.setInformativeText(
                u"Площадь до: {:g}, после: {:g} (изменение {:g}). "
                u"Результат показывается после записи; операция "
                u"отменяется одним Ctrl+Z.".format(
                    area_before, area_after,
                    abs(area_after - area_before)))
            yes, no = _sbtn("Yes"), _sbtn("No")
            if yes is not None and no is not None:
                box.setStandardButtons(yes | no)
                box.setDefaultButton(no)
            return _exec(box) == yes
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Экспорт
    # ------------------------------------------------------------------

    def _on_export(self):
        rows = self._filtered()
        if not rows:
            self._status(u"Нет ошибок для экспорта.", warning=True)
            return
        layer = self.current_layer()
        try:
            crs = layer.crs() if layer is not None else None
            authid = crs.authid() if crs is not None else u"EPSG:4326"
            vl = QgsVectorLayer(
                u"Point?crs={}".format(authid or u"EPSG:4326"),
                u"Ошибки аудита: {}".format(
                    layer.name() if layer else u"?"), u"memory")
            if not vl.isValid():
                raise RuntimeError(u"memory-слой не создан")
            vl.dataProvider().addAttributes([
                QgsField(u"code", QVariant.String),
                QgsField(u"layer", QVariant.String),
                QgsField(u"fid", QVariant.LongLong),
                QgsField(u"area", QVariant.Double),
                QgsField(u"message", QVariant.String),
            ])
            vl.updateFields()
            feats = []
            for f in rows:
                if f.point is None:
                    continue
                ft = QgsFeature(vl.fields())
                ft.setGeometry(QgsGeometry.fromPointXY(
                    QgsPointXY(f.point)))
                ft.setAttribute(u"code", f.code)
                ft.setAttribute(u"layer", f.layer_name or u"")
                ft.setAttribute(u"fid", int(f.fid) if f.fid is not None
                                else -1)
                ft.setAttribute(u"area",
                                float(f.area) if f.area is not None
                                else None)
                ft.setAttribute(u"message", f.message or u"")
                feats.append(ft)
            vl.dataProvider().addFeatures(feats)
            vl.updateExtents()
            QgsProject.instance().addMapLayer(vl)
        except Exception as exc:
            self._status(u"Экспорт не удался: {}".format(exc),
                         warning=True)
            return
        self._status(u"Ошибки выгружены в слой «{}» ({} точек)."
                     .format(vl.name(), len(feats)))
        self._offer_gpkg_save(vl)

    def _offer_gpkg_save(self, vl):
        """Возможность сохранить выгруженный слой в GeoPackage."""
        try:
            box = QMessageBox(self)
            box.setWindowTitle(u"Сохранить в GeoPackage")
            box.setText(u"Слой «{}» добавлен в проект. Сохранить его в "
                        u"GeoPackage?".format(vl.name()))
            yes, no = _sbtn("Yes"), _sbtn("No")
            if yes is not None and no is not None:
                box.setStandardButtons(yes | no)
            if _exec(box) != yes:
                return
            path, _flt = QFileDialog.getSaveFileName(
                self, u"Файл GeoPackage", u"audit_errors.gpkg",
                u"GeoPackage (*.gpkg)")
            if not path:
                return
            if not path.lower().endswith(u".gpkg"):
                path += u".gpkg"
            options = QgsVectorFileWriter.SaveVectorOptions()
            options.driverName = u"GPKG"
            options.layerName = vl.name()
            res = QgsVectorFileWriter.writeAsVectorFormatV3(
                vl, path, QgsProject.instance().transformContext(),
                options)
            err = res[0] if isinstance(res, (tuple, list)) else res
            ok = str(err) in (u"NoError",) or err == 0
            if ok:
                self._status(u"Сохранено: {}".format(path))
            else:
                self._status(u"Не сохранено: {}".format(err),
                             warning=True)
        except Exception as exc:
            self._status(u"Сохранение в GeoPackage не удалось: {}"
                         .format(exc), warning=True)

    # ------------------------------------------------------------------
    # Служебное
    # ------------------------------------------------------------------

    def _status(self, text, warning=False):
        self.lbl_status.setText(text)
        if warning:
            try:
                self.lbl_status.setStyleSheet(
                    u"color: rgb(200, 60, 20);")
            except Exception:
                pass
        else:
            try:
                self.lbl_status.setStyleSheet(u"")
            except Exception:
                pass

    def shutdown(self):
        """Полная остановка: задача, оверлей (для unload плагина)."""
        if self._task is not None:
            try:
                self._task.cancel()
            except Exception:
                pass
            self._task = None
        self._audit_running = False
        if self._overlay is not None:
            try:
                self._overlay.shutdown()
            except Exception:
                pass

    def closeEvent(self, event):
        # остановить фоновую задачу и убрать оверлей
        if self._task is not None:
            try:
                self._task.cancel()
            except Exception:
                pass
            self._task = None
        if self._overlay is not None:
            try:
                self._overlay.shutdown()
            except Exception:
                pass
        super(AuditDock, self).closeEvent(event)
