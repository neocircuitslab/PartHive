# PartHive

A KiCad plugin that imports **EasyEDA / JLCPCB (LCSC)** components — symbol,
footprint, and 3D model — into a local KiCad library from a part number
(e.g. `C25804`), and wires the three together.

> **License: AGPL-3.0-or-later.** PartHive builds on
> [easyeda2kicad.py](https://github.com/uPesy/easyeda2kicad.py) (uPesy), which is
> AGPL-3.0. See [`LICENSE`](LICENSE) and [`plugins/NOTICE.md`](plugins/NOTICE.md).

## Features

- Import by LCSC part number; creates & links `.kicad_sym`, `.pretty`, `.3dshapes`.
- Symbol → footprint and footprint → 3D-model links, with a portable
  `${USERPROFILE}`/`${HOME}` 3D path (absolute fallback outside the profile).
- **Simple** (flat) or **Advanced** (organized into `symbols/ footprints/
  3dmodels/`) library layout, with a pick-or-create dropdown per library.
- Advanced-mode **confirm dialog**: per item Import/Overwrite, Skip, or Use
  existing (footprint & 3D).
- STEP-only 3D option, configurable text sizes, fixed `?` footprint reference,
  optional auto-registration into KiCad's global library tables.

## Repository layout

```
metadata.json          # KiCad PCM package metadata (in-archive copy; no download_* keys)
plugins/               # the plugin itself (this is what ships)
  __init__.py          #   SWIG action-plugin registration (KiCad 7–10)
  plugin.json          #   IPC API metadata (KiCad 9+)
  parthive_*.py        #   PartHive's own code
  parthive_ee/         #   vendored easyeda2kicad converter (AGPL-3.0)
  icon.png, LICENSE, NOTICE.md, README.md, requirements.txt
resources/icon.png     # 64px+ icon shown in the PCM
tools/build_zip.py     # builds the installable/PCM .zip
```

## Install

**From file (any KiCad 7–10):** *Plugins → Plugin and Content Manager → Install
from File…* → pick the built `.zip` → restart KiCad. See
[`plugins/README.md`](plugins/README.md) for full usage.

## Build the package

```
python tools/build_zip.py . ./PartHive.zip
```

This produces a PCM-compatible archive (`metadata.json` + `plugins/` +
`resources/` at the root).

## Publishing to the KiCad PCM

1. Create a GitHub Release and attach the built `PartHive-x.y.z.zip`.
2. Compute its `sha256`, download size, and install (uncompressed) size.
3. Submit a merge request to `https://gitlab.com/kicad/addons/metadata` adding
   `packages/com.github.neocircuitslab.parthive/metadata.json` with a `versions[]`
   entry that includes `download_url`, `download_sha256`, `download_size`, and
   `install_size` (these `download_*` keys go **only** in the submitted metadata,
   never in the archive's copy).
