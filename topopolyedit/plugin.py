# -*- coding: utf-8 -*-
"""TopoPolyEdit — главный класс плагина.

Создаёт панель инструментов и действия:
  * «Топологическое перемещение узлов» — перетаскивание общего узла
    сразу во всех полигонах редактируемого слоя;
  * «Перемещение ребра» — перетаскивание целого ребра: оба конца
    и все смежные полигоны двигаются сразу;
  * «Добавить узел на ребре» — топологическая вставка узла на ребро;
  * «Аудит узлов» — вершины редактируемого слоя и слоёв прилипания
    одновременно, контрастными цветами; красные крестики — узлы общей
    границы без пары на другой стороне (топологические ошибки);
  * «Отменить правку» — откат последней операции плагина целиком;
    работает, даже если редактируемый слой не активен (штатная отмена
    QGIS привязана к активному слою и с модулем часто остаётся серой);
    Ctrl+Z в окне карты;
  * «Настройки прилипания…» — выбор слоёв-эталонов и допусков.
"""

import os

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QIcon, QKeySequence
from qgis.PyQt.QtWidgets import QAction
from qgis.core import Qgis, QgsProject

from .topo_move_tool import TopoMoveTool
from .topo_edge_move_tool import TopoEdgeMoveTool
from .topo_add_vertex_tool import TopoAddVertexTool
from .audit_overlay import NodeAuditOverlay
from .settings_dialog import SettingsDialog
from . import settings
from . import utils

MENU_TITLE = u"Топологическое редактирование"

TOOLTIP_MOVE = (
    u"Топологическое перемещение узлов\n\n"
    u"ЛКМ по узлу и перетаскивание: узел смещается во всех полигонах\n"
    u"редактируемого слоя, где он есть. Прилипание к узлам и рёбрам\n"
    u"всех включенных слоёв в экстенте карты; вершины слоёв прилипания\n"
    u"подсвечиваются. Перехлёсты с другими слоями отсекаются автоматически,\n"
    u"с объектами этого же слоя — по запросу. Esc — отмена/выход.")

TOOLTIP_ADD = (
    u"Добавить узел на ребре (топологически)\n\n"
    u"Клик по ребру: узел вставляется во все полигоны редактируемого\n"
    u"слоя, содержащие это ребро, включая T-образные примыкания.\n"
    u"Esc — выход.")

TOOLTIP_EDGE = (
    u"Перемещение ребра (топологически)\n\n"
    u"ЛКМ по ребру и перетаскивание: ребро смещается параллельным\n"
    u"переносом, оба его конца обновляются во всех полигонах\n"
    u"редактируемого слоя, где они есть (общее ребро и примыкающие\n"
    u"только концом полигоны двигаются сразу). Прилипание к узлам и\n"
    u"рёбрам выбранных слоёв (вершины подсвечиваются), автоотсечение\n"
    u"перехлестов; перехлёст с объектами этого же слоя — по запросу.\n"
    u"Esc — отмена/выход.")

TOOLTIP_UNDO = (
    u"Отменить правку (последнюю операцию плагина)\n\n"
    u"Откатывает последнюю операцию ЦЕЛИКОМ: перемещение узлов или\n"
    u"ребра (вместе с автоотсечением перехлёстов и обрезкой соседей),\n"
    u"вставку узла. Работает, даже если редактируемый слой НЕ активен\n"
    u"в панели слоёв — штатная отмена QGIS привязана к активному слою\n"
    u"и потому с модулем часто остаётся серой. Ctrl+Z — в окне карты.")

TOOLTIP_SETTINGS = (
    u"Настройки прилипания\n\n"
    u"Выбор слоёв, к которым выполняется прилипание, и допуска\n"
    u"(рёбра / узлы, в пикселях или единицах карты), подсветка вершин\n"
    u"и режим обработки перехлёста с объектами своего слоя.\n"
    u"Настройки действуют сразу на все три инструмента.")

TOOLTIP_AUDIT = (
    u"Аудит узлов: вершины двух групп слоёв сразу\n\n"
    u"Оранжевые квадраты — узлы редактируемого слоя (или активного\n"
    u"полигонального), синие кружки — узлы слоёв прилипания.\n"
    u"Красные крестики — узлы на чужой границе, у которых нет пары на\n"
    u"другой стороне стыка: лишние или недостающие узлы — топологические\n"
    u"ошибки на границах слоёв. Режим работает поверх любого инструмента;\n"
    u"картина обновляется при пан/зуме и правках слоя. Состояние\n"
    u"запоминается между сеансами QGIS.")


