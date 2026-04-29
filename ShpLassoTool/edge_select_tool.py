"""
Edge Multi-Select tool — drag a rectangle to highlight a chain of polygon
vertices, then drag the highlighted chain to translate it. Unselected
neighbour vertices stay put, so the boundary edges that connect them to
the moved chain stretch automatically.

Pan / zoom shortcuts mirror the lasso tool:
    Middle drag, or Space + Left drag    → pan
    Mouse wheel, or "+" / "-" keys       → zoom (cursor-centred)

Performance notes
    * Selection uses ``QgsGeometry.vertices()`` plus pure-float bbox tests
      (no ``QgsPointXY`` construction in the inner loop).
    * Each selection caches its bounding rectangle so click-vs-selection
      hit testing is O(N_features) box checks rather than O(edges).
    * Entering MOVING precomputes flattened "extended-chain" templates
      (each chain plus its two unselected neighbours, marked sel/non-sel).
      Move-event updates then only translate selected entries and call
      ``QgsRubberBand.setToGeometry`` on already-allocated rubber bands —
      no ``asMultiPolygon()`` copy or ``QgsRubberBand`` re-creation per
      frame, and the live preview is a polyline instead of a filled
      polygon ghost.

Copyright (c) 2026 RFB Studio Ltd. All rights reserved.
"""

from collections import namedtuple

from qgis.PyQt.QtCore import Qt, QEvent, QTimer
from qgis.PyQt.QtGui import QColor

from qgis.core import (
    QgsFeatureRequest,
    QgsGeometry,
    QgsPointXY,
    QgsRectangle,
    QgsVectorLayer,
    QgsWkbTypes,
)
from qgis.gui import QgsMapTool, QgsRubberBand


# Geometry-type constants compatible with both Qt5 (QGIS 3) and Qt6 (QGIS 4).
try:
    _POLYGON_GEOM = QgsWkbTypes.GeometryType.PolygonGeometry
    _LINE_GEOM = QgsWkbTypes.GeometryType.LineGeometry
except AttributeError:
    _POLYGON_GEOM = QgsWkbTypes.PolygonGeometry  # type: ignore
    _LINE_GEOM = QgsWkbTypes.LineGeometry  # type: ignore


# Pixel buffer added around the selection's bounding rectangle when
# deciding whether a click "lands on" the selection. Bigger = more
# forgiving click target, but too big and clicks far from the selection
# also start a move. 12 px is comfortable without feeling like the
# selection swallows half the canvas.
_HIT_TOLERANCE_PX = 12

# Keyboard nudge step sizes, expressed in screen pixels. Multiplied by the
# canvas's current map-units-per-pixel at keypress time so the *visual*
# step stays the same regardless of zoom (1 px on screen always moves the
# chain 1 px on screen, no matter how zoomed in / out the user is).
_NUDGE_PX = 2          # plain arrow key
_NUDGE_PX_SHIFT = 20   # Shift + arrow key

# How long to wait after the last translation (drag finish or arrow nudge)
# before triggering a layer repaint. Shorter = more frequent layer redraws
# (smoother but heavier); longer = jankier with rapid keyboard nudges.
# 50 ms is short enough to feel immediate to the user while still
# coalescing 30 Hz key auto-repeat into a single redraw.
_REPAINT_DEBOUNCE_MS = 50


# Named tuple storage for an active selection entry.
#   fid     — feature id
#   sel_ids — frozenset of absolute vertex ids that are selected
#   geom    — QgsGeometry copy at time of selection (or after a move)
#   bbox    — QgsRectangle covering only the selected vertices (cached
#             so click hit-testing is a constant-time box check)
_SelectedFeat = namedtuple("_SelectedFeat", "fid sel_ids geom bbox")


