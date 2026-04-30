# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.1.2] — 2026-04-29

### Changed
- Refreshed the README screenshots.
- LICENSE file is now bundled **inside** the plugin package (`ShpLassoTool/LICENSE`) so it travels with the plugin into every QGIS profile.

### Note
- Documentation / packaging release only — no change to the plugin's functional code from 1.1.1.

## [1.1.1] — 2026-04-29

### Added
- First public release under the **GNU General Public License v3.0**.
- Repository moved to GitHub: <https://github.com/rfb-studio/shp-lasso-tool>.
- Plugin icon (`icon.png`) and `homepage` / `repository` / `tracker` URLs in plugin metadata, so the QGIS Plugin Manager renders them as clickable links next to the plugin entry.

### Changed
- **Rebranded** from the internal name "Vegetagent QC Lasso Tool" to **Shp Lasso Tool** for the public release. No functional change.

### Removed
- Internal proprietary licence and the time-limited evaluation wrapper used in private demo builds. The open-source build has no trial, no license check, and no time bombs.

## [1.1.0] — internal

### Added
- **Edge Multi-Select** map tool: marquee selection of polygon vertices with white-line highlight, drag-to-translate the chain with stretching boundary edges, Shift-additive selection (green marquee), arrow-key nudge (1 / 10 screen pixels with optional Shift).
- Per-chain coordinate cache and reusable rubber bands for snappy interaction during nudges.
- Debounced layer repaint to coalesce rapid keystrokes into a single redraw.
- Event filter on the canvas to swallow arrow keys while the tool is active so QGIS's default canvas pan-on-arrow does not fight the nudge.
- Shift+drag additive selection mode for the marquee.

### Changed
- Reduced click-to-move hit tolerance from 25 px to 12 px around the selection bounding box for tighter targeting.

### Fixed
- Vertex index drift after translation by removing an unnecessary `makeValid()` call that was reordering ring vertices and causing visible misalignment between the white highlight and the rendered polygon.

## [0.1.0] — internal

### Added
- Initial **Lasso edit** map tool with `add` (union & dissolve into touched features) and `subtract` (difference from touched features) operations on polygon layers.
- Pan via middle-drag or Space+Left, zoom via mouse wheel or `+` / `-` keys; cursor-centred zoom matches QGIS native behaviour.
- Right-click and Ctrl+Left both trigger subtract for keyboard-driven workflows.
- Auto-dissolve of overlapping or adjacent polygons after each `add` operation, so the result is always a single feature.
- New features inherit attributes from a nearby existing feature so they fall into the active layer's renderer category instead of rendering as the default "no match" symbol.
