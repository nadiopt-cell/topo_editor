# -*- coding: utf-8 -*-
"""TopoPolyEdit — главный класс плагина.

Создаёт панель инструментов и действия:
  * «Топологическое перемещение узлов» — перетаскивание общего узла
    сразу во всех полигонах редактируемого слоя;
  * «Перемещение ребра» — перетаскивание целого ребра: оба конца
    и все смежные полигоны двигаются сразу;
  * «Добавить узел на ребре» — топологическая вставка узла на ребро;
  * «Настройки прилипания…» — выбор слоёв-эталонов и допусков.
"""

import os

from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction
from qgis.core import Qgis

from .topo_move_tool import TopoMoveTool
from .topo_edge_move_tool import TopoEdgeMoveTool
from .topo_add_vertex_tool import TopoAddVertexTool
from .settings_dialog import SettingsDialog

MENU_TITLE = u"Топологическое редактирование"

TOOLTIP_MOVE = (
    u"Топологическое перемещение узлов\n\n"
    u"ЛКМ по узлу и перетаскивание: узел смещается во всех полигонах\n"
    u"редактируемого слоя, где он есть. Прилипание к узлам и рёбрам\n"
    u"всех включенных слоёв в экстенте карты. Перехлесты отсекаются\n"
    u"автоматически. Esc — отмена/выход.")

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
    u"рёбрам выбранных слоёв, автоотсечение перехлестов.\n"
    u"Esc — отмена/выход.")

TOOLTIP_SETTINGS = (
    u"Настройки прилипания\n\n"
    u"Выбор слоёв, к которым выполняется прилипание, и допуска\n"
    u"(рёбра / узлы, в пикселях или единицах карты).\n"
    u"Настройки действуют сразу на все три инструмента.")


class TopoPolyEditPlugin(object):
    """Класс плагина, инициализируемый через classFactory."""

    def __init__(self, iface):
        self.iface = iface
        self.toolbar = None
        self.act_move = None
        self.act_edge = None
        self.act_add = None
        self.act_settings = None
        self.tool_move = None
        self.tool_edge = None
        self.tool_add = None

    # ------------------------------------------------------------------
    def initGui(self):
        base = os.path.dirname(os.path.abspath(__file__))
        icon_move = QIcon(os.path.join(base, "icons", "topo_move.svg"))
        icon_edge = QIcon(os.path.join(base, "icons", "topo_edge.svg"))
        icon_add = QIcon(os.path.join(base, "icons", "topo_add_vertex.svg"))
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

        self.act_settings = QAction(icon_settings, u"Настройки прилипания…",
                                    self.iface.mainWindow())
        self.act_settings.setToolTip(TOOLTIP_SETTINGS)
        self.act_settings.triggered.connect(self._on_settings_triggered)

        for act in (self.act_move, self.act_edge, self.act_add,
                    self.act_settings):
            self.toolbar.addAction(act)
            self.iface.addPluginToVectorMenu(MENU_TITLE, act)

        self.tool_move = TopoMoveTool(self.iface, self.act_move)
        self.tool_edge = TopoEdgeMoveTool(self.iface, self.act_edge)
        self.tool_add = TopoAddVertexTool(self.iface, self.act_add)

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
                    self.act_settings):
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
        self.tool_move = None
        self.tool_edge = None
        self.tool_add = None
        self.act_move = None
        self.act_edge = None
        self.act_add = None
        self.act_settings = None
        self.toolbar = None
