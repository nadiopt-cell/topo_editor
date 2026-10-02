# -*- coding: utf-8 -*-
"""TopoPolyEdit — главный класс плагина.

Создаёт панель инструментов и два проверяемых действия:
  * «Топологическое перемещение узлов» — перетаскивание общего узла
    сразу во всех полигонах редактируемого слоя;
  * «Добавить узел на ребре» — топологическая вставка узла на ребро.
"""

import os

from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction

from .topo_move_tool import TopoMoveTool
from .topo_add_vertex_tool import TopoAddVertexTool

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


class TopoPolyEditPlugin(object):
    """Класс плагина, инициализируемый через classFactory."""

    def __init__(self, iface):
        self.iface = iface
        self.toolbar = None
        self.act_move = None
        self.act_add = None
        self.tool_move = None
        self.tool_add = None

    # ------------------------------------------------------------------
    def initGui(self):
        base = os.path.dirname(os.path.abspath(__file__))
        icon_move = QIcon(os.path.join(base, "icons", "topo_move.svg"))
        icon_add = QIcon(os.path.join(base, "icons", "topo_add_vertex.svg"))

        self.toolbar = self.iface.addToolBar(MENU_TITLE)
        self.toolbar.setObjectName("TopoPolyEditToolbar")

        self.act_move = QAction(icon_move, u"Топологическое перемещение узлов",
                                self.iface.mainWindow())
        self.act_move.setCheckable(True)
        self.act_move.setToolTip(TOOLTIP_MOVE)
        self.act_move.triggered.connect(self._on_move_triggered)

        self.act_add = QAction(icon_add, u"Добавить узел на ребре",
                               self.iface.mainWindow())
        self.act_add.setCheckable(True)
        self.act_add.setToolTip(TOOLTIP_ADD)
        self.act_add.triggered.connect(self._on_add_triggered)

        for act in (self.act_move, self.act_add):
            self.toolbar.addAction(act)
            self.iface.addPluginToVectorMenu(MENU_TITLE, act)

        self.tool_move = TopoMoveTool(self.iface, self.act_move)
        self.tool_add = TopoAddVertexTool(self.iface, self.act_add)

    # ------------------------------------------------------------------
    def _on_move_triggered(self, checked):
        if checked:
            self.act_add.setChecked(False)
            self.iface.mapCanvas().setMapTool(self.tool_move)
        else:
            if self.iface.mapCanvas().mapTool() is self.tool_move:
                self.iface.actionPan().trigger()

    def _on_add_triggered(self, checked):
        if checked:
            self.act_move.setChecked(False)
            self.iface.mapCanvas().setMapTool(self.tool_add)
        else:
            if self.iface.mapCanvas().mapTool() is self.tool_add:
                self.iface.actionPan().trigger()

    # ------------------------------------------------------------------
    def unload(self):
        for act in (self.act_move, self.act_add):
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
        self.tool_add = None
        self.act_move = None
        self.act_add = None
        self.toolbar = None
