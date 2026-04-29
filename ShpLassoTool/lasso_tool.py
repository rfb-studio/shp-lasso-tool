from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QColor
from qgis.gui import QgsMapTool, QgsRubberBand
from qgis.core import (
    QgsFeature,
    QgsFeatureRequest,
    QgsGeometry,
    QgsPointXY,
    QgsProject,
    QgsVectorLayer,
    QgsVectorLayerUtils,
    QgsWkbTypes,
)

# QGIS 3.30+ / QGIS 4 expose the new Qgis.GeometryType enum;
# older QGIS 3 still has QgsWkbTypes.PolygonGeometry. Both are int-compatible.
try:
    from qgis.core import Qgis
    _POLYGON_GEOM = Qgis.GeometryType.Polygon
except (ImportError, AttributeError):
    _POLYGON_GEOM = QgsWkbTypes.PolygonGeometry


MIN_VERTICES = 3
SIMPLIFY_TOLERANCE_PX = 1.5  # screen-pixel tolerance for thinning the freehand stroke


class LassoEditTool(QgsMapTool):
    """Freehand lasso edit tool.

    Left-button drag  -> union the lasso polygon into the active layer
                         (any touched features are merged into one).
    Right-button drag -> subtract the lasso polygon from intersecting features.

    The active layer must be a polygon vector layer. Editing is started
    automatically if the layer is not already in edit mode.
    """

    def __init__(self, canvas, iface):
        super().__init__(canvas)
        self.canvas = canvas
        self.iface = iface
        self._rubber = None
        self._points = []
        self._mode = None      # 'add' or 'remove'
        self._drawing = False
        self._panning = False
        self._space_held = False
        self._last_screen_pt = None
        self._last_canvas_pos = None  # for keyboard zoom centering
        self.setCursor(Qt.CursorShape.CrossCursor)

    # ---------- helpers ----------

    def _active_polygon_layer(self):
        layer = self.iface.activeLayer()
        if layer is None:
            self.iface.messageBar().pushWarning(
                "Shp Lasso Tool", "Select a polygon layer in the Layers panel first."
            )
            return None
        if not isinstance(layer, QgsVectorLayer):
            self.iface.messageBar().pushWarning(
                "Shp Lasso Tool", "The active layer is not a vector layer."
            )
            return None
        if int(layer.geometryType()) != int(_POLYGON_GEOM):
            self.iface.messageBar().pushWarning(
                "Shp Lasso Tool", "The active layer is not a polygon layer."
            )
            return None
        return layer

    def _start_rubber(self, color):
        # Defensive: clear any prior rubber band before creating a new one,
        # in case a previous stroke didn't go through normal release cleanup.
        self._clear_rubber()
        self._rubber = QgsRubberBand(self.canvas, _POLYGON_GEOM)
        self._rubber.setColor(color)
        fill = QColor(color)
        fill.setAlpha(60)
        self._rubber.setFillColor(fill)
        self._rubber.setWidth(2)

    def _clear_rubber(self):
        if self._rubber is not None:
            # reset() empties the geometry so the band is invisible immediately;
            # removeItem detaches it from the scene; both are needed because
            # QGraphicsScene can otherwise leave a residual painted item.
            try:
                self._rubber.reset(_POLYGON_GEOM)
            except Exception:
                pass
            try:
                self.canvas.scene().removeItem(self._rubber)
            except Exception:
                pass
            self._rubber = None

    def _update_rubber(self):
        if self._rubber is None or len(self._points) < 2:
            return
        geom = QgsGeometry.fromPolygonXY([self._points])
        self._rubber.setToGeometry(geom, None)

    # ---------- QgsMapTool overrides ----------

    def canvasPressEvent(self, e):
        # Middle button = pan, regardless of layer state.
        if e.button() == Qt.MouseButton.MiddleButton:
            if self._drawing:
                return
            self._panning = True
            self.canvas.setCursor(Qt.CursorShape.ClosedHandCursor)
            return

        # Space + left = pan (Photoshop-style alternative to middle drag).
        if e.button() == Qt.MouseButton.LeftButton and self._space_held:
            if self._drawing:
                return
            self._panning = True
            self.canvas.setCursor(Qt.CursorShape.ClosedHandCursor)
            return

        layer = self._active_polygon_layer()
        if layer is None:
            return

        # ControlModifier on macOS maps to Cmd; MetaModifier maps to physical Ctrl.
        # Accept either so the "Ctrl+left = subtract" alias works for whichever
        # the user actually presses on their platform.
        mods = e.modifiers()
        ctrl_or_cmd = bool(
            mods & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.MetaModifier)
        )

        if e.button() == Qt.MouseButton.LeftButton and ctrl_or_cmd:
            self._mode = "remove"
            color = QColor(200, 0, 0)
        elif e.button() == Qt.MouseButton.LeftButton:
            self._mode = "add"
            color = QColor(0, 170, 0)
        elif e.button() == Qt.MouseButton.RightButton:
            self._mode = "remove"
            color = QColor(200, 0, 0)
        else:
            return

        if not layer.isEditable():
            layer.startEditing()

        map_pt = self.toMapCoordinates(e.pos())
        self._points = [QgsPointXY(map_pt)]
        self._last_screen_pt = e.pos()
        self._start_rubber(color)
        self._drawing = True

    def canvasMoveEvent(self, e):
        self._last_canvas_pos = e.pos()
        if self._panning:
            self.canvas.panAction(e)
            return
        if not self._drawing:
            return
        # Skip pixel-level noise — keep the stroke smooth and the geometry small.
        if self._last_screen_pt is not None:
            dx = e.pos().x() - self._last_screen_pt.x()
            dy = e.pos().y() - self._last_screen_pt.y()
            if (dx * dx + dy * dy) < (SIMPLIFY_TOLERANCE_PX * SIMPLIFY_TOLERANCE_PX):
                return
        self._last_screen_pt = e.pos()
        self._points.append(QgsPointXY(self.toMapCoordinates(e.pos())))
        self._update_rubber()

    def canvasReleaseEvent(self, e):
        # Either a middle-button release (mouse pan) or a left-button release
        # (Space+Left pan) can end the current pan.
        if self._panning and e.button() in (
            Qt.MouseButton.MiddleButton, Qt.MouseButton.LeftButton
        ):
            self.canvas.panActionEnd(e.pos())
            self._panning = False
            # If Space is still held, keep the open-hand hint; otherwise crosshair.
            self.canvas.setCursor(
                Qt.CursorShape.OpenHandCursor if self._space_held
                else Qt.CursorShape.CrossCursor
            )
            return
        if not self._drawing:
            return
        self._drawing = False

        try:
            if len(self._points) < MIN_VERTICES:
                return
            ring = list(self._points)
            if ring[0] != ring[-1]:
                ring.append(ring[0])
            lasso = QgsGeometry.fromPolygonXY([ring])
            if lasso.isEmpty():
                return
            # Self-intersecting freehand strokes are common; makeValid fixes them.
            lasso = lasso.makeValid()
            if lasso.isEmpty():
                return

            layer = self._active_polygon_layer()
            if layer is None:
                return
            self._apply(layer, lasso)
        finally:
            self._clear_rubber()
            self._points = []
            self._last_screen_pt = None
            self._mode = None

    def wheelEvent(self, e):
        # Zoom centered on the cursor. Don't fight an in-progress stroke.
        if self._drawing or self._panning:
            e.accept()
            return
        delta = e.angleDelta().y()
        if delta == 0:
            e.accept()
            return
        factor = self.canvas.zoomInFactor() if delta > 0 else self.canvas.zoomOutFactor()
        # Scale by how far the wheel moved (one notch = 120 units).
        steps = abs(delta) / 120.0
        factor = factor ** steps
        pos = e.position().toPoint() if hasattr(e, "position") else e.pos()
        self.canvas.zoomByFactor(factor, self.toMapCoordinates(pos))
        e.accept()

    def keyPressEvent(self, e):
        key = e.key()
        if key == Qt.Key.Key_Space:
            if not self._space_held:
                self._space_held = True
                # Open-hand hint when ready-to-pan, only if no other gesture is active.
                if not self._drawing and not self._panning:
                    self.canvas.setCursor(Qt.CursorShape.OpenHandCursor)
            e.accept()
            return
        # +/= zoom in, -/_ zoom out, centered on the last known cursor position
        # (matches the wheel behavior so keyboard and wheel feel the same).
        if key in (Qt.Key.Key_Plus, Qt.Key.Key_Equal):
            self._zoom_at_cursor(self.canvas.zoomInFactor())
            e.accept()
            return
        if key in (Qt.Key.Key_Minus, Qt.Key.Key_Underscore):
            self._zoom_at_cursor(self.canvas.zoomOutFactor())
            e.accept()
            return
        super().keyPressEvent(e)

    def keyReleaseEvent(self, e):
        if e.key() == Qt.Key.Key_Space:
            self._space_held = False
            if not self._drawing and not self._panning:
                self.canvas.setCursor(Qt.CursorShape.CrossCursor)
            e.accept()
            return
        super().keyReleaseEvent(e)

    def _zoom_at_cursor(self, factor):
        pos = self._last_canvas_pos if self._last_canvas_pos is not None \
            else self.canvas.rect().center()
        self.canvas.zoomByFactor(factor, self.toMapCoordinates(pos))

    def deactivate(self):
        self._drawing = False
        self._panning = False
        self._space_held = False
        self._clear_rubber()
        self._points = []
        super().deactivate()

    def isZoomTool(self):
        return False

    def isTransient(self):
        return False

    def isEditTool(self):
        return True

    # ---------- edit logic ----------

    def _apply(self, layer, lasso):
        # Reproject the lasso (always in canvas/project CRS) into the layer CRS.
        canvas_crs = self.canvas.mapSettings().destinationCrs()
        layer_crs = layer.crs()
        if canvas_crs != layer_crs:
            from qgis.core import QgsCoordinateTransform
            xform = QgsCoordinateTransform(canvas_crs, layer_crs, QgsProject.instance())
            lasso = QgsGeometry(lasso)
            lasso.transform(xform)

        request = QgsFeatureRequest().setFilterRect(lasso.boundingBox())
        touched = [
            f for f in layer.getFeatures(request)
            if f.hasGeometry() and f.geometry().intersects(lasso)
        ]

        if self._mode == "add":
            self._do_add(layer, lasso, touched)
        elif self._mode == "remove":
            self._do_remove(layer, lasso, touched)

        # Clear any selection QGIS may have left on the edited features so the
        # result renders with the layer's normal symbology, not the selection color.
        layer.removeSelection()
        layer.triggerRepaint()
        # Full canvas refresh evicts any stray rubber-band remnants from the scene.
        self.canvas.refresh()

    def _do_add(self, layer, lasso, touched):
        # Union lasso with every touched feature, then replace them with one merged feature.
        merged = QgsGeometry(lasso)
        keeper_id = None
        keeper_attrs = None
        for f in touched:
            merged = merged.combine(f.geometry())
            if keeper_id is None:
                keeper_id = f.id()
                keeper_attrs = f.attributes()
        merged = merged.makeValid()
        if merged.isEmpty():
            return

        layer.beginEditCommand("Lasso add")
        try:
            if keeper_id is not None:
                # Reuse one existing feature so its attributes survive; delete the rest.
                others = [f.id() for f in touched if f.id() != keeper_id]
                if others:
                    layer.deleteFeatures(others)
                layer.changeGeometry(keeper_id, merged)
            else:
                # No touched feature → copy attributes from any existing feature
                # so the new one falls into the same symbology category (otherwise
                # categorized/rule-based renderers paint it as the "no match" default).
                template_attrs = {}
                for tf in layer.getFeatures(QgsFeatureRequest().setLimit(1)):
                    for i, v in enumerate(tf.attributes()):
                        template_attrs[i] = v
                    break
                # createFeature respects layer defaults and re-generates PK fields.
                feat = QgsVectorLayerUtils.createFeature(layer, merged, template_attrs)
                layer.addFeature(feat)
            layer.endEditCommand()
        except Exception:
            layer.destroyEditCommand()
            raise

    def _do_remove(self, layer, lasso, touched):
        layer.beginEditCommand("Lasso remove")
        try:
            for f in touched:
                new_geom = f.geometry().difference(lasso)
                if new_geom is None or new_geom.isEmpty():
                    layer.deleteFeature(f.id())
                else:
                    new_geom = new_geom.makeValid()
                    if new_geom.isEmpty():
                        layer.deleteFeature(f.id())
                    else:
                        layer.changeGeometry(f.id(), new_geom)
            layer.endEditCommand()
        except Exception:
            layer.destroyEditCommand()
            raise
