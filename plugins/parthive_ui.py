"""PartHive – wxPython user interface.

A single dialog that mirrors the agreed layout:

    * Library dir (with Browse)         – holds the three library components
    * Symbol / Footprint / 3D lib names – created automatically, editable
    * EasyEDA/JLCPCB number + Import
    * Skip-3D toggle (off by default; 3D can need more bandwidth)
    * Symbol text size (mm, default 1.27) and Footprint text size (mm, default 1.0)
    * a live log area

The dialog runs the import on a worker thread and streams progress back to the
log via ``wx.CallAfter`` so the UI never freezes.

PartHive is licensed under GPL-3.0-or-later (see LICENSE).
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

import wx

# Absolute imports so this works whether launched as a script (IPC entrypoint /
# standalone) or imported as part of a package (SWIG action plugin).
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from parthive_config import ParthiveConfig  # noqa: E402
from parthive_importer import (  # noqa: E402
    ACTION_IMPORT,
    ACTION_SKIP,
    ACTION_USE_EXISTING,
    ComponentImporter,
    ItemResolution,
    Resolution,
)
from parthive_paths import (  # noqa: E402
    BRAND,
    default_library_dir,
    library_paths,
    scan_libraries,
)

_TITLE = f"{BRAND} — EasyEDA / JLCPCB → KiCad"

# Sentinel dropdown entry that lets the user create a brand-new library.
_CREATE_NEW = "+ Create new library..."


def _clean_item_name(value: str, default: str) -> str:
    value = (value or "").strip()
    for ch in '<>:"/\\|?*':
        value = value.replace(ch, "_")
    value = value.replace("..", "_").strip(" .")
    return value or default


class ResolutionDialog(wx.Dialog):
    """Advanced-mode pre-import dialog: shows the proposed symbol / footprint /
    3D-model names, lets the user edit them, and — per item — choose Import
    (Overwrite), Skip, or Use existing (footprint & 3D only)."""

    def __init__(self, parent, plan):
        super().__init__(parent, title=f"{BRAND} — confirm import", style=wx.DEFAULT_DIALOG_STYLE)
        self.plan = plan
        self._rows: dict = {}
        self._build()
        self.Fit()
        self.SetMinSize(self.GetSize())
        self.CentreOnParent()

    def _build(self) -> None:
        pad = 6
        panel = wx.Panel(self)
        root = wx.BoxSizer(wx.VERTICAL)
        root.Add(
            wx.StaticText(
                panel,
                label=f"Component {self.plan.component_id}: choose how to import each item.",
            ),
            0,
            wx.ALL,
            pad,
        )

        grid = wx.FlexGridSizer(cols=4, vgap=pad, hgap=pad)
        grid.AddGrowableCol(1, 1)
        grid.AddGrowableCol(3, 1)
        for header in ("Item", "Name", "Action", "Use existing"):
            grid.Add(wx.StaticText(panel, label=header), 0, wx.ALIGN_CENTER_VERTICAL)

        self._add_row(panel, grid, "symbol", "Symbol", self.plan.symbol, allow_existing=False)
        self._add_row(panel, grid, "footprint", "Footprint", self.plan.footprint, allow_existing=True)
        self._add_row(panel, grid, "model", "3D model", self.plan.model, allow_existing=True)
        root.Add(grid, 0, wx.EXPAND | wx.ALL, pad)

        note = wx.StaticText(
            panel,
            label=(
                'Rename by editing the Name. "Skip" keeps the existing file. '
                '"Use existing" links to another library item without importing.'
            ),
        )
        note.SetForegroundColour(wx.Colour(110, 110, 110))
        root.Add(note, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, pad)

        btnrow = wx.BoxSizer(wx.HORIZONTAL)
        btnrow.AddStretchSpacer(1)
        cancel = wx.Button(panel, wx.ID_CANCEL, "Cancel")
        ok = wx.Button(panel, wx.ID_OK, "Import")
        ok.SetDefault()
        btnrow.Add(cancel, 0, wx.RIGHT, pad)
        btnrow.Add(ok, 0)
        root.Add(btnrow, 0, wx.EXPAND | wx.ALL, pad)

        panel.SetSizer(root)
        outer = wx.BoxSizer(wx.VERTICAL)
        outer.Add(panel, 1, wx.EXPAND)
        self.SetSizer(outer)

    def _add_row(self, panel, grid, kind, label, item, allow_existing) -> None:
        if item is None:
            grid.Add(wx.StaticText(panel, label=f"{label}: (none)"), 0, wx.ALIGN_CENTER_VERTICAL)
            grid.Add(wx.StaticText(panel, label="—"), 0, wx.ALIGN_CENTER_VERTICAL)
            grid.Add(wx.StaticText(panel, label=""), 0)
            grid.Add(wx.StaticText(panel, label=""), 0)
            return

        status = "exists" if item.exists else "new"
        grid.Add(wx.StaticText(panel, label=f"{label}  ({status})"), 0, wx.ALIGN_CENTER_VERTICAL)

        name_ctrl = wx.TextCtrl(panel, value=item.name)
        grid.Add(name_ctrl, 1, wx.EXPAND)

        actions = []
        if item.exists:
            actions.append(("Overwrite", ACTION_IMPORT))
            actions.append(("Skip (keep existing)", ACTION_SKIP))
        else:
            actions.append(("Import", ACTION_IMPORT))
        if allow_existing and item.existing:
            actions.append(("Use existing", ACTION_USE_EXISTING))

        choice = wx.Choice(panel, choices=[a[0] for a in actions])
        # Safe default: keep existing files (Skip) when the item already exists.
        default_idx = 0
        for i, (_lbl, act) in enumerate(actions):
            if item.exists and act == ACTION_SKIP:
                default_idx = i
                break
        choice.SetSelection(default_idx)
        grid.Add(choice, 0, wx.ALIGN_CENTER_VERTICAL)

        existing_combo = wx.Choice(panel, choices=list(item.existing or []))
        if item.existing:
            existing_combo.SetSelection(0)
        existing_combo.Enable(False)
        grid.Add(existing_combo, 1, wx.EXPAND)

        self._rows[kind] = {
            "item": item,
            "name": name_ctrl,
            "choice": choice,
            "actions": actions,
            "existing": existing_combo,
        }
        choice.Bind(wx.EVT_CHOICE, lambda _e, k=kind: self._sync_row(k))
        self._sync_row(kind)

    def _row_action(self, kind) -> str:
        row = self._rows[kind]
        return row["actions"][row["choice"].GetSelection()][1]

    def _sync_row(self, kind) -> None:
        row = self._rows[kind]
        act = self._row_action(kind)
        row["name"].Enable(act == ACTION_IMPORT)
        row["existing"].Enable(act == ACTION_USE_EXISTING)

    def _row_resolution(self, kind):
        row = self._rows.get(kind)
        if row is None:
            return None
        act = self._row_action(kind)
        # Only honour the edited Name for Import/Overwrite. For Skip/Use-existing
        # the field may hold a stale rename typed while Import was selected, which
        # would otherwise mislink the symbol->footprint / footprint->model refs.
        if act == ACTION_IMPORT:
            name = _clean_item_name(row["name"].GetValue(), row["item"].name)
        else:
            name = row["item"].name
        existing = None
        if act == ACTION_USE_EXISTING:
            sel = row["existing"].GetSelection()
            if sel != wx.NOT_FOUND:
                existing = row["existing"].GetString(sel)
        return ItemResolution(action=act, name=name, existing=existing)

    def get_resolution(self) -> Resolution:
        return Resolution(
            symbol=self._row_resolution("symbol"),
            footprint=self._row_resolution("footprint"),
            model=self._row_resolution("model"),
        )


class ParthiveDialog(wx.Dialog):
    def __init__(self, parent: wx.Window | None = None):
        super().__init__(
            parent,
            title=_TITLE,
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        self.config = ParthiveConfig.load()
        self._importing = False
        self._build()
        self._load_into_widgets()
        self.SetMinSize(wx.Size(560, 720))
        self.Fit()
        self.CentreOnParent()

    # --------------------------------------------------------------- layout
    def _build(self) -> None:
        pad = 6
        panel = wx.Panel(self)
        root = wx.BoxSizer(wx.VERTICAL)

        # Helper: a StaticBoxSizer whose children are parented to the StaticBox
        # (the wx-recommended pattern; avoids "should be created as child of its
        # wxStaticBox" assertions).
        def static_box(title, orient=wx.VERTICAL):
            box = wx.StaticBox(panel, label=title)
            return box, wx.StaticBoxSizer(box, orient)

        # --- Library location -------------------------------------------
        loc_sb, loc_box = static_box("Library location")
        loc_row = wx.BoxSizer(wx.HORIZONTAL)
        loc_row.Add(wx.StaticText(loc_sb, label="Library dir:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, pad)
        self.txt_dir = wx.TextCtrl(loc_sb)
        loc_row.Add(self.txt_dir, 1, wx.EXPAND | wx.RIGHT, pad)
        self.btn_browse = wx.Button(loc_sb, label="Browse…")
        loc_row.Add(self.btn_browse, 0)
        loc_box.Add(loc_row, 0, wx.EXPAND | wx.ALL, pad)
        loc_box.Add(
            wx.StaticText(
                loc_sb,
                label="Holds your libraries. Default: Documents/KiCad/AddedParts.",
            ),
            0,
            wx.LEFT | wx.RIGHT | wx.BOTTOM,
            pad,
        )
        root.Add(loc_box, 0, wx.EXPAND | wx.ALL, pad)

        # --- Target libraries -------------------------------------------
        name_sb, name_box = static_box("Target libraries")
        self.chk_advanced = wx.CheckBox(
            name_sb,
            label=(
                "Advanced: organise into symbols/ footprints/ 3dmodels/ subfolders "
                "and pick the target library"
            ),
        )
        name_box.Add(self.chk_advanced, 0, wx.ALL, 3)

        grid = wx.FlexGridSizer(cols=2, vgap=pad, hgap=pad)
        grid.AddGrowableCol(1, 1)
        # Read-only dropdowns: the user either picks an existing library or uses
        # "+ Create new library..." — no free typing.
        self.cmb_sym = wx.ComboBox(name_sb, style=wx.CB_READONLY)
        self.cmb_fp = wx.ComboBox(name_sb, style=wx.CB_READONLY)
        self.cmb_3d = wx.ComboBox(name_sb, style=wx.CB_READONLY)
        for label, ctrl in (
            ("Symbol lib (.kicad_sym):", self.cmb_sym),
            ("Footprint lib (.pretty):", self.cmb_fp),
            ("3D lib (.3dshapes):", self.cmb_3d),
        ):
            grid.Add(wx.StaticText(name_sb, label=label), 0, wx.ALIGN_CENTER_VERTICAL)
            grid.Add(ctrl, 1, wx.EXPAND)
        name_box.Add(grid, 0, wx.EXPAND | wx.ALL, pad)

        hint_row = wx.BoxSizer(wx.HORIZONTAL)
        self.btn_refresh = wx.Button(name_sb, label="Refresh lists")
        hint_row.Add(self.btn_refresh, 0, wx.RIGHT, pad)
        self.btn_show_paths = wx.Button(name_sb, label="Show paths")
        hint_row.Add(self.btn_show_paths, 0, wx.RIGHT, pad)
        hint_row.Add(
            wx.StaticText(
                name_sb,
                label='Pick an existing library, or "+ Create new library..." to add a new one.',
            ),
            0,
            wx.ALIGN_CENTER_VERTICAL,
        )
        name_box.Add(hint_row, 0, wx.ALL, 3)
        root.Add(name_box, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, pad)

        # combo -> library type; and last-good value per combo (for create-cancel)
        self._combo_kind = {
            self.cmb_sym: "symbols",
            self.cmb_fp: "footprints",
            self.cmb_3d: "models",
        }
        self._prev_lib: dict = {}

        # --- Import ------------------------------------------------------
        imp_sb, imp_box = static_box("Import component", wx.HORIZONTAL)
        imp_box.Add(
            wx.StaticText(imp_sb, label="EasyEDA / JLCPCB (LCSC) number:"),
            0,
            wx.ALIGN_CENTER_VERTICAL | wx.ALL,
            pad,
        )
        self.txt_component = wx.TextCtrl(imp_sb, style=wx.TE_PROCESS_ENTER)
        self.txt_component.SetHint("e.g. C25804")
        imp_box.Add(self.txt_component, 1, wx.ALIGN_CENTER_VERTICAL | wx.ALL, pad)
        self.btn_import = wx.Button(imp_sb, label="Import")
        self.btn_import.SetDefault()
        imp_box.Add(self.btn_import, 0, wx.ALL, pad)
        root.Add(imp_box, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, pad)

        # --- Options -----------------------------------------------------
        opt_sb, opt_box = static_box("Options")
        self.chk_skip3d = wx.CheckBox(opt_sb, label="Skip 3D model  (faster / lower bandwidth)")
        self.chk_step = wx.CheckBox(opt_sb, label="Prefer STEP 3D model — STEP only (skip WRL)")
        self.chk_compress = wx.CheckBox(opt_sb, label="Compress STEP model (.step.gz)")
        self.chk_overwrite = wx.CheckBox(opt_sb, label="Overwrite existing parts (simple mode)")
        self.chk_register = wx.CheckBox(opt_sb, label="Register libraries in KiCad automatically")
        self.chk_close = wx.CheckBox(opt_sb, label="Close this window after a successful import")
        for c in (
            self.chk_skip3d,
            self.chk_step,
            self.chk_compress,
            self.chk_overwrite,
            self.chk_register,
            self.chk_close,
        ):
            opt_box.Add(c, 0, wx.ALL, 3)

        size_grid = wx.FlexGridSizer(cols=2, vgap=pad, hgap=pad)
        self.spin_sym = wx.SpinCtrlDouble(opt_sb, min=0.1, max=20.0, inc=0.01)
        self.spin_sym.SetDigits(2)
        self.spin_fp = wx.SpinCtrlDouble(opt_sb, min=0.1, max=20.0, inc=0.01)
        self.spin_fp.SetDigits(2)
        size_grid.Add(wx.StaticText(opt_sb, label="Symbol text size (mm):"), 0, wx.ALIGN_CENTER_VERTICAL)
        size_grid.Add(self.spin_sym, 0)
        size_grid.Add(wx.StaticText(opt_sb, label="Footprint text size (mm):"), 0, wx.ALIGN_CENTER_VERTICAL)
        size_grid.Add(self.spin_fp, 0)
        opt_box.Add(size_grid, 0, wx.ALL, pad)
        root.Add(opt_box, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, pad)

        # --- Log ---------------------------------------------------------
        log_sb, log_box = static_box("Log")
        self.txt_log = wx.TextCtrl(
            log_sb, style=wx.TE_MULTILINE | wx.TE_READONLY | wx.HSCROLL
        )
        self.txt_log.SetMinSize(wx.Size(-1, 150))
        log_box.Add(self.txt_log, 1, wx.EXPAND | wx.ALL, pad)
        root.Add(log_box, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, pad)

        # --- Bottom buttons ---------------------------------------------
        btm = wx.BoxSizer(wx.HORIZONTAL)
        self.btn_open_dir = wx.Button(panel, label="Open library folder")
        btm.Add(self.btn_open_dir, 0, wx.RIGHT, pad)
        btm.AddStretchSpacer(1)
        self.btn_close = wx.Button(panel, wx.ID_CLOSE, "Close")
        btm.Add(self.btn_close, 0)
        root.Add(btm, 0, wx.EXPAND | wx.ALL, pad)

        panel.SetSizer(root)
        outer = wx.BoxSizer(wx.VERTICAL)
        outer.Add(panel, 1, wx.EXPAND)
        self.SetSizer(outer)

        # --- events ------------------------------------------------------
        self.btn_browse.Bind(wx.EVT_BUTTON, self.on_browse)
        self.btn_import.Bind(wx.EVT_BUTTON, self.on_import)
        self.txt_component.Bind(wx.EVT_TEXT_ENTER, self.on_import)
        self.btn_open_dir.Bind(wx.EVT_BUTTON, self.on_open_dir)
        self.btn_close.Bind(wx.EVT_BUTTON, lambda _e: self.Close())
        self.Bind(wx.EVT_CLOSE, self.on_close)
        self.Bind(wx.EVT_CHAR_HOOK, self._on_char_hook)

        self.chk_advanced.Bind(wx.EVT_CHECKBOX, self._on_advanced_toggle)
        self.btn_refresh.Bind(wx.EVT_BUTTON, lambda _e: self._refresh_library_choices())
        self.btn_show_paths.Bind(wx.EVT_BUTTON, self._on_show_paths)
        for _cmb in (self.cmb_sym, self.cmb_fp, self.cmb_3d):
            _cmb.Bind(wx.EVT_COMBOBOX, self._on_lib_combo)

    # ----------------------------------------------------------- data <-> UI
    def _load_into_widgets(self) -> None:
        c = self.config
        self.txt_dir.SetValue(c.library_dir or str(default_library_dir(c.model_lib_name)))
        self.chk_advanced.SetValue(c.advanced_mode)
        # Seed the desired selection per (read-only) combo; the actual dropdown
        # items are populated by _refresh_library_choices, which honours these.
        self._prev_lib = {
            self.cmb_sym: c.symbol_lib_name or "AddedParts",
            self.cmb_fp: c.footprint_lib_name or "AddedParts",
            self.cmb_3d: c.model_lib_name or "AddedParts",
        }
        self.chk_skip3d.SetValue(c.skip_3d)
        self.chk_step.SetValue(c.prefer_step)
        self.chk_compress.SetValue(c.compress_models)
        self.chk_overwrite.SetValue(c.overwrite)
        self.chk_register.SetValue(c.auto_register)
        self.chk_close.SetValue(c.close_after_import)
        self.spin_sym.SetValue(c.symbol_text_size)
        self.spin_fp.SetValue(c.footprint_text_size)
        # Intentionally start with an empty part-number field on each open.
        self.txt_component.SetValue("")
        self._refresh_library_choices()

    def _clean_name(self, value: str, default: str) -> str:
        value = (value or "").strip()
        if not value or value == _CREATE_NEW:
            return default
        for ch in '<>:"/\\|?*':
            value = value.replace(ch, "_")
        return value.strip() or default

    def _current_names(self) -> tuple[str, str, str]:
        c = self.config
        return (
            self._clean_name(self.cmb_sym.GetStringSelection(), c.symbol_lib_name or "AddedParts"),
            self._clean_name(self.cmb_fp.GetStringSelection(), c.footprint_lib_name or "AddedParts"),
            self._clean_name(self.cmb_3d.GetStringSelection(), c.model_lib_name or "AddedParts"),
        )

    def _harvest_widgets(self) -> ParthiveConfig:
        c = self.config
        c.library_dir = self.txt_dir.GetValue().strip()
        c.advanced_mode = self.chk_advanced.GetValue()
        c.symbol_lib_name, c.footprint_lib_name, c.model_lib_name = self._current_names()
        c.skip_3d = self.chk_skip3d.GetValue()
        c.prefer_step = self.chk_step.GetValue()
        c.compress_models = self.chk_compress.GetValue()
        c.overwrite = self.chk_overwrite.GetValue()
        c.auto_register = self.chk_register.GetValue()
        c.close_after_import = self.chk_close.GetValue()
        c.symbol_text_size = round(float(self.spin_sym.GetValue()), 3)
        c.footprint_text_size = round(float(self.spin_fp.GetValue()), 3)
        c.last_component = self.txt_component.GetValue().strip()
        return c

    # -------------------------------------------------- target-library combos
    def _on_show_paths(self, _evt) -> None:
        advanced = self.chk_advanced.GetValue()
        libdir = self.txt_dir.GetValue().strip() or str(default_library_dir())
        sym, fp, td = self._current_names()
        p = library_paths(libdir, sym, fp, td, advanced=advanced)
        wx.MessageBox(
            f"Symbol:    {p['symbol_lib']}\n"
            f"Footprint: {p['footprint_dir']}\n"
            f"3D model:  {p['model_dir']}",
            "PartHive — target paths",
            wx.OK | wx.ICON_INFORMATION,
        )

    def _refresh_library_choices(self) -> None:
        advanced = self.chk_advanced.GetValue()
        libdir = self.txt_dir.GetValue().strip()
        try:
            found = (
                scan_libraries(libdir, advanced)
                if libdir
                else {"symbols": [], "footprints": [], "models": []}
            )
        except Exception:  # noqa: BLE001
            found = {"symbols": [], "footprints": [], "models": []}

        defaults = {
            "symbols": self.config.symbol_lib_name or "AddedParts",
            "footprints": self.config.footprint_lib_name or "AddedParts",
            "models": self.config.model_lib_name or "AddedParts",
        }
        for cmb, kind in self._combo_kind.items():
            desired = (self._prev_lib.get(cmb) or defaults[kind]).strip()
            if not desired or desired == _CREATE_NEW:
                desired = defaults[kind]
            names = list(found[kind])
            # A read-only combo can only display a value present in its list, so
            # ensure the pending target name is selectable even if it does not
            # exist on disk yet.
            if desired not in names:
                names = [desired] + names
            cmb.Set([_CREATE_NEW] + names)
            cmb.SetStringSelection(desired)
            self._prev_lib[cmb] = desired

    def _on_advanced_toggle(self, _evt) -> None:
        self._refresh_library_choices()

    def _ask_new_name(self, kind: str, default: str = "") -> str:
        label = {"symbols": "symbol", "footprints": "footprint", "models": "3D-model"}[kind]
        with wx.TextEntryDialog(
            self, f"Name for the new {label} library:", "Create library", value=default
        ) as dlg:
            if dlg.ShowModal() != wx.ID_OK:
                return ""
            return self._clean_name(dlg.GetValue(), "")

    def _create_library(self, kind: str, name: str) -> None:
        advanced = self.chk_advanced.GetValue()
        libdir = self.txt_dir.GetValue().strip() or str(default_library_dir())
        p = library_paths(libdir, name, name, name, advanced=advanced)
        if kind == "symbols":
            f = p["symbol_lib"]
            f.parent.mkdir(parents=True, exist_ok=True)
            if not f.exists():
                f.write_text(
                    '(kicad_symbol_lib\n  (version 20211014)\n  (generator "PartHive")\n)\n',
                    encoding="utf-8",
                )
        elif kind == "footprints":
            p["footprint_dir"].mkdir(parents=True, exist_ok=True)
        else:
            p["model_dir"].mkdir(parents=True, exist_ok=True)
        self.append_log(f"Created {kind} library '{name}'.")

    def _on_lib_combo(self, evt) -> None:
        cmb = evt.GetEventObject()
        selection = cmb.GetStringSelection().strip()
        if selection != _CREATE_NEW:
            self._prev_lib[cmb] = selection
            return
        # "+ Create new library..." chosen — prompt, create, and reselect it.
        kind = self._combo_kind[cmb]
        name = self._ask_new_name(kind)
        if not name:
            self._refresh_library_choices()  # restore previous selection
            return
        try:
            self._create_library(kind, name)
        except Exception as exc:  # noqa: BLE001
            wx.MessageBox(f"Could not create library: {exc}", "PartHive", wx.OK | wx.ICON_ERROR)
            self._refresh_library_choices()
            return
        self._prev_lib[cmb] = name
        self._refresh_library_choices()

    # ---------------------------------------------------------------- events
    def on_browse(self, _evt) -> None:
        start = self.txt_dir.GetValue().strip() or str(default_library_dir())
        with wx.DirDialog(self, "Choose the library directory", start) as dlg:
            if dlg.ShowModal() == wx.ID_OK:
                self.txt_dir.SetValue(dlg.GetPath())
                self._refresh_library_choices()

    def on_open_dir(self, _evt) -> None:
        path = Path(self.txt_dir.GetValue().strip()).expanduser()
        path.mkdir(parents=True, exist_ok=True)
        try:
            wx.LaunchDefaultApplication(str(path))
        except Exception:  # noqa: BLE001
            self.append_log(f"Could not open folder: {path}")

    def append_log(self, msg: str) -> None:
        self.txt_log.AppendText(msg + "\n")

    def _set_busy(self, busy: bool) -> None:
        self._importing = busy
        self.btn_import.Enable(not busy)
        self.btn_import.SetLabel("Importing…" if busy else "Import")

    def on_import(self, _evt) -> None:
        if self._importing:
            return
        config = self._harvest_widgets()
        config.save()
        component = config.last_component
        if not component:
            self.append_log("Enter an EasyEDA/JLCPCB (LCSC) number, e.g. C25804.")
            return

        self.txt_log.Clear()
        self._set_busy(True)

        def log(m: str) -> None:
            wx.CallAfter(self.append_log, m)

        importer = ComponentImporter(config, log)

        # Simple mode: one-shot import (no resolution dialog).
        if not config.advanced_mode:
            def worker() -> None:
                try:
                    result = importer.import_component(component)
                except Exception as exc:  # noqa: BLE001
                    wx.CallAfter(self.append_log, f"Unexpected error: {exc}")
                    wx.CallAfter(self._set_busy, False)
                    return
                wx.CallAfter(self._finish, result)

            threading.Thread(target=worker, daemon=True).start()
            return

        # Advanced mode: fetch + plan on a worker, then show the resolution dialog.
        def plan_worker() -> None:
            try:
                plan = importer.plan(component)
            except Exception as exc:  # noqa: BLE001
                wx.CallAfter(self.append_log, f"Unexpected error: {exc}")
                wx.CallAfter(self._set_busy, False)
                return
            wx.CallAfter(self._after_plan, importer, plan)

        threading.Thread(target=plan_worker, daemon=True).start()

    def _after_plan(self, importer, plan) -> None:
        if plan is None:
            self._set_busy(False)
            return
        dlg = ResolutionDialog(self, plan)
        proceed = dlg.ShowModal() == wx.ID_OK
        resolution = dlg.get_resolution() if proceed else None
        dlg.Destroy()
        if not proceed:
            self.append_log("Import cancelled.")
            self._set_busy(False)
            return

        def exec_worker() -> None:
            try:
                result = importer.execute(plan, resolution)
            except Exception as exc:  # noqa: BLE001
                wx.CallAfter(self.append_log, f"Unexpected error: {exc}")
                wx.CallAfter(self._set_busy, False)
                return
            wx.CallAfter(self._finish, result)

        threading.Thread(target=exec_worker, daemon=True).start()

    def _finish(self, result) -> None:
        self._set_busy(False)
        if result.ok:
            self.append_log(f"[OK] Done: {result.component_id}")
        else:
            self.append_log(f"[FAILED] {result.component_id}")
        # a new library may have been created during import — reflect it
        self._refresh_library_choices()
        if result.ok and self.chk_close.GetValue():
            self.Close()

    def _on_char_hook(self, evt) -> None:
        # Route ESC through on_close (so the import guard applies) instead of the
        # dialog's default immediate cancel/destroy.
        if evt.GetKeyCode() == wx.WXK_ESCAPE:
            self.Close()
            return
        evt.Skip()

    def on_close(self, evt) -> None:
        # Never tear down the dialog while a background import is still running:
        # the worker's wx.CallAfter(self._finish, ...) would then run against
        # destroyed widgets and raise "wrapped C/C++ object has been deleted".
        if self._importing:
            wx.MessageBox(
                "An import is still running — please wait for it to finish before closing.",
                "PartHive",
                wx.OK | wx.ICON_INFORMATION,
            )
            if hasattr(evt, "CanVeto") and evt.CanVeto():
                evt.Veto()
            return
        try:
            self._harvest_widgets().save()
        except Exception:  # noqa: BLE001
            pass
        self.Destroy()


def show_dialog(parent: wx.Window | None = None) -> None:
    """Show the dialog. Assumes a wx.App already exists (KiCad provides one)."""
    dlg = ParthiveDialog(parent)
    dlg.ShowModal()
    try:
        dlg.Destroy()
    except Exception:  # noqa: BLE001
        pass


def run_standalone() -> None:
    """Entry point for running the UI outside KiCad (testing)."""
    app = wx.App(False)
    show_dialog(None)
    app.MainLoop()


if __name__ == "__main__":
    run_standalone()
