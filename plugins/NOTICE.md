# PartHive — Attribution & Modification Notice

PartHive's own code (`parthive_*.py`, `__init__.py`, `plugin.json`, `metadata.json`,
`icon.png`) is licensed **GPL-3.0-or-later** (see `LICENSE`).

## Bundled converter (AGPL-3.0)

PartHive bundles the EasyEDA→KiCad converter **`easyeda2kicad_ph`**, included as a git
submodule under `easyeda2kicad-ph/` and shipped inside the plugin package:

- Package: `easyeda2kicad-ph` (import name `easyeda2kicad_ph`)
- Source:  https://github.com/neocircuitslab/easyeda2kicad-ph
- License: **AGPL-3.0-or-later** (preserved in `easyeda2kicad-ph/LICENSE`)
- It is NeoCircuitsLab's fork of **easyeda2kicad.py** by uPesy
  (https://github.com/uPesy/easyeda2kicad.py, AGPL-3.0), with configurable footprint
  reference, configurable text sizes, symbol↔footprint library linking, and a
  standard-library-only rewrite. Changes are marked with `PartHive:` comments and
  documented in that repository.

## Licensing of the combination

GPL-3.0 and AGPL-3.0 are compatible: **GPLv3 §13** permits combining a GPL-3.0 work with an
AGPL-3.0 work into a single program and conveying it. PartHive's own code is GPL-3.0-or-later;
the bundled converter remains AGPL-3.0-or-later, with its full source public at the URL above.
The combined work is distributed honoring both licenses.

KiCad's Plugin & Content Manager validates `metadata.json`'s `license` field against a fixed
list that has no AGPL entry, so it is set to `GPL-3.0` (PartHive's own-code license). The
bundled converter's AGPL license is kept intact in `easyeda2kicad-ph/LICENSE` and disclosed here.

## Trademarks

"KiCad", "EasyEDA", "JLCPCB", and "LCSC" are trademarks of their respective owners. PartHive is
an independent project and is not affiliated with, endorsed by, or sponsored by any of them.
