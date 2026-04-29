# Contributing to Shp Lasso Tool

Thanks for your interest in improving this plugin. This is a small, focused codebase — contributions of any size (typo fixes, doc tweaks, bug reports, full features) are welcome.

## Reporting issues

Please file bugs and feature requests on the [GitHub issue tracker](https://github.com/rfb-studio/shp-lasso-tool/issues).

A useful bug report includes:
- QGIS version (e.g. `3.34.5` / `4.0.1`) and OS
- A short description of what you did, what you expected, and what actually happened
- Console output if QGIS printed any Python errors (Plugins → Python Console)
- A small example layer / project where the bug reproduces, if possible

## Development setup

```bash
git clone https://github.com/rfb-studio/shp-lasso-tool
cd shp-lasso-tool
bash install/install.sh    # macOS / Linux — copies plugin into your QGIS profile
```

After every code edit:
1. Quit QGIS completely (`Cmd+Q` on macOS) and reopen, **or**
2. Use the [Plugin Reloader](https://plugins.qgis.org/plugins/plugin_reloader/) plugin to skip the restart

The install script auto-detects every QGIS profile under your user data directory (3.x and 4.x both supported) and copies the plugin into each.

## Code style

- **Python 3.9+** — the plugin must run on every Python version QGIS bundles, so don't use 3.10+ syntax (no `match`/`case`, no `X | Y` type unions in runtime code).
- Standard library only — no third-party `pip` dependencies. Keep the install footprint minimal so users can drop in the source folder without `pip install` steps.
- 4-space indent, double-quoted strings, comments in English.
- For Qt enum access, prefer the **scoped form** (`Qt.Key.Key_Up`, `Qt.MouseButton.LeftButton`) — it works in both PyQt5 (QGIS 3) and PyQt6 (QGIS 4).
- Keep the two map tools' user gestures consistent: middle-drag / Space+Left = pan, wheel / `+` / `-` = zoom.

## Pull requests

- Branch off `main` with a descriptive name (`feat/snap-to-vertex`, `fix/move-rubber-leak`).
- One logical change per PR — small, reviewable diffs are easier to land.
- Update [`CHANGELOG.md`](CHANGELOG.md) under the next unreleased version.
- If you change visible behaviour, update the README usage table.
- Run `python -m py_compile ShpLassoTool/*.py` locally to catch syntax errors before pushing.

## Testing

There is no automated test suite (yet) — QGIS map tools are tightly coupled to canvas events and difficult to test outside QGIS itself. Manual testing in the live QGIS canvas is the current bar.

A future direction is to extract the pure geometry helpers (`_build_translated_geom`, `_chains_in_geom`, `_compute_sel_bbox`) into a separately testable module that doesn't import `qgis.gui`, so unit tests can cover them without launching QGIS.

## License

By contributing you agree your contributions are licensed under the project's **GPL v3** licence (see [LICENSE](LICENSE)).
