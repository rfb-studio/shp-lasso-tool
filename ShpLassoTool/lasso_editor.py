from qgis.PyQt.QtCore import Qt, QPoint, QSize
from qgis.PyQt.QtGui import QIcon, QPixmap, QPainter, QColor, QPen, QPolygon

# QAction lives in QtWidgets under Qt5 (QGIS 3) and in QtGui under Qt6 (QGIS 4).
try:
    from qgis.PyQt.QtGui import QAction
except ImportError:
    from qgis.PyQt.QtWidgets import QAction

from .lasso_tool import LassoEditTool
from .edge_select_tool import EdgeSelectTool


_MENU = "&Shp Lasso Tool"


def _build_lasso_icon():
    """Green dashed lasso-octagon — drawn at runtime so the plugin ships
    without a PNG asset."""
    pix = QPixmap(QSize(24, 24))
    pix.fill(QColor(0, 0, 0, 0))
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(QColor(20, 120, 20))
    pen.setWidth(2)
    pen.setStyle(Qt.PenStyle.DashLine)
    p.setPen(pen)
    pts = QPolygon([
        QPoint(4, 12), QPoint(8, 5), QPoint(15, 4), QPoint(20, 9),
        QPoint(20, 16), QPoint(14, 21), QPoint(7, 19), QPoint(4, 12),
    ])
    p.drawPolyline(pts)
    p.end()
    return QIcon(pix)


def _build_edge_icon():
    """Blue dashed marquee with three white-filled vertex dots — visual cue
    that this tool box-selects vertex chains."""
    pix = QPixmap(QSize(24, 24))
    pix.fill(QColor(0, 0, 0, 0))
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)

    # Marquee rectangle.
    pen = QPen(QColor(40, 80, 200))
    pen.setWidth(2)
    pen.setStyle(Qt.PenStyle.DashLine)
    p.setPen(pen)
    p.drawRect(2, 5, 19, 14)

    # Highlighted vertices on the chain.
    p.setPen(QPen(QColor(20, 20, 20), 1))
    p.setBrush(QColor(255, 255, 255))
    for cx, cy in ((7, 9), (13, 9), (10, 16)):
        p.drawEllipse(QPoint(cx, cy), 2, 2)

    p.end()
    return QIcon(pix)


class ShpLassoTool:
    """
    Plugin entry point. Registers two toolbar actions:
      * Lasso edit (add / subtract polygon area, auto-dissolve)
      * Edge multi-select (marquee a vertex chain, drag to translate)

    Only one of the two map tools is active at a time; QGIS's
    ``QgsMapTool.setAction`` plumbing keeps each action's checkbox state
    in sync with whether its tool is the canvas's active tool, so
    switching between them via toolbar clicks Just Works.
    """

    def __init__(self, iface):
        self.iface = iface
        self.canvas = iface.mapCanvas()
        self.lasso_action = None
        self.edge_action = None
        self.lasso_tool = None
        self.edge_tool = None
        self._prev_tool = None

    def initGui(self):
        # ---- Lasso edit button ----
        self.lasso_action = QAction(
            _build_lasso_icon(), "Shp Lasso Tool", self.iface.mainWindow()
        )
        self.lasso_action.setCheckable(True)
        self.lasso_action.setToolTip(
            "<b>Shp Lasso Tool</b><br>"
            "Lasso polygon editor — left-drag: add &amp; dissolve, "
            "right-drag (or Ctrl+left): subtract, "
            "middle-drag (or Space+left): pan, wheel (or +/-): zoom."
        )
        self.lasso_action.setStatusTip(
            "Shp Lasso Tool: lasso add/subtract editor for polygon layers."
        )
        self.lasso_action.triggered.connect(self._toggle_lasso)
        self.iface.addToolBarIcon(self.lasso_action)
        self.iface.addPluginToVectorMenu(_MENU, self.lasso_action)
        self.lasso_tool = LassoEditTool(self.canvas, self.iface)
        self.lasso_tool.setAction(self.lasso_action)

        # ---- Edge multi-select button ----
        self.edge_action = QAction(
            _build_edge_icon(),
            "Shp Lasso Tool — Edge Multi-Select",
            self.iface.mainWindow(),
        )
        self.edge_action.setCheckable(True)
        self.edge_action.setToolTip(
            "<b>Shp Lasso Tool — Edge Multi-Select</b><br>"
            "Drag a rectangle to highlight a chain of polygon vertices in "
            "white (hold <b>Shift</b> while dragging to add to the existing "
            "selection). Drag the chain to translate it. Boundary edges "
            "stretch to stay connected. Arrow keys nudge by 1 screen pixel "
            "(Shift+Arrow nudges by 10). Esc clears the selection."
        )
        self.edge_action.setStatusTip(
            "Shp Lasso Tool — Edge Multi-Select: rectangle-select polygon "
            "vertex chains and translate them as a group."
        )
        self.edge_action.triggered.connect(self._toggle_edge)
        self.iface.addToolBarIcon(self.edge_action)
        self.iface.addPluginToVectorMenu(_MENU, self.edge_action)
        self.edge_tool = EdgeSelectTool(self.canvas, self.iface)
        self.edge_tool.setAction(self.edge_action)

    def unload(self):
        for tool in (self.lasso_tool, self.edge_tool):
            if tool is not None and self.canvas.mapTool() is tool:
                self.canvas.unsetMapTool(tool)
        for action in (self.lasso_action, self.edge_action):
            if action is not None:
                self.iface.removePluginVectorMenu(_MENU, action)
                self.iface.removeToolBarIcon(action)
        self.lasso_action = None
        self.edge_action = None
        self.lasso_tool = None
        self.edge_tool = None

    # ------------------------------------------------------------------
    # Toggle slots
    # ------------------------------------------------------------------

    def _toggle_lasso(self, checked):
        if checked:
            self._prev_tool = self.canvas.mapTool()
            self.canvas.setMapTool(self.lasso_tool)
        else:
            self.canvas.unsetMapTool(self.lasso_tool)
            if self._prev_tool is not None and self._prev_tool is not self.lasso_tool:
                self.canvas.setMapTool(self._prev_tool)
            self._prev_tool = None

    def _toggle_edge(self, checked):
        if checked:
            self._prev_tool = self.canvas.mapTool()
            self.canvas.setMapTool(self.edge_tool)
        else:
            self.canvas.unsetMapTool(self.edge_tool)
            if self._prev_tool is not None and self._prev_tool is not self.edge_tool:
                self.canvas.setMapTool(self._prev_tool)
            self._prev_tool = None
