# easyeda2kicad-ph

EasyEDA / JLCPCB (LCSC) → KiCad converter (symbols, footprints, 3D models).

> **This is a fork of [uPesy/easyeda2kicad.py](https://github.com/uPesy/easyeda2kicad.py).**
> Original work © uPesy, licensed **AGPL-3.0**. This fork is maintained by **NeoCircuitsLab** and is
> likewise licensed **AGPL-3.0-or-later** — see [`LICENSE`](LICENSE) and [`NOTICE`](NOTICE).

It is the converter engine used by the **PartHive** KiCad plugin. The import package is named
`easyeda2kicad_ph` (not `easyeda2kicad`) so it can coexist with the upstream package if both are
installed in the same Python environment (e.g. alongside other KiCad plugins).

## Modifications vs upstream
- Footprint reference defaults to `?` (upstream: `REF**`) — configurable.
- Configurable symbol / footprint field text sizes (`set_property_font_size`).
- Symbol → footprint library linking (`save_to_lib(..., footprint_lib_name=...)`, `tune_footprint_ref_path`).
- Added `id_already_in_symbol_lib`; 3D model `export(output_dir, overwrite=...)`.
- Rewritten to use only the Python standard library (no `pydantic` / `requests` dependencies).

In-source changes are marked with `# PartHive:` comments.

## Install
```bash
# from PyPI (if published):
pip install easyeda2kicad-ph

# or from a release wheel (no git required):
pip install https://github.com/neocircuitslab/easyeda2kicad-ph/releases/download/vX.Y.Z/easyeda2kicad_ph-X.Y.Z-py3-none-any.whl
```

## Use as a library
```python
from easyeda2kicad_ph import (
    EasyedaApi,
    EasyedaSymbolImporter,
    EasyedaFootprintImporter,
    Easyeda3dModelImporter,
    ExporterSymbolKicad,
    ExporterFootprintKicad,
    Exporter3dModelKicad,
)
```

## CLI
```bash
python -m easyeda2kicad_ph --help
```

## License
**AGPL-3.0-or-later.** Because this is a derivative of AGPL-3.0 software, the whole package remains
under AGPL-3.0-or-later. If you distribute it (or run it as a network service), you must make the
complete corresponding source available under the same license.
