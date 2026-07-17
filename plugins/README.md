# PartHive

Import **EasyEDA / JLCPCB (LCSC)** components into a local **KiCad** library from
their part number (e.g. `C25804`). PartHive creates and links a symbol library,
a footprint library, and a 3D-model library inside one directory, then registers
them in KiCad for you.

> **License: AGPL-3.0-or-later.** PartHive is a derivative work of
> [easyeda2kicad.py](https://github.com/uPesy/easyeda2kicad.py) by uPesy, which
> is AGPL-3.0. See [`LICENSE`](LICENSE) and [`NOTICE.md`](NOTICE.md). You may use,
> modify, redistribute, and sell it, but it must remain open source under
> AGPL-3.0 and keep the attribution.

---

## What it does

From an LCSC number it produces, inside your chosen **library dir**:

```
<library dir>/
├── AddedParts.kicad_sym     # symbols
├── AddedParts.pretty/       # footprints (.kicad_mod)
└── AddedParts.3dshapes/     # 3D models (.wrl / .step[.gz])
```

and wires them together:

- **symbol → footprint** — the symbol's `Footprint` field becomes
  `AddedParts:<footprint>`.
- **footprint → 3D model** — the footprint's `(model …)` line points into the
  `.3dshapes` folder using a **portable path**:
  - inside your user profile → `${USERPROFILE}/…` (Windows) or `${HOME}/…`
    (macOS/Linux), so the library still works after copying to another machine
    or user account;
  - outside your user profile → a fixed absolute path.

### PartHive-specific behaviour

| Setting | Default | Notes |
|---|---|---|
| Footprint reference | `?` (fixed) | Set in code; upstream easyeda2kicad writes `REF**`. |
| Footprint text size | `1.0 mm` | Upstream hard-codes 0.8 mm. |
| Symbol field text size | `1.27 mm` | Matches KiCad's standard field height. |
| Skip 3D model | off | 3D download can need more bandwidth. |
| Prefer STEP — STEP only | on | Keeps only the STEP model and drops the WRL. |
| Auto-register libraries | on | Adds entries to KiCad's global tables. |
| Close after successful import | off | The dialog is modal; auto-close frees the other KiCad windows. |

The three library **names** are independent and editable (Symbol / Footprint /
3D), all defaulting to `AddedParts`. The **library dir** defaults to
`Documents/KiCad/AddedParts` (Windows Documents redirection, e.g. OneDrive, is
handled automatically).

## Simple vs. Advanced layout

PartHive has two layouts, toggled by the **Advanced** checkbox:

**Simple (default)** — a flat library dir:

```
<library dir>/AddedParts.kicad_sym
<library dir>/AddedParts.pretty/
<library dir>/AddedParts.3dshapes/
```

**Advanced** — the library dir is organised into per-type subfolders, and each
of the three library fields becomes a **dropdown** listing the libraries already
present in that subfolder:

```
<library dir>/symbols/*.kicad_sym
<library dir>/footprints/*.pretty/
<library dir>/3dmodels/*.3dshapes/
```

In Advanced mode each field is a dropdown: pick an existing library, or choose
**"+ Create new library…"** to make one. The **Show paths** button (next to
**Refresh lists**) shows exactly where the symbol, footprint, and 3D model will
be written. Links (symbol→footprint, footprint→3D model) and library
registration work identically in both layouts.

### Confirm dialog (Advanced mode)

When you click **Import** in Advanced mode, PartHive fetches the part and then
shows a **confirm dialog** with the proposed **symbol**, **footprint**, and
**3D-model** names. For each one you can:

- **edit the name** (rename before it is written),
- **Import / Overwrite** it,
- **Skip** it (keep the existing file untouched), or
- **Use existing** — link to another library item *without* importing a new
  footprint/3D file (footprint & 3D only).

Existing items default to **Skip** so nothing is overwritten unless you choose
to. Simple mode imports in one click without this dialog.

---

## Install

### A. Via KiCad's Plugin & Content Manager (recommended)

1. In KiCad: **Plugins → Plugin and Content Manager → Install from File…**
2. Pick `PartHive-0.1.6.zip`.
3. Restart KiCad. A **PartHive** button appears in the PCB and Schematic
   editors.

### B. Manual drop-in

Extract the `plugins/` folder of the zip into a KiCad 3rd-party plugin folder,
into a subfolder named after the identifier, e.g. on Windows:

```
%USERPROFILE%\Documents\KiCad\10.0\3rdparty\plugins\com_github_neocircuitslab_parthive\
```

(If your Documents are redirected to OneDrive, use that path.) Then in KiCad:
**Tools → External Plugins → Refresh Plugins** (or restart).

---

## Use

1. Open PartHive from the toolbar.
2. Check/adjust the **library dir** and the three library **names**.
3. Type an **LCSC number** (e.g. `C25804`) and click **Import**.
4. Watch the log. On success the symbol, footprint, and 3D model are written and
   the libraries are registered.
5. **Restart KiCad** the first time so the newly registered libraries appear.

> Registering libraries edits KiCad's global `sym-lib-table` / `fp-lib-table`.
> PartHive backs those files up first (`*.parthive-bak`) and only *adds* missing
> entries. New libraries appear after a KiCad restart.

You can also run the UI outside KiCad for testing:

```
python parthive_action.py
```

---

## Rebranding

The brand name lives in one place: the constant `BRAND` in
[`parthive_paths.py`](parthive_paths.py). Change it, then update `plugin.json`,
`metadata.json`, the `identifier` (use your own `com.github.<youruser>.<name>`),
and this README. Keep `LICENSE` and `NOTICE.md` intact (AGPL requirement).

## Submitting to the official KiCad repository (PCM) later

1. Host the repo on your GitHub account.
2. Publish a release zip and compute its `download_sha256`, `download_size`, and
   `install_size`; add a full `versions[]` entry (with `download_url`) to
   `metadata.json`, and set `status` to `stable` when ready.
3. Open a merge request against KiCad's addon metadata repository
   (`gitlab.com/kicad/addons/metadata`). AGPL-3.0 is an accepted license.