class EdgeSelectTool(QgsMapTool):
    # State machine —
    #   IDLE      no selection, ready for a new rectangle
    #   RECT      user is currently drawing the marquee rectangle
    #   SELECTED  a vertex chain is highlighted, awaiting move or Esc
    #   MOVING    user is dragging the highlighted chain
    STATE_IDLE = 0
    STATE_RECT = 1
    STATE_SELECTED = 2
    STATE_MOVING = 3

    def __init__(self, canvas, iface):
        super().__init__(canvas)
        self.canvas = canvas
        self.iface = iface
        self._state = self.STATE_IDLE

        # Visual rubber bands.
        self._rect_rubber = None         # marquee selection rectangle
        self._sel_rubbers = []           # white polylines for selected chains (static)
        self._ghost_rubbers = []         # live preview polylines while moving

        # Active selection — list of _SelectedFeat.
        self._selection = []

        # Drag bookkeeping.
        self._rect_start_screen = None
        self._move_start_map = None
        # True while a marquee drag is in additive mode (Shift held at press
        # time). Read by ``_finish_rect`` to decide between merge and replace.
        # Captured at press time so Shift can be released mid-drag without
        # changing the decision the user committed to when they clicked.
        self._additive = False

        # Per-move template cache, populated in ``_start_move`` and reused
        # by every ``_update_move`` until ``_finish_move``. Each entry is
        # a list of (x, y, is_selected) tuples representing one extended
        # chain (selected run + the two unselected neighbour anchors).
        self._move_templates = []

        # Cached chain points (already-built ``QgsPointXY`` lists, one per
        # chain across all selected features). Computed once in
        # ``_rebuild_chain_cache`` and then translated in place by every
        # nudge / drag-finish — this eliminates the per-frame
        # ``_chains_in_geom`` walk that was the dominant cost during
        # rapid arrow-key nudges.
        self._chain_cache = []

        # Debounce timer for ``layer.triggerRepaint``. Calling
        # ``triggerRepaint`` per nudge forces the entire vector layer
        # (which can be expensive for large datasets) to redraw on every
        # keypress. Instead we coalesce: rubber bands update instantly
        # for visual feedback, and the layer redraws once after the user
        # pauses for ``_REPAINT_DEBOUNCE_MS`` milliseconds.
        self._repaint_timer = QTimer()
        self._repaint_timer.setSingleShot(True)
        self._repaint_timer.timeout.connect(self._do_layer_repaint)

        # The layer whose ``geometryChanged`` / ``featureDeleted`` signals
        # we're currently subscribed to. Set when a selection is made,
        # cleared when the selection is cleared or the tool deactivates.
        # Tracking the *original* layer (rather than always re-querying
        # ``iface.activeLayer()``) keeps Ctrl+Z handling correct even if
        # the user changes the active layer after selecting.
        self._signal_layer = None
        # Re-entrancy guard: we set this around our own changeGeometry
        # calls so ``_on_geometry_changed`` skips them and does not redo
        # the redraw work we already did inline.
        self._suppress_handler = False

        # Pan / zoom state — same semantics as LassoEditTool.
        self._panning = False
        self._space_held = False
        self._last_canvas_pos = None

        self.setCursor(Qt.CursorShape.CrossCursor)

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _active_polygon_layer(self):
        layer = self.iface.activeLayer()
        if layer is None:
            self.iface.messageBar().pushWarning(
                "Shp Lasso Tool",
                "Select a polygon layer in the Layers panel first."
            )
            return None
        if not isinstance(layer, QgsVectorLayer):
            self.iface.messageBar().pushWarning(
                "Shp Lasso Tool",
                "The active layer is not a vector layer."
            )
            return None
        if int(layer.geometryType()) != int(_POLYGON_GEOM):
            self.iface.messageBar().pushWarning(
                "Shp Lasso Tool",
                "The active layer is not a polygon layer."
            )
            return None
        return layer

    def _build_translated_geom(self, orig_geom, sel_ids, dx, dy):
        """
        Return a copy of ``orig_geom`` with every vertex whose absolute id is
        in ``sel_ids`` shifted by (dx, dy). Each ring is re-closed by reusing
        the (possibly translated) first vertex, so a ring whose first vertex
        is selected stays watertight. Used at commit time (``_finish_move``);
        the per-frame move preview uses the lighter template path instead.
        """
        is_multi = orig_geom.isMultipart()
        multi = orig_geom.asMultiPolygon() if is_multi else [orig_geom.asPolygon()]

        new_multi = []
        abs_id = 0
        for poly in multi:
            new_poly = []
            for ring in poly:
                ring_len = len(ring)
                if ring_len < 2:
                    new_poly.append([QgsPointXY(p.x(), p.y()) for p in ring])
                    abs_id += ring_len
                    continue
                new_ring = []
                first_new = None
                for i, pt in enumerate(ring[:-1]):
                    if abs_id in sel_ids:
                        new_pt = QgsPointXY(pt.x() + dx, pt.y() + dy)
                    else:
                        new_pt = QgsPointXY(pt.x(), pt.y())
                    new_ring.append(new_pt)
                    if i == 0:
                        first_new = new_pt
                    abs_id += 1
                abs_id += 1  # account for the closing-duplicate slot
                if first_new is not None:
                    new_ring.append(QgsPointXY(first_new.x(), first_new.y()))
                new_poly.append(new_ring)
            new_multi.append(new_poly)

        if is_multi:
            return QgsGeometry.fromMultiPolygonXY(new_multi)
        return QgsGeometry.fromPolygonXY(new_multi[0])

    def _compute_sel_bbox(self, geom, sel_ids):
        """Bounding rect that covers ONLY the selected vertices, or None
        if the selection is empty. Used for fast click-hit testing."""
        min_x = float("inf")
        min_y = float("inf")
        max_x = float("-inf")
        max_y = float("-inf")
        abs_id = 0
        for v in geom.vertices():
            if abs_id in sel_ids:
                x = v.x()
                y = v.y()
                if x < min_x:
                    min_x = x
                if x > max_x:
                    max_x = x
                if y < min_y:
                    min_y = y
                if y > max_y:
                    max_y = y
            abs_id += 1
        if min_x == float("inf"):
            return None
        return QgsRectangle(min_x, min_y, max_x, max_y)

    # ------------------------------------------------------------------
    # layer-signal plumbing — keeps cached selection state in sync with
    # the underlying layer, especially for Ctrl+Z / Ctrl+Shift+Z.
    # ------------------------------------------------------------------

    def _connect_layer(self, layer):
        if self._signal_layer is layer:
            return
        self._disconnect_layer()
        if layer is None:
            return
        try:
            layer.geometryChanged.connect(self._on_geometry_changed)
            layer.featureDeleted.connect(self._on_feature_deleted)
        except Exception:
            return
        self._signal_layer = layer

    def _disconnect_layer(self):
        if self._signal_layer is None:
            return
        try:
            self._signal_layer.geometryChanged.disconnect(self._on_geometry_changed)
        except Exception:
            pass
        try:
            self._signal_layer.featureDeleted.disconnect(self._on_feature_deleted)
        except Exception:
            pass
        self._signal_layer = None

    def _on_geometry_changed(self, fid, geom):
        # Skip while the user is mid-drag (the live preview is authoritative)
        # and while we're applying our own changeGeometry calls (we'll redraw
        # ourselves once at the end, no need to do it per feature).
        if self._suppress_handler or self._state == self.STATE_MOVING:
            return
        if not self._selection:
            return
        changed = False
        for i, entry in enumerate(self._selection):
            if entry.fid != fid:
                continue
            geom_copy = QgsGeometry(geom)
            # Vertex count can shift if external code edits the same
            # feature. Drop selected ids that no longer exist.
            total = 0
            for _ in geom_copy.vertices():
                total += 1
            valid_ids = frozenset(i_ for i_ in entry.sel_ids if i_ < total)
            if not valid_ids:
                continue
            bbox = self._compute_sel_bbox(geom_copy, valid_ids)
            self._selection[i] = entry._replace(
                sel_ids=valid_ids, geom=geom_copy, bbox=bbox
            )
            changed = True
        if changed:
            # Cache mirrored from layer state — rebuild from the fresh geom.
            self._rebuild_chain_cache()
            if self._state == self.STATE_SELECTED:
                self._draw_selection_highlight()

    def _on_feature_deleted(self, fid):
        if self._suppress_handler or self._state == self.STATE_MOVING:
            return
        before = len(self._selection)
        self._selection = [e for e in self._selection if e.fid != fid]
        if len(self._selection) == before:
            return
        if not self._selection:
            self._clear_selection()
        else:
            self._rebuild_chain_cache()
            if self._state == self.STATE_SELECTED:
                self._draw_selection_highlight()

    def _chains_in_geom(self, geom, sel_ids):
        """
        Return a list of "inner" chains — each chain is a list of
        QgsPointXY of consecutively-selected distinct vertices in a single
        ring (rings are cyclic, so a chain may wrap the seam). Used to
        render the static white highlight after a selection is made.
        """
        is_multi = geom.isMultipart()
        multi = geom.asMultiPolygon() if is_multi else [geom.asPolygon()]
        chains = []
        abs_id = 0
        for poly in multi:
            for ring in poly:
                ring_len = len(ring)
                if ring_len < 2:
                    abs_id += ring_len
                    continue
                distinct = ring_len - 1
                ring_sel = [(abs_id + i) in sel_ids for i in range(distinct)]
                ring_pts = [QgsPointXY(p.x(), p.y()) for p in ring[:-1]]

                n_sel = sum(ring_sel)
                if n_sel == 0:
                    abs_id += ring_len
                    continue
                if n_sel == distinct:
                    chains.append(list(ring_pts) + [ring_pts[0]])
                    abs_id += ring_len
                    continue

                start = 0
                for i in range(distinct):
                    if not ring_sel[i]:
                        start = (i + 1) % distinct
                        break

                current = []
                for k in range(distinct):
                    idx = (start + k) % distinct
                    if ring_sel[idx]:
                        current.append(ring_pts[idx])
                    else:
                        if current:
                            chains.append(current)
                            current = []
                if current:
                    chains.append(current)

                abs_id += ring_len
        return chains

    def _build_move_templates(self):
        """
        Per-chain "extended" templates that drive the live move preview.
        Each template is a list of ``(x, y, is_selected)`` tuples covering
        one selected run plus, when present, the two unselected neighbour
        vertices that anchor the boundary edges. During ``_update_move``
        the selected entries are translated by the current delta and the
        unselected entries stay put — that's exactly how the user sees
        the boundary edges stretching.

        Computed once in ``_start_move`` from the cached per-feature ring
        layouts, so per-frame work stays O(total chain vertices) rather
        than O(total polygon vertices).
        """
        templates = []
        for entry in self._selection:
            geom = entry.geom
            sel_ids = entry.sel_ids
            is_multi = geom.isMultipart()
            multi = geom.asMultiPolygon() if is_multi else [geom.asPolygon()]
            abs_id = 0
            for poly in multi:
                for ring in poly:
                    ring_len = len(ring)
                    if ring_len < 2:
                        abs_id += ring_len
                        continue
                    distinct = ring_len - 1
                    ring_sel = [(abs_id + i) in sel_ids for i in range(distinct)]
                    ring_xy = [(ring[i].x(), ring[i].y()) for i in range(distinct)]

                    n_sel = sum(ring_sel)
                    if n_sel == 0:
                        abs_id += ring_len
                        continue
                    if n_sel == distinct:
                        # Whole ring → one closed loop, every entry selected.
                        tpl = [(x, y, True) for x, y in ring_xy]
                        tpl.append((ring_xy[0][0], ring_xy[0][1], True))
                        templates.append(tpl)
                        abs_id += ring_len
                        continue

                    # Walk one full lap starting after the first unselected
                    # vertex, so chains that wrap the seam get one
                    # contiguous template instead of two halves.
                    start = 0
                    for i in range(distinct):
                        if not ring_sel[i]:
                            start = (i + 1) % distinct
                            break
                    current = None
                    prev_unsel_xy = None
                    for k in range(distinct):
                        idx = (start + k) % distinct
                        sel = ring_sel[idx]
                        x, y = ring_xy[idx]
                        if sel:
                            if current is None:
                                current = []
                                if prev_unsel_xy is not None:
                                    current.append(
                                        (prev_unsel_xy[0], prev_unsel_xy[1], False)
                                    )
                            current.append((x, y, True))
                        else:
                            if current is not None:
                                current.append((x, y, False))
                                templates.append(current)
                                current = None
                            prev_unsel_xy = (x, y)
                    # Defensive: shouldn't happen because we start after
                    # an unselected vertex, but keep any open chain.
                    if current is not None:
                        templates.append(current)

                    abs_id += ring_len
        return templates

    # ------------------------------------------------------------------
    # chain-cache + debounced repaint
    # ------------------------------------------------------------------

    def _rebuild_chain_cache(self):
        """One-shot rebuild of the per-chain ``QgsPointXY`` cache. Called
        on a fresh selection or after an external geometry change (Ctrl+Z)
        — never on the per-keypress nudge path."""
        self._chain_cache = []
        for entry in self._selection:
            for chain in self._chains_in_geom(entry.geom, entry.sel_ids):
                if len(chain) >= 2:
                    self._chain_cache.append(list(chain))

    def _translate_chain_cache(self, dx, dy):
        """In-place ``(dx, dy)`` shift of every cached chain point. All
        points in ``_chain_cache`` are by construction selected vertices,
        so the translation is uniform — much cheaper than recomputing
        chains from scratch."""
        if dx == 0 and dy == 0:
            return
        for i, pts in enumerate(self._chain_cache):
            self._chain_cache[i] = [
                QgsPointXY(p.x() + dx, p.y() + dy) for p in pts
            ]

    def _do_layer_repaint(self):
        if self._signal_layer is None:
            return
        try:
            self._signal_layer.triggerRepaint()
        except Exception:
            pass

    def _schedule_layer_repaint(self):
        self._repaint_timer.start(_REPAINT_DEBOUNCE_MS)

    def _cancel_layer_repaint(self):
        self._repaint_timer.stop()

    # ------------------------------------------------------------------
    # rubber-band lifecycle
    # ------------------------------------------------------------------

    def _make_rubber(self, geom_type):
        return QgsRubberBand(self.canvas, geom_type)

    def _drop(self, rb, geom_type):
        if rb is None:
            return
        try:
            rb.reset(geom_type)
        except Exception:
            pass
        try:
            self.canvas.scene().removeItem(rb)
        except Exception:
            pass

    def _clear_rect_rubber(self):
        self._drop(self._rect_rubber, _POLYGON_GEOM)
        self._rect_rubber = None

    def _clear_sel_rubbers(self):
        for rb in self._sel_rubbers:
            self._drop(rb, _LINE_GEOM)
        self._sel_rubbers = []

    def _clear_ghost_rubbers(self):
        for rb in self._ghost_rubbers:
            self._drop(rb, _LINE_GEOM)
        self._ghost_rubbers = []

    def _clear_selection(self):
        self._clear_sel_rubbers()
        self._clear_ghost_rubbers()
        self._selection = []
        self._move_templates = []
        self._chain_cache = []
        self._cancel_layer_repaint()
        self._disconnect_layer()
        self._state = self.STATE_IDLE

    # ------------------------------------------------------------------
    # marquee rectangle
    # ------------------------------------------------------------------

    def _start_rect(self, screen_pt):
        self._rect_start_screen = screen_pt
        self._clear_rect_rubber()
        self._rect_rubber = self._make_rubber(_POLYGON_GEOM)
        # Visual cue: blue marquee = replace selection, green = additive
        # (Shift held at press time). Mirrors the convention in many
        # graphics tools where a "+" tint signals adding to a selection.
        col = QColor(40, 160, 60) if self._additive else QColor(50, 80, 200)
        self._rect_rubber.setColor(col)
        fill = QColor(col)
        fill.setAlpha(40)
        self._rect_rubber.setFillColor(fill)
        self._rect_rubber.setWidth(1)
        self._state = self.STATE_RECT

    def _update_rect(self, screen_pt):
        if self._rect_rubber is None or self._rect_start_screen is None:
            return
        p1 = self.toMapCoordinates(self._rect_start_screen)
        p2 = self.toMapCoordinates(screen_pt)
        ring = [
            QgsPointXY(p1.x(), p1.y()),
            QgsPointXY(p2.x(), p1.y()),
            QgsPointXY(p2.x(), p2.y()),
            QgsPointXY(p1.x(), p2.y()),
            QgsPointXY(p1.x(), p1.y()),
        ]
        self._rect_rubber.setToGeometry(QgsGeometry.fromPolygonXY([ring]), None)

    def _finish_rect(self, screen_pt):
        layer = self._active_polygon_layer()
        if self._rect_start_screen is None or layer is None:
            self._clear_rect_rubber()
            self._rect_start_screen = None
            self._state = self.STATE_IDLE
            return

        p1 = self.toMapCoordinates(self._rect_start_screen)
        p2 = self.toMapCoordinates(screen_pt)
        rect_map = QgsRectangle(p1, p2)
        rect_map.normalize()
        self._clear_rect_rubber()
        self._rect_start_screen = None

        if rect_map.width() == 0 and rect_map.height() == 0:
            self._state = self.STATE_IDLE
            return

        # Pre-extract bbox edges as floats — avoids per-vertex method calls
        # in the hot loop (~3-4x faster than rect_map.contains(QgsPointXY)
        # for big polygons).
        xmn = rect_map.xMinimum()
        xmx = rect_map.xMaximum()
        ymn = rect_map.yMinimum()
        ymx = rect_map.yMaximum()

        new_selection = []
        request = QgsFeatureRequest().setFilterRect(rect_map)
        for f in layer.getFeatures(request):
            geom = f.geometry()
            if geom is None or geom.isEmpty():
                continue
            sel_ids = set()
            abs_id = 0
            for v in geom.vertices():
                x = v.x()
                y = v.y()
                if xmn <= x <= xmx and ymn <= y <= ymx:
                    sel_ids.add(abs_id)
                abs_id += 1
            if not sel_ids:
                continue
            sel_ids = frozenset(sel_ids)
            geom_copy = QgsGeometry(geom)
            bbox = self._compute_sel_bbox(geom_copy, sel_ids)
            new_selection.append(_SelectedFeat(f.id(), sel_ids, geom_copy, bbox))

        additive = self._additive
        self._additive = False  # consume the press-time flag

        if not new_selection:
            # Empty marquee. In additive mode we keep whatever we already
            # have; otherwise the user gets the "selection cleared" state
            # they implicitly asked for by clicking off-selection.
            if additive and self._selection:
                self._state = self.STATE_SELECTED
            else:
                self._state = self.STATE_IDLE
            return

        if additive and self._selection:
            # Merge: union sel_ids per feature, keep features that aren't
            # touched by the new marquee, prefer the *new* (current-state)
            # geometry copy over any stale cached one.
            existing_by_fid = {entry.fid: entry for entry in self._selection}
            merged = []
            for new_entry in new_selection:
                old = existing_by_fid.pop(new_entry.fid, None)
                if old is None:
                    merged.append(new_entry)
                    continue
                merged_ids = frozenset(new_entry.sel_ids | old.sel_ids)
                bbox = self._compute_sel_bbox(new_entry.geom, merged_ids)
                merged.append(
                    _SelectedFeat(new_entry.fid, merged_ids, new_entry.geom, bbox)
                )
            # Existing features the new rectangle did not touch survive
            # untouched.
            merged.extend(existing_by_fid.values())
            self._selection = merged
        else:
            self._selection = new_selection

        # Subscribe to this layer's signals so undo / redo / external
        # geometry edits keep the cached selection in sync.
        self._connect_layer(layer)
        self._rebuild_chain_cache()
        self._draw_selection_highlight()
        self._state = self.STATE_SELECTED

    def _draw_selection_highlight(self):
        """Draw white polylines for every cached chain. Reuses existing
        rubber bands instead of recreating them, and pulls coordinates
        from ``self._chain_cache`` instead of walking ``asMultiPolygon()``
        each time — both critical for snappy arrow-key nudges."""
        chains = self._chain_cache
        white = QColor(255, 255, 255)
        while len(self._sel_rubbers) < len(chains):
            rb = self._make_rubber(_LINE_GEOM)
            rb.setColor(white)
            rb.setWidth(4)
            self._sel_rubbers.append(rb)
        while len(self._sel_rubbers) > len(chains):
            self._drop(self._sel_rubbers.pop(), _LINE_GEOM)
        for rb, pts in zip(self._sel_rubbers, chains):
            rb.setToGeometry(QgsGeometry.fromPolylineXY(pts), None)

    def _click_hits_selection(self, screen_pt):
        """True if a click at ``screen_pt`` falls within the bounding
        rectangle of the selected vertices, expanded by the per-tool
        pixel tolerance. Constant-time per feature."""
        if not self._selection:
            return False
        click_map = self.toMapCoordinates(screen_pt)
        cx = click_map.x()
        cy = click_map.y()
        tol = _HIT_TOLERANCE_PX * self.canvas.mapUnitsPerPixel()
        for entry in self._selection:
            bb = entry.bbox
            if bb is None:
                continue
            if (bb.xMinimum() - tol <= cx <= bb.xMaximum() + tol and
                    bb.yMinimum() - tol <= cy <= bb.yMaximum() + tol):
                return True
        return False

    # ------------------------------------------------------------------
    # move
    # ------------------------------------------------------------------

    def _start_move(self, map_pt):
        self._move_start_map = map_pt
        self._state = self.STATE_MOVING

        # Hide the static white-segment markers so they don't double-draw
        # underneath the live preview.
        self._clear_sel_rubbers()

        # Precompute extended-chain templates — flattened, all-Python data.
        self._move_templates = self._build_move_templates()

        # Pre-allocate one polyline rubber band per chain. We re-use these
        # across every move event, which is much cheaper than recreating
        # QgsRubberBand objects from scratch.
        self._clear_ghost_rubbers()
        self._ghost_rubbers = []
        white = QColor(255, 255, 255)
        for _ in self._move_templates:
            rb = self._make_rubber(_LINE_GEOM)
            rb.setColor(white)
            rb.setWidth(3)
            self._ghost_rubbers.append(rb)

        # Initial render at delta=0 so the chain shows immediately.
        self._update_move(map_pt)

    def _update_move(self, map_pt):
        if self._move_start_map is None or not self._move_templates:
            return
        dx = map_pt.x() - self._move_start_map.x()
        dy = map_pt.y() - self._move_start_map.y()
        for tpl, rb in zip(self._move_templates, self._ghost_rubbers):
            pts = [
                QgsPointXY(x + dx, y + dy) if sel else QgsPointXY(x, y)
                for (x, y, sel) in tpl
            ]
            rb.setToGeometry(QgsGeometry.fromPolylineXY(pts), None)

    def _finish_move(self, map_pt):
        if self._move_start_map is None:
            self._clear_ghost_rubbers()
            self._draw_selection_highlight()
            self._state = self.STATE_SELECTED
            return

        dx = map_pt.x() - self._move_start_map.x()
        dy = map_pt.y() - self._move_start_map.y()
        self._move_start_map = None
        self._move_templates = []
        self._clear_ghost_rubbers()

        if dx == 0 and dy == 0 or not self._selection:
            self._draw_selection_highlight()
            self._state = self.STATE_SELECTED
            return

        self._apply_translation(dx, dy, "Edge Multi-Select translate")
        self._state = self.STATE_SELECTED

    def _apply_translation(self, dx, dy, command_name):
        """Translate every selected vertex by (dx, dy), commit as one undo
        command, refresh cached selection state, and redraw the highlight.
        Used by both the drag-finish path and the keyboard-arrow nudge.

        Crucial details:
          * Builds each translated geometry **once** per feature and reuses
            it for both ``layer.changeGeometry`` and the cache update.
          * No ``canvas.refresh()`` — that's a synchronous full-canvas
            re-render and was the single biggest stall on mouse release.
            ``triggerRepaint()`` queues an async redraw of the affected
            layer only.
          * **No ``makeValid()`** — that call is what made arrow-key
            nudges feel sluggish AND caused the highlight to drift away
            from the rendered polygon. ``makeValid`` may rotate ring
            start vertices, merge close-by points, or reorder parts even
            for already-valid geometries; any of those breaks the
            ``abs_id`` ↔ vertex mapping that ``sel_ids`` is built around,
            so the next ``_chains_in_geom`` walk picks the wrong vertices
            for the white overlay. A pure translation cannot turn a valid
            polygon invalid, so makeValid was both expensive and harmful.
        """
        layer = self._signal_layer
        if layer is None or not self._selection:
            return
        if not layer.isEditable():
            layer.startEditing()

        refreshed = []
        self._suppress_handler = True
        try:
            layer.beginEditCommand(command_name)
            try:
                for entry in self._selection:
                    new_geom = self._build_translated_geom(
                        entry.geom, entry.sel_ids, dx, dy
                    )
                    if new_geom is None or new_geom.isEmpty():
                        refreshed.append(entry)
                        continue
                    layer.changeGeometry(entry.fid, new_geom)
                    bbox = self._compute_sel_bbox(new_geom, entry.sel_ids)
                    refreshed.append(
                        _SelectedFeat(entry.fid, entry.sel_ids, new_geom, bbox)
                    )
                layer.endEditCommand()
            except Exception:
                layer.destroyEditCommand()
                raise
        finally:
            self._suppress_handler = False

        self._selection = refreshed
        # Translate the cached chain points (cheap), redraw rubber bands
        # immediately, and defer the layer repaint via the debounce timer
        # so rapid arrow-key nudges don't queue dozens of redraws.
        self._translate_chain_cache(dx, dy)
        self._draw_selection_highlight()
        self._schedule_layer_repaint()

    def _nudge(self, dx, dy):
        """Keyboard-driven version of a drag move: arrow keys translate
        the selected chain in screen-pixel-equivalent map units."""
        if not self._selection or self._signal_layer is None:
            return
        self._apply_translation(dx, dy, "Edge Multi-Select nudge")

    # ------------------------------------------------------------------
    # QgsMapTool overrides
    # ------------------------------------------------------------------

    def canvasPressEvent(self, e):
        # Pan via middle drag, or Space + Left drag.
        if e.button() == Qt.MouseButton.MiddleButton or (
            e.button() == Qt.MouseButton.LeftButton and self._space_held
        ):
            if self._state in (self.STATE_RECT, self.STATE_MOVING):
                return
            self._panning = True
            self.canvas.setCursor(Qt.CursorShape.ClosedHandCursor)
            return

        if e.button() != Qt.MouseButton.LeftButton:
            return

        shift = bool(e.modifiers() & Qt.KeyboardModifier.ShiftModifier)

        # Click inside the selection bbox → start moving (only when Shift
        # is *not* held; Shift always means "make a new marquee, additive").
        if (
            not shift
            and self._state == self.STATE_SELECTED
            and self._click_hits_selection(e.pos())
        ):
            self._start_move(self.toMapCoordinates(e.pos()))
            return

        # Otherwise → start a marquee. Without Shift we drop the previous
        # selection first; with Shift we keep it and merge in ``_finish_rect``.
        if not shift:
            self._clear_selection()
        self._additive = shift
        self._start_rect(e.pos())

    def canvasMoveEvent(self, e):
        self._last_canvas_pos = e.pos()
        if self._panning:
            self.canvas.panAction(e)
            return
        if self._state == self.STATE_RECT:
            self._update_rect(e.pos())
        elif self._state == self.STATE_MOVING:
            self._update_move(self.toMapCoordinates(e.pos()))

    def canvasReleaseEvent(self, e):
        if self._panning and e.button() in (
            Qt.MouseButton.MiddleButton, Qt.MouseButton.LeftButton
        ):
            self.canvas.panActionEnd(e.pos())
            self._panning = False
            self.canvas.setCursor(
                Qt.CursorShape.OpenHandCursor if self._space_held
                else Qt.CursorShape.CrossCursor
            )
            return

        if e.button() != Qt.MouseButton.LeftButton:
            return

        if self._state == self.STATE_RECT:
            self._finish_rect(e.pos())
        elif self._state == self.STATE_MOVING:
            self._finish_move(self.toMapCoordinates(e.pos()))

    # Arrow keys are handled in ``eventFilter`` instead of ``keyPressEvent``
    # because QGIS's ``QgsMapCanvas`` handles arrow-key panning at the
    # widget level *before* it dispatches to the active map tool's
    # ``keyPressEvent``. A ``QObject`` event filter installed on the
    # canvas runs ahead of that, so we can swallow the event entirely
    # and the canvas never sees it (= no spurious panning).

    def eventFilter(self, obj, event):
        # Only intervene while this tool is the active map tool. With the
        # filter installed on the canvas, this gate is necessary because
        # the canvas shows up as ``obj`` for every kind of event regardless
        # of which tool is in use.
        if self.canvas.mapTool() is not self:
            return False
        try:
            etype = event.type()
        except RuntimeError:
            return False
        if etype != QEvent.Type.KeyPress:
            return False
        key = event.key()
        if key not in (
            Qt.Key.Key_Left, Qt.Key.Key_Right,
            Qt.Key.Key_Up, Qt.Key.Key_Down,
        ):
            return False
        # Always swallow arrow keys (whether or not we have a selection)
        # so the canvas's pan-on-arrow handler never fires while this
        # tool is active. We additionally nudge if a selection exists.
        if self._state == self.STATE_SELECTED:
            big = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
            step_px = _NUDGE_PX_SHIFT if big else _NUDGE_PX
            step_map = step_px * self.canvas.mapUnitsPerPixel()
            if key == Qt.Key.Key_Left:
                self._nudge(-step_map, 0)
            elif key == Qt.Key.Key_Right:
                self._nudge(step_map, 0)
            elif key == Qt.Key.Key_Up:
                # QGIS map Y axis goes up.
                self._nudge(0, step_map)
            else:  # Key_Down
                self._nudge(0, -step_map)
        return True  # consume — do NOT let the canvas pan

    def keyPressEvent(self, e):
        key = e.key()
        if key == Qt.Key.Key_Escape:
            self._clear_selection()
            e.accept()
            return
        if key == Qt.Key.Key_Space:
            if not self._space_held:
                self._space_held = True
                if self._state in (self.STATE_IDLE, self.STATE_SELECTED) and not self._panning:
                    self.canvas.setCursor(Qt.CursorShape.OpenHandCursor)
            e.accept()
            return
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
            if not self._panning:
                self.canvas.setCursor(Qt.CursorShape.CrossCursor)
            e.accept()
            return
        super().keyReleaseEvent(e)

    def _zoom_at_cursor(self, factor):
        pos = self._last_canvas_pos if self._last_canvas_pos is not None \
            else self.canvas.rect().center()
        self.canvas.zoomByFactor(factor, self.toMapCoordinates(pos))

    def activate(self):
        super().activate()
        # Intercept arrow-key panning at the canvas level — see
        # ``eventFilter`` for why this is necessary.
        try:
            self.canvas.installEventFilter(self)
        except Exception:
            pass

    def deactivate(self):
        try:
            self.canvas.removeEventFilter(self)
        except Exception:
            pass
        self._cancel_layer_repaint()
        self._clear_selection()
        self._clear_rect_rubber()
        self._disconnect_layer()
        self._panning = False
        self._space_held = False
        super().deactivate()
