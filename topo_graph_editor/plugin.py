from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction, QDoubleSpinBox
from qgis.core import QgsApplication, QgsSettings

import os

from .map_tool import GraphInspectTool
from .vertex_move_tool import VertexMoveTool

SETTINGS_KEY = "topo_graph_editor/tolerance"
DEFAULT_TOLERANCE = 0.01  # в единицах СК слоя (для проекции в метрах - 1 см)
MENU = "&Topo Graph Editor"
ICON_PATH = os.path.join(os.path.dirname(__file__), "icons", "icon.svg")


class TopoGraphEditorPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.canvas = iface.mapCanvas()
        self.spin = None
        self.spin_action = None
        self.insert_action = None
        self.vertex_tool = None
        self.tools = []      # [(action, tool), ...]

    def _plugin_icon(self):
        """Иконка плагина: своя из icons/, с откатом на тему QGIS."""
        if os.path.exists(ICON_PATH):
            return QIcon(ICON_PATH)
        return QIcon(QgsApplication.getThemeIcon("/mActionNodeTool.svg"))

    def _add_tool(self, tool, icon_name, text):
        action = QAction(self._plugin_icon(), text, self.iface.mainWindow())
        action.setCheckable(True)
        action.triggered.connect(lambda _checked=False, t=tool: self.canvas.setMapTool(t))
        tool.setAction(action)  # синхронизирует состояние кнопки с активным инструментом
        self.iface.addToolBarIcon(action)
        self.iface.addPluginToVectorMenu(MENU, action)
        self.tools.append((action, tool))

    def initGui(self):
        self.spin = QDoubleSpinBox()
        self.spin.setDecimals(4)
        self.spin.setRange(0.0001, 1000.0)
        self.spin.setSingleStep(0.01)
        self.spin.setPrefix("допуск: ")
        self.spin.setToolTip("Допуск склейки вершин, единицы СК слоя")
        self.spin.setValue(float(QgsSettings().value(SETTINGS_KEY, DEFAULT_TOLERANCE)))
        self.spin.valueChanged.connect(lambda v: QgsSettings().setValue(SETTINGS_KEY, v))
        self.spin_action = self.iface.addToolBarWidget(self.spin)

        tol = self.spin.value
        self._add_tool(
            GraphInspectTool(self.iface, tol), "/mActionVertexTool.svg", "Показать граф рёбер"
        )
        self.vertex_tool = VertexMoveTool(self.iface, tol)
        self._add_tool(
            self.vertex_tool,
            "/mActionMoveVertex.svg",
            "Перенести вершину (с соседними участками)",
        )

        # переключатель режима "добавить вершину на ребро" для активного инструмента
        self.insert_action = QAction(
            self._plugin_icon(),
            "Режим: добавлять вершину на ребро (Shift+клик тоже работает)",
            self.iface.mainWindow(),
        )
        self.insert_action.setCheckable(True)
        self.insert_action.setToolTip(
            "Включает режим добавления новой вершины: перетаскивание не переносит\n"
            "существующую вершину, а вставляет новую во все участки, проходящие\n"
            "через выбранное ребро. Общий край участков остаётся общим.\n"
            "Тот же режим включается удержанием Shift при клике."
        )
        self.insert_action.toggled.connect(self._on_insert_toggled)
        self.iface.addToolBarIcon(self.insert_action)
        self.iface.addPluginToVectorMenu(MENU, self.insert_action)

    def _on_insert_toggled(self, checked):
        if self.vertex_tool is not None:
            self.vertex_tool.set_insert_mode(checked)

    def unload(self):
        for action, tool in self.tools:
            tool.clear()
            if self.canvas.mapTool() is tool:
                self.canvas.unsetMapTool(tool)
            self.iface.removePluginVectorMenu(MENU, action)
            self.iface.removeToolBarIcon(action)
        self.tools = []
        if self.insert_action is not None:
            self.iface.removePluginVectorMenu(MENU, self.insert_action)
            self.iface.removeToolBarIcon(self.insert_action)
            self.insert_action = None
        if self.spin_action is not None:
            self.iface.removeToolBarIcon(self.spin_action)
        self.vertex_tool = None
        self.spin = self.spin_action = None