class TopoPolyEditPlugin(object):
    """Класс плагина, инициализируемый через classFactory."""

    def __init__(self, iface):
        self.iface = iface
        self.toolbar = None
        self.act_move = None
        self.act_edge = None
        self.act_add = None
        self.act_undo = None
        self._undo_shortcut = None
        self.act_audit = None
        self.act_settings = None
        self.tool_move = None
        self.tool_edge = None
        self.tool_add = None
        self.audit = None

    # ------------------------------------------------------------------
    def initGui(self):
        base = os.path.dirname(os.path.abspath(__file__))
        icon_move = QIcon(os.path.join(base, "icons", "topo_move.svg"))
        icon_edge = QIcon(os.path.join(base, "icons", "topo_edge.svg"))
        icon_add = QIcon(os.path.join(base, "icons", "topo_add_vertex.svg"))
        icon_undo = QIcon(os.path.join(base, "icons", "topo_undo.svg"))
        icon_audit = QIcon(os.path.join(base, "icons", "topo_audit.svg"))
        icon_settings = QIcon(os.path.join(base, "icons",
                                           "topo_settings.svg"))

        self.toolbar = self.iface.addToolBar(MENU_TITLE)
        self.toolbar.setObjectName("TopoPolyEditToolbar")

        self.act_move = QAction(icon_move, u"Топологическое перемещение узлов",
                                self.iface.mainWindow())
        self.act_move.setCheckable(True)
        self.act_move.setToolTip(TOOLTIP_MOVE)
        self.act_move.triggered.connect(self._on_move_triggered)

        self.act_edge = QAction(icon_edge, u"Перемещение ребра",
                                self.iface.mainWindow())
        self.act_edge.setCheckable(True)
        self.act_edge.setToolTip(TOOLTIP_EDGE)
        self.act_edge.triggered.connect(self._on_edge_triggered)

        self.act_add = QAction(icon_add, u"Добавить узел на ребре",
                               self.iface.mainWindow())
        self.act_add.setCheckable(True)
        self.act_add.setToolTip(TOOLTIP_ADD)
        self.act_add.triggered.connect(self._on_add_triggered)

        self.act_undo = QAction(icon_undo, u"Отменить правку",
                                self.iface.mainWindow())
        self.act_undo.setToolTip(TOOLTIP_UNDO)
        self.act_undo.triggered.connect(self._on_undo_triggered)

        self.act_audit = QAction(icon_audit, u"Аудит узлов",
                                 self.iface.mainWindow())
        self.act_audit.setCheckable(True)
        self.act_audit.setToolTip(TOOLTIP_AUDIT)
        self.act_audit.triggered.connect(self._on_audit_triggered)

        self.act_settings = QAction(icon_settings, u"Настройки прилипания…",
                                    self.iface.mainWindow())
        self.act_settings.setToolTip(TOOLTIP_SETTINGS)
        self.act_settings.triggered.connect(self._on_settings_triggered)

        for act in (self.act_move, self.act_edge, self.act_add,
                    self.act_undo, self.act_audit, self.act_settings):
            self.toolbar.addAction(act)
            self.iface.addPluginToVectorMenu(MENU_TITLE, act)

        self.tool_move = TopoMoveTool(self.iface, self.act_move)
        self.tool_edge = TopoEdgeMoveTool(self.iface, self.act_edge)
        self.tool_add = TopoAddVertexTool(self.iface, self.act_add)
        self.audit = NodeAuditOverlay(self.iface)

        # Ctrl+Z в окне карты: сокращённый контекст (WidgetWithChildren)
        # у канвы — пока фокус в окне карты, срабатывает НАША отмена,
        # а не штатная QGIS (она привязана к активному слою). Вне окна
        # карты действует штатная. Безопасно вне QGIS/в старых PyQt:
        # при любой ошибке остаётся кнопка на тулбаре.
        self._undo_shortcut = None
        try:
            try:
                from qgis.PyQt.QtGui import QShortcut as _QShortcut  # Qt6
            except ImportError:
                from qgis.PyQt.QtWidgets import QShortcut as _QShortcut
            sc = _QShortcut(QKeySequence(QKeySequence.Undo),
                            self.iface.mapCanvas())
            sc.setContext(utils.enum_member(
                Qt, "ShortcutContext", "WidgetWithChildrenShortcut"))
            sc.activated.connect(self._on_undo_triggered)
            self._undo_shortcut = sc
        except Exception as exc:
            utils.log(u"Горячая клавиша Ctrl+Z не назначена: {}".format(exc))

        # восстановление состояния «Аудита узлов» прошлого сеанса
        if settings.audit_enabled():
            self.act_audit.setChecked(True)
            try:
                self.audit.set_enabled(True)
            except Exception:
                self.act_audit.setChecked(False)

    # ------------------------------------------------------------------
    def _on_move_triggered(self, checked):
        if checked:
            self.act_edge.setChecked(False)
            self.act_add.setChecked(False)
            self.iface.mapCanvas().setMapTool(self.tool_move)
        else:
            if self.iface.mapCanvas().mapTool() is self.tool_move:
                self.iface.actionPan().trigger()

    def _on_edge_triggered(self, checked):
        if checked:
            self.act_move.setChecked(False)
            self.act_add.setChecked(False)
            self.iface.mapCanvas().setMapTool(self.tool_edge)
        else:
            if self.iface.mapCanvas().mapTool() is self.tool_edge:
                self.iface.actionPan().trigger()

    def _on_add_triggered(self, checked):
        if checked:
            self.act_move.setChecked(False)
            self.act_edge.setChecked(False)
            self.iface.mapCanvas().setMapTool(self.tool_add)
        else:
            if self.iface.mapCanvas().mapTool() is self.tool_add:
                self.iface.actionPan().trigger()

    def _on_audit_triggered(self, checked):
        try:
            self.audit.set_enabled(checked)
        except Exception as exc:
            self.iface.messageBar().pushMessage(
                MENU_TITLE,
                u"Не удалось переключить аудит узлов: {}".format(exc),
                level=Qgis.Critical, duration=5)
            self.act_audit.setChecked(False)
            try:
                self.audit.set_enabled(False)
            except Exception:
                pass
        settings.set_audit_enabled(bool(checked))

    def _on_undo_triggered(self):
        """«Отменить правку»: откат последней операции на её слое.

        Цель — utils.find_undo_target: последний слой, изменённый
        плагином, затем активный слой, затем любой слой в правке с
        непустым стеком отмены. Кнопка не дизейблится (чтобы не
        повторять главную проблему штатной отмены — «непонятно почему
        серая»): при отсутствии цели показывается поясняющее сообщение.
        """
        try:
            layer = utils.find_undo_target(
                QgsProject.instance().mapLayers().values(),
                last_edited=utils.last_edited_layer(),
                active=self.iface.activeLayer())
            if layer is None:
                self.iface.messageBar().pushMessage(
                    MENU_TITLE,
                    u"Нет изменений для отмены: на слоях в режиме "
                    u"редактирования нет правок.",
                    level=Qgis.Info, duration=4)
                return
            stack = layer.undoStack()
            text = u""
            try:
                idx = stack.index()
                if idx > 0:
                    text = stack.text(idx - 1) or u""
            except Exception:
                text = u""
            stack.undo()
            self.iface.messageBar().pushMessage(
                MENU_TITLE,
                u"Отменено: {} (слой «{}»)".format(
                    text or u"последняя правка", layer.name()),
                level=Qgis.Info, duration=4)
        except Exception as exc:
            self.iface.messageBar().pushMessage(
                MENU_TITLE, u"Не удалось отменить: {}".format(exc),
                level=Qgis.Critical, duration=5)

    def _on_settings_triggered(self):
        try:
            dlg = SettingsDialog(self.iface, self.iface.mainWindow())
            dlg.exec_()
        except Exception as exc:
            self.iface.messageBar().pushMessage(
                u"Топологическое редактирование",
                u"Не удалось открыть настройки: {}".format(exc),
                level=Qgis.Critical, duration=5)

    # ------------------------------------------------------------------
    def unload(self):
        # горячая клавиша Ctrl+Z: снять с канвы до удаления обработчика
        if self._undo_shortcut is not None:
            try:
                self._undo_shortcut.setEnabled(False)
            except Exception:
                pass
            try:
                self._undo_shortcut.deleteLater()
            except Exception:
                pass
            self._undo_shortcut = None
        # оверлей аудита: отключить сигналы и сбросить ленты
        if self.audit is not None:
            try:
                self.audit.shutdown()
            except Exception:
                pass
        # если какой-то из инструментов ещё активен — снять его с канвы,
        # чтобы QGIS не держал ссылку на удаляемый объект
        try:
            canvas = self.iface.mapCanvas()
        except Exception:
            canvas = None
        for tool in (self.tool_move, self.tool_edge, self.tool_add):
            if tool is None or canvas is None:
                continue
            try:
                if canvas.mapTool() is tool:
                    canvas.unsetMapTool(tool)
            except Exception:
                pass
        for act in (self.act_move, self.act_edge, self.act_add,
                    self.act_undo, self.act_audit, self.act_settings):
            if act is None:
                continue
            try:
                self.iface.removePluginMenu(MENU_TITLE, act)
            except Exception:
                pass
            try:
                self.iface.removeToolBarIcon(act)
            except Exception:
                pass
        if self.toolbar is not None:
            self.toolbar.deleteLater()
        self.audit = None
        self.tool_move = None
        self.tool_edge = None
        self.tool_add = None
        self.act_move = None
        self.act_edge = None
        self.act_add = None
        self.act_undo = None
        self._undo_shortcut = None
        self.act_audit = None
        self.act_settings = None
        self.toolbar = None
