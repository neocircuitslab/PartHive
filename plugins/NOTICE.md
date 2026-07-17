# PartHive — Attribution & Modification Notice

PartHive is a KiCad plugin that imports EasyEDA / JLCPCB (LCSC) components into a
local KiCad library. It is **licensed under AGPL-3.0-or-later** (see `LICENSE`).

## Upstream work this software builds on

PartHive bundles and builds directly on top of **easyeda2kicad.py**:

- Project: easyeda2kicad.py
- Author:  uPesy (contact@upesy.com)
- Source:  https://github.com/uPesy/easyeda2kicad.py
- License: AGPL-3.0-or-later
- Copyright (C) uPesy and contributors

The bundled copy lives in the `parthive_ee/` package — a verbatim copy of
easyeda2kicad, renamed only so it cannot clash on `sys.path` with other plugins
that also bundle a package named `easyeda2kicad` (e.g. impartGUI). It remains
under its original AGPL-3.0-or-later license. Because PartHive is a derivative
work of an AGPL-licensed program, PartHive as a whole is distributed under
AGPL-3.0-or-later.

## Modifications made by PartHive to the bundled easyeda2kicad

The following changes were applied to the vendored `parthive_ee/` sources
(each site is marked with a `PartHive:` comment):

- `parthive_ee/kicad/parameters_kicad_footprint.py`
  - The footprint reference text is now a parameter (default `?`; upstream used
    the literal `REF**`).
  - The footprint text height and stroke thickness are now parameters
    (default 1.0 mm; upstream hard-coded 0.8 mm / 0.15 mm).
- `parthive_ee/kicad/export_kicad_footprint.py`
  - `ExporterFootprintKicad.export()` accepts `reference` and `text_size`.
- `parthive_ee/kicad/parameters_kicad_symbol.py`
  - Added `set_property_font_size()` / `property_font_size()` so the symbol
    field (property) text height is configurable at runtime
    (PartHive default 1.27 mm; upstream default 1.0 mm).

All other logic that produces the KiCad symbol, footprint, and 3D model is the
upstream easyeda2kicad code, unmodified.

## PartHive's own code

The files `parthive_*.py`, `__init__.py`, `plugin.json`, `metadata.json`, and
`icon.png` are original to PartHive.

Copyright (C) 2026 PartHive contributors.

This program is free software: you can redistribute it and/or modify it under
the terms of the GNU Affero General Public License as published by the Free
Software Foundation, either version 3 of the License, or (at your option) any
later version. This program is distributed WITHOUT ANY WARRANTY. See the
`LICENSE` file for the full text.

## Trademarks

"KiCad", "EasyEDA", "JLCPCB", and "LCSC" are trademarks of their respective
owners. PartHive is an independent project and is not affiliated with,
endorsed by, or sponsored by any of them. Their names are used here only to
describe interoperability.
