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

import dataclasses
import json
import os
import shutil
import sys
import threading
from pathlib import Path

import wx

# Absolute imports so this works whether launched as a script (IPC entrypoint /
# standalone) or imported as part of a package (SWIG action plugin).
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from parthive_config import (  # noqa: E402
    PROJECT_LIB_SUBDIR,
    ParthiveConfig,
    config_dir,
    ProjectSettings,
    project_settings_path,
)
from parthive_importer import (  # noqa: E402
    ACTION_IMPORT,
    ACTION_SKIP,
    ACTION_USE_EXISTING,
    ComponentImporter,
    ItemResolution,
    Resolution,
)
from parthive_fp_fix import (  # noqa: E402
    repair_footprint_dir,
    scan_footprint_dir,
)
from parthive_lib_table import (  # noqa: E402
    detect_open_project,
    find_settings_dir,
    find_project_root,
    is_project_dir,
    loaded_footprint_libs,
    conflict_tables,
    nickname_conflict,
    registration_state,
    scope_tables,
)
from parthive_manage import (  # noqa: E402
    TABLE_KIND,
    existing_kinds,
    lib_path,
    relink_footprints,
    relink_models,
    table_remove,
    table_rename,
    to_recycle_bin,
)
from parthive_pins import drop_pin, ensure_pins, move_pin, request_pins  # noqa: E402
import parthive_copy as copylib  # noqa: E402
import parthive_fixlinks as fixlinks  # noqa: E402
from parthive_paths import (  # noqa: E402
    BRAND,
    VERSION,
    default_library_dir,
    detect_advanced_layout,
    library_paths,
    lib_dir_problem,
    lib_name_problem,
    safe_item_name,
    safe_lib_name,
    scan_libraries,
)

_TITLE = f"{BRAND} v{VERSION} — EasyEDA / JLCPCB → KiCad"

# Sentinel dropdown entries: create a brand-new library, or manage the
# selected one (only offered when it exists on disk).
_CREATE_NEW = "+ Create new library..."
_RENAME = "✏ Rename library..."
# Pin types option (see parthive_pintypes)
PIN_TYPE_MODES = ("smart", "keep", "passive")
PIN_TYPE_LABELS = {"smart": "Smart (recommended)", "keep": "Keep EasyEDA's", "passive": "All passive"}
_DELETE = "✕ Delete library..."
_ACTIONS = (_CREATE_NEW, _RENAME, _DELETE)

# Shown in the three target-library dropdowns until a layout is chosen.
_PICK_LAYOUT = "— select a layout first —"


def _clean_item_name(value: str, default: str) -> str:
    return safe_item_name((value or "").strip(), default)


def _muted_colour() -> wx.Colour:
    return wx.SystemSettings.GetColour(wx.SYS_COLOUR_GRAYTEXT)


def _is_inside(path: str, folder: str) -> bool:
    """True if ``path`` is ``folder`` or below it (case-insensitive on Windows)."""
    try:
        p = os.path.normcase(str(Path(path).expanduser().resolve()))
        f = os.path.normcase(str(Path(folder).expanduser().resolve()))
    except OSError:
        return False
    return p == f or p.startswith(f.rstrip("\\/") + os.sep)


def _short_path(path: str, base: str | None = None) -> str:
    """Card-friendly path: relative to the project's parent when inside it
    (``Sentinel-Relay-PCB\\Libraries``), else Documents-relative, else as is."""
    try:
        p = Path(path).expanduser()
        if base and _is_inside(path, base):
            return str(p.resolve().relative_to(Path(base).resolve().parent))
        docs = default_library_dir().parent.parent  # <Documents>
        if _is_inside(path, str(docs)):
            return str(Path("Documents") / p.resolve().relative_to(docs.resolve()))
    except (OSError, ValueError):
        pass
    return str(path)


class _ScopeCard(wx.Panel):
    """A clickable card (radio + path + note) for the Global / Project choice.

    Draws its own rounded border: thick in the system highlight colour when
    selected, thin and grey otherwise — readable in light and dark themes.
    """

    def __init__(self, parent: wx.Window, title: str, on_pick) -> None:
        super().__init__(parent, style=wx.TAB_TRAVERSAL)
        self._on_pick = on_pick
        self._selected = False
        self.radio = wx.RadioButton(self, label=title, style=wx.RB_SINGLE)
        font = self.radio.GetFont()
        font.SetWeight(wx.FONTWEIGHT_BOLD)
        self.radio.SetFont(font)
        self.lbl_path = wx.StaticText(
            self, label="", style=wx.ST_ELLIPSIZE_MIDDLE | wx.ST_NO_AUTORESIZE
        )
        self.lbl_path.SetMinSize(wx.Size(150, -1))
        s = wx.BoxSizer(wx.VERTICAL)
        s.Add(self.radio, 0, wx.LEFT | wx.RIGHT | wx.TOP, 9)
        s.Add(self.lbl_path, 0, wx.EXPAND | wx.ALL, 9)
        self.SetSizer(s)
        for w in (self, self.lbl_path):
            w.Bind(wx.EVT_LEFT_UP, self._clicked)
            w.SetCursor(wx.Cursor(wx.CURSOR_HAND))
        self.radio.Bind(wx.EVT_RADIOBUTTON, self._clicked)
        self.Bind(wx.EVT_PAINT, self._paint)
        self.Bind(wx.EVT_SIZE, lambda e: (self.Refresh(), e.Skip()))

    def SetToolTip(self, tip) -> None:  # noqa: N802 - wx naming
        for w in (self, self.radio, self.lbl_path):
            wx.Window.SetToolTip(w, tip)

    def _clicked(self, evt) -> None:
        # Re-sync the radio to the real state; the owner decides what happens.
        self.radio.SetValue(self._selected)
        wx.CallAfter(self._on_pick)
        if hasattr(evt, "Skip"):
            evt.Skip()

    def set_content(self, path: str) -> None:
        self.lbl_path.SetLabel(path)
        self.lbl_path.SetToolTip(path)

    def set_selected(self, selected: bool) -> None:
        self._selected = selected
        self.radio.SetValue(selected)
        self.Refresh()

    def _paint(self, _evt) -> None:
        dc = wx.PaintDC(self)
        gc = wx.GraphicsContext.Create(dc)
        if gc is None:
            return
        w, h = self.GetClientSize()
        if self._selected:
            colour, width = wx.SystemSettings.GetColour(wx.SYS_COLOUR_HIGHLIGHT), 2
        else:
            colour, width = _muted_colour(), 1
        gc.SetPen(wx.Pen(colour, width))
        gc.SetBrush(wx.TRANSPARENT_BRUSH)
        gc.DrawRoundedRectangle(width / 2, width / 2, w - width, h - width, 6)


_KIND_LABEL = {"symbols": "symbol", "footprints": "footprint", "models": "3D"}


class NewLibraryDialog(wx.Dialog):
    """"+ Create new library...": a name, plus a ticked-by-default checkbox for
    each of the other two library types, to give them the same name."""

    def __init__(self, parent, kind: str, name: str = "", also=None) -> None:
        super().__init__(parent, title="Create library")
        self.kind = kind
        others = [k for k in ("symbols", "footprints", "models") if k != kind]
        pad = 8
        s = wx.BoxSizer(wx.VERTICAL)
        s.Add(wx.StaticText(self, label=f"Name for the new {_KIND_LABEL[kind]} library:"),
              0, wx.LEFT | wx.RIGHT | wx.TOP, pad * 2)
        self.txt = wx.TextCtrl(self, value=name, size=wx.Size(340, -1))
        s.Add(self.txt, 0, wx.EXPAND | wx.ALL, pad * 2)
        self.chk: dict = {}
        for k in others:
            c = wx.CheckBox(self, label=f"Also use this name for the {_KIND_LABEL[k]} library")
            c.SetValue(True if also is None else k in also)
            self.chk[k] = c
            s.Add(c, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, pad * 2)
        buttons = self.CreateStdDialogButtonSizer(wx.OK | wx.CANCEL)
        s.Add(buttons, 0, wx.EXPAND | wx.ALL, pad)
        self.SetSizerAndFit(s)
        self.CentreOnParent()
        self.txt.SetFocus()
        self.txt.SelectAll()

    def get_result(self) -> tuple:
        kinds = [self.kind] + [k for k, c in self.chk.items() if c.GetValue()]
        return self.txt.GetValue().strip(), kinds


def _match_colour(level: int) -> wx.Colour:
    """Warning colour for a footprint suggestion: light yellow (closest),
    yellow, deep yellow (check before linking) — tuned for dark and light themes."""
    dark = wx.SystemSettings.GetColour(wx.SYS_COLOUR_WINDOW).GetLuminance() < 0.5
    if dark:
        rgb = {1: (255, 236, 153), 2: (250, 204, 21), 3: (245, 140, 0)}
    else:
        rgb = {1: (202, 168, 0), 2: (202, 128, 4), 3: (180, 83, 9)}
    return wx.Colour(*rgb.get(level, rgb[3]))


class _MatchChip(wx.Panel):
    """A clickable rounded chip: a coloured ⚠ and a footprint name."""

    def __init__(self, parent, name: str, level: int, why: str, on_pick) -> None:
        super().__init__(parent, style=wx.BORDER_NONE)
        self.name, self.level = name, level
        self._on_pick = on_pick
        self._selected = self._hover = False
        self.SetToolTip(f"{name}\n{why}\nClick to link this footprint instead of importing a new one.")
        self.SetCursor(wx.Cursor(wx.CURSOR_HAND))
        dc = wx.ClientDC(self)
        dc.SetFont(self.GetFont())
        self._icon = "\u26a0"
        iw, ih = dc.GetTextExtent(self._icon + " ")
        tw, th = dc.GetTextExtent(name)
        self._iw = iw
        self.SetMinSize(wx.Size(10 + iw + tw + 12, max(ih, th) + 10))
        self.Bind(wx.EVT_PAINT, self._paint)
        self.Bind(wx.EVT_LEFT_UP, lambda _e: self._on_pick(self.name))
        self.Bind(wx.EVT_ENTER_WINDOW, lambda _e: self._set_hover(True))
        self.Bind(wx.EVT_LEAVE_WINDOW, lambda _e: self._set_hover(False))

    def _set_hover(self, on: bool) -> None:
        self._hover = on
        self.Refresh()

    def set_selected(self, on: bool) -> None:
        if on != self._selected:
            self._selected = on
            self.Refresh()

    def _paint(self, _evt) -> None:
        dc = wx.PaintDC(self)
        gc = wx.GraphicsContext.Create(dc)
        if gc is None:
            return
        w, h = self.GetClientSize()
        if self._selected:
            pen = wx.Pen(wx.SystemSettings.GetColour(wx.SYS_COLOUR_HIGHLIGHT), 2)
        elif self._hover:
            pen = wx.Pen(wx.SystemSettings.GetColour(wx.SYS_COLOUR_WINDOWTEXT), 1)
        else:
            pen = wx.Pen(_muted_colour(), 1)
        gc.SetPen(pen)
        gc.SetBrush(wx.TRANSPARENT_BRUSH)
        gc.DrawRoundedRectangle(1, 1, w - 2, h - 2, (h - 2) / 2)
        font = self.GetFont()
        gc.SetFont(font, _match_colour(self.level))
        _tw, th = gc.GetTextExtent(self._icon)
        gc.DrawText(self._icon, 8, (h - th) / 2)
        gc.SetFont(font, wx.SystemSettings.GetColour(wx.SYS_COLOUR_WINDOWTEXT))
        _tw, th = gc.GetTextExtent(self.name)
        gc.DrawText(self.name, 8 + self._iw, (h - th) / 2)


class CopyPartsDialog(wx.Dialog):
    """Pick parts from another project's (or a global) library to copy into
    the Target libraries."""

    COLS = ("Symbol", "Footprint", "LCSC Part #", "In target")

    def __init__(self, parent, project_dirs: list, start: int, target_text: str,
                 target_names: set) -> None:
        super().__init__(parent, title="Copy parts from another project",
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self._projects = list(project_dirs)
        self._target_names = target_names
        self._parts: list = []
        self._shown: list = []
        self._checked: set = set()
        pad = 8
        s = wx.BoxSizer(wx.VERTICAL)
        grid = wx.FlexGridSizer(cols=3, vgap=pad, hgap=pad)
        grid.AddGrowableCol(1, 1)
        grid.Add(wx.StaticText(self, label="From:"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.cmb_from = wx.Choice(self)
        grid.Add(self.cmb_from, 1, wx.EXPAND)
        self.btn_browse = wx.Button(self, label="Browse…")
        grid.Add(self.btn_browse, 0)
        grid.Add(wx.StaticText(self, label="Library:"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.cmb_lib = wx.Choice(self)
        grid.Add(self.cmb_lib, 1, wx.EXPAND)
        grid.AddSpacer(1)
        grid.Add(wx.StaticText(self, label="Search:"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.txt_search = wx.SearchCtrl(self)
        self.txt_search.SetDescriptiveText("name, LCSC number or footprint")
        grid.Add(self.txt_search, 1, wx.EXPAND)
        grid.AddSpacer(1)
        s.Add(grid, 0, wx.EXPAND | wx.ALL, pad * 2)
        self.lst = wx.ListCtrl(self, style=wx.LC_REPORT | wx.LC_SINGLE_SEL, size=wx.Size(620, 260))
        self.lst.EnableCheckBoxes(True)
        for i, (col, w) in enumerate(zip(self.COLS, (190, 210, 100, 90))):
            self.lst.InsertColumn(i, col, width=w)
        s.Add(self.lst, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, pad * 2)
        self.lbl_into = wx.StaticText(self, label=f"Into: {target_text}")
        self.lbl_into.SetForegroundColour(_muted_colour())
        s.Add(self.lbl_into, 0, wx.ALL, pad * 2)
        buttons = wx.StdDialogButtonSizer()
        self.btn_ok = wx.Button(self, wx.ID_OK, label="Copy")
        buttons.AddButton(self.btn_ok)
        buttons.AddButton(wx.Button(self, wx.ID_CANCEL))
        buttons.Realize()
        s.Add(buttons, 0, wx.EXPAND | wx.ALL, pad)
        self.SetSizerAndFit(s)
        self.CentreOnParent()
        self._fill_from(start)
        self.cmb_from.Bind(wx.EVT_CHOICE, lambda _e: self._load_libs())
        self.cmb_lib.Bind(wx.EVT_CHOICE, lambda _e: self._load_parts())
        self.txt_search.Bind(wx.EVT_TEXT, lambda _e: self._show())
        self.btn_browse.Bind(wx.EVT_BUTTON, self._on_browse)
        self.lst.Bind(wx.EVT_LIST_ITEM_CHECKED, self._on_check)
        self.lst.Bind(wx.EVT_LIST_ITEM_UNCHECKED, self._on_check)

    # sources: each project, then "Global libraries"
    def _fill_from(self, select: int) -> None:
        labels = [f"{p.name}  —  {p}" for p in self._projects] + ["Global libraries"]
        self.cmb_from.Set(labels)
        self.cmb_from.SetSelection(max(0, min(select, len(labels) - 1)))
        self._load_libs()

    def _on_browse(self, _evt) -> None:
        with wx.FileDialog(self, "Choose a KiCad project", wildcard="KiCad project (*.kicad_pro)|*.kicad_pro",
                           style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST) as dlg:
            if dlg.ShowModal() != wx.ID_OK:
                return
            folder = Path(dlg.GetPath()).parent
        if all(str(folder).casefold() != str(p).casefold() for p in self._projects):
            self._projects.insert(0, folder)
        self._fill_from(next(i for i, p in enumerate(self._projects)
                             if str(p).casefold() == str(folder).casefold()))

    def _load_libs(self) -> None:
        i = self.cmb_from.GetSelection()
        self._sources = (copylib.sources_for_project(self._projects[i]) if i < len(self._projects)
                         else copylib.global_sources())
        self.cmb_lib.Set([src.nickname for src in self._sources] or ["(no libraries)"])
        self.cmb_lib.SetSelection(0)
        self._load_parts()

    def source(self):
        i = self.cmb_lib.GetSelection()
        return self._sources[i] if self._sources and 0 <= i < len(self._sources) else None

    def _load_parts(self) -> None:
        src = self.source()
        self._parts = sorted(copylib.read_parts(src.sym_path), key=lambda p: p.name.casefold()) if src else []
        self._checked = set()
        self._show()

    def _show(self) -> None:
        q = self.txt_search.GetValue().strip().casefold()
        self._shown = [p for p in self._parts
                       if not q or q in p.name.casefold() or q in p.lcsc.casefold() or q in p.footprint.casefold()]
        self.lst.DeleteAllItems()
        for row, part in enumerate(self._shown):
            self.lst.InsertItem(row, part.name)
            self.lst.SetItem(row, 1, part.footprint.split(":", 1)[-1])
            self.lst.SetItem(row, 2, part.lcsc)
            exists = part.name in self._target_names
            self.lst.SetItem(row, 3, "exists" if exists else "new")
            self.lst.CheckItem(row, part.name in self._checked)
        self._update_ok()

    def _on_check(self, evt) -> None:
        part = self._shown[evt.GetIndex()]
        if self.lst.IsItemChecked(evt.GetIndex()):
            self._checked.add(part.name)
        else:
            self._checked.discard(part.name)
        self._update_ok()

    def _update_ok(self) -> None:
        n = len(self._checked)
        self.btn_ok.SetLabel(f"Copy {n} part{'' if n == 1 else 's'}" if n else "Copy")
        self.btn_ok.Enable(n > 0)
        self.Layout()

    def chosen(self) -> list:
        return [p for p in self._parts if p.name in self._checked]


class FixLinksDialog(wx.Dialog):
    """One row per missing library: which symbols, where it was found, and
    how to fix it."""

    def __init__(self, parent, missing: dict, target_sym: str) -> None:
        super().__init__(parent, title="Fix library links")
        self._missing = missing
        labels = {
            fixlinks.FIX_COPY: f"Copy into '{target_sym}'",
            fixlinks.FIX_RELINK: "Re-link (same LCSC Part #)",
            fixlinks.FIX_RECOVER: f"Recover from schematic into '{target_sym}'",
            fixlinks.FIX_NONE: "Leave as is",
        }
        pad = 8
        s = wx.BoxSizer(wx.VERTICAL)
        s.Add(wx.StaticText(self, label="These symbols link to libraries this project doesn't have:"),
              0, wx.ALL, pad * 2)
        grid = wx.FlexGridSizer(cols=4, vgap=pad, hgap=pad * 2)
        for h in ("Missing library", "Symbols", "Found in", "Fix"):
            t = wx.StaticText(self, label=h)
            t.SetForegroundColour(_muted_colour())
            grid.Add(t)
        self._choices = {}
        for nick, ml in missing.items():
            refs = sorted({r.reference for r in ml.refs})
            grid.Add(wx.StaticText(self, label=nick), 0, wx.ALIGN_CENTER_VERTICAL)
            shown = ", ".join(refs[:6]) + (" …" if len(refs) > 6 else "")
            grid.Add(wx.StaticText(self, label=f"{len(ml.refs)}  ({shown})"), 0, wx.ALIGN_CENTER_VERTICAL)
            where = (ml.source.label if ml.source else
                     "same LCSC Part # here" if fixlinks.FIX_RELINK in ml.options() else "not found")
            grid.Add(wx.StaticText(self, label=where), 0, wx.ALIGN_CENTER_VERTICAL)
            opts = ml.options()
            ch = wx.Choice(self, choices=[labels[o] for o in opts])
            ch.SetSelection(opts.index(ml.fix) if ml.fix in opts else 0)
            grid.Add(ch, 0, wx.EXPAND)
            self._choices[nick] = (ch, opts)
        s.Add(grid, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, pad * 2)
        note = wx.StaticText(self, label=(
            "PartHive rewrites the schematic (a backup is kept) and re-links footprints on the open "
            "board — save the board afterwards. The Schematic Editor must be closed while it works."))
        note.Wrap(560)
        note.SetForegroundColour(_muted_colour())
        s.Add(note, 0, wx.ALL, pad * 2)
        buttons = wx.StdDialogButtonSizer()
        n = sum(len(ml.refs) for ml in missing.values())
        buttons.AddButton(wx.Button(self, wx.ID_OK, label=f"Fix {n} symbol{'' if n == 1 else 's'}"))
        buttons.AddButton(wx.Button(self, wx.ID_CANCEL))
        buttons.Realize()
        s.Add(buttons, 0, wx.EXPAND | wx.ALL, pad)
        self.SetSizerAndFit(s)
        self.CentreOnParent()

    def apply_choices(self) -> None:
        for nick, (ch, opts) in self._choices.items():
            self._missing[nick].fix = opts[ch.GetSelection()]


class LibraryActionDialog(wx.Dialog):
    """Rename or Delete a library. A dropdown picks WHICH library (starting on
    the one selected in the main window). The other two library types get a
    checkbox each: ticked and enabled when a library with the same name exists,
    unticked and greyed out when it doesn't — updated as the pick changes."""

    def __init__(self, parent, action: str, kind: str, choices: list, selected: str,
                 available_for, new_name: str = "", also=None) -> None:
        verb = "Rename" if action == "rename" else "Delete"
        super().__init__(parent, title=f"{verb} library")
        self.action, self.kind, self.verb = action, kind, verb
        self._available_for = available_for
        self._also = also
        pad = 8
        s = wx.BoxSizer(wx.VERTICAL)
        grid = wx.FlexGridSizer(cols=2, vgap=pad, hgap=pad)
        grid.AddGrowableCol(1, 1)
        grid.Add(wx.StaticText(self, label=f"{_KIND_LABEL[kind].capitalize()} library:"),
                 0, wx.ALIGN_CENTER_VERTICAL)
        self.cmb = wx.Choice(self, choices=choices)
        self.cmb.SetMinSize(wx.Size(300, -1))
        if not self.cmb.SetStringSelection(selected) and choices:
            self.cmb.SetSelection(0)
        grid.Add(self.cmb, 1, wx.EXPAND)
        if action == "rename":
            grid.Add(wx.StaticText(self, label="New name:"), 0, wx.ALIGN_CENTER_VERTICAL)
            self.txt = wx.TextCtrl(self, value=new_name or self.target())
            grid.Add(self.txt, 1, wx.EXPAND)
        else:
            self.txt = None
        s.Add(grid, 0, wx.EXPAND | wx.ALL, pad * 2)
        self.chk: dict = {}
        for k in ("symbols", "footprints", "models"):
            if k != kind:
                self.chk[k] = wx.CheckBox(self, label="")
                s.Add(self.chk[k], 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, pad * 2)
        if action == "rename":
            note = ("KiCad's library list, the footprint links in these symbols and the 3D "
                    "paths in these footprints are updated, and a pin moves to the new name. "
                    "Schematics and boards that already use these parts keep the old name — "
                    "re-link them with Tools > Change Symbols / Change Footprints. Afterwards, "
                    "restart KiCad (or reopen the project).")
        else:
            note = ("The files go to the Recycle Bin, and the library is removed from KiCad's "
                    "library list and unpinned. Schematics and boards that use these parts "
                    "keep their copies but lose the library link. Afterwards, restart KiCad "
                    "(or reopen the project).")
        row = wx.BoxSizer(wx.HORIZONTAL)
        row.Add(wx.StaticBitmap(self, bitmap=wx.ArtProvider.GetBitmap(
            wx.ART_WARNING, wx.ART_MESSAGE_BOX, wx.Size(16, 16))), 0, wx.TOP | wx.RIGHT, 2)
        txt = wx.StaticText(self, label=note)
        txt.Wrap(400)
        row.Add(txt, 1)
        s.Add(row, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, pad * 2)
        buttons = wx.StdDialogButtonSizer()
        buttons.AddButton(wx.Button(self, wx.ID_OK, label=verb))
        buttons.AddButton(wx.Button(self, wx.ID_CANCEL))
        buttons.Realize()
        s.Add(buttons, 0, wx.EXPAND | wx.ALL, pad)
        self._sync(first=True)
        self.SetSizerAndFit(s)
        self.CentreOnParent()
        self.cmb.Bind(wx.EVT_CHOICE, lambda _e: self._sync())
        if self.txt is not None:
            self.txt.SetFocus()
            self.txt.SelectAll()
        else:
            self.FindWindowById(wx.ID_CANCEL, self).SetFocus()  # safer default for delete

    def target(self) -> str:
        return self.cmb.GetStringSelection()

    def _sync(self, first: bool = False) -> None:
        """Pick changed: update the checkbox labels/state (and the new name)."""
        name = self.target()
        available = self._available_for(name) if name else {}
        verb = self.verb.lower()
        for k, c in self.chk.items():
            if available.get(k):
                c.SetLabel(f"Also {verb} the {_KIND_LABEL[k]} library '{name}'")
                c.Enable(True)
                c.SetValue(True if (self._also is None or not first) else k in self._also)
            else:
                c.SetLabel(f"Also {verb} the {_KIND_LABEL[k]} library (there is none called '{name}')")
                c.SetValue(False)
                c.Enable(False)
            c.InvalidateBestSize()  # the label length changed: re-measure it
        if self.txt is not None and not first:
            self.txt.SetValue(name)
            self.txt.SetFocus()
            self.txt.SelectAll()
        if not first:
            self.GetSizer().Layout()
            self.Fit()
            self.Refresh()

    def get_result(self) -> tuple:
        """(library, new name, [kinds])"""
        kinds = [self.kind] + [k for k, c in self.chk.items() if c.IsEnabled() and c.GetValue()]
        return self.target(), (self.txt.GetValue().strip() if self.txt else ""), kinds


class ResolutionDialog(wx.Dialog):
    """Per-item confirmation dialog (shown when "Confirm each import" is on):
    presents the proposed symbol / footprint /
    3D-model names, lets the user edit them, and — per item — choose Import
    (Overwrite), Skip, or Use existing (footprint & 3D only)."""

    def __init__(self, parent, plan):
        super().__init__(parent, title=f"{BRAND} — confirm import", style=wx.DEFAULT_DIALOG_STYLE)
        self.plan = plan
        self._rows: dict = {}
        # 3D model choice saved when "Use existing" footprint auto-skipped it
        # (None = not auto-skipped, or the user has changed the 3D row since).
        self._model_before_auto_skip: int | None = None
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

        grid = wx.GridBagSizer(vgap=pad, hgap=pad)
        self._grid_row = 0
        for col, header in enumerate(("Item", "Name", "Action", "Use existing")):
            grid.Add(wx.StaticText(panel, label=header), pos=(0, col), flag=wx.ALIGN_CENTER_VERTICAL)
        self._grid_row = 1

        self._add_row(panel, grid, "symbol", "Symbol", self.plan.symbol, allow_existing=False)
        self._add_row(panel, grid, "footprint", "Footprint", self.plan.footprint, allow_existing=True)
        self._add_match_row(panel, grid, self.plan.footprint)
        self._add_row(panel, grid, "model", "3D model", self.plan.model, allow_existing=True)
        grid.AddGrowableCol(1, 1)
        grid.AddGrowableCol(3, 1)
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
        ok = wx.Button(panel, wx.ID_OK, "Import")
        ok.SetDefault()
        cancel = wx.Button(panel, wx.ID_CANCEL, "Cancel")
        btnrow.Add(ok, 0, wx.RIGHT, pad)
        btnrow.Add(cancel, 0)
        root.Add(btnrow, 0, wx.EXPAND | wx.ALL, pad)

        panel.SetSizer(root)
        outer = wx.BoxSizer(wx.VERTICAL)
        outer.Add(panel, 1, wx.EXPAND)
        self.SetSizer(outer)

    def _add_row(self, panel, grid, kind, label, item, allow_existing) -> None:
        r = self._grid_row
        self._grid_row += 1
        if item is None:
            grid.Add(wx.StaticText(panel, label=f"{label}: (none)"), pos=(r, 0), flag=wx.ALIGN_CENTER_VERTICAL)
            grid.Add(wx.StaticText(panel, label="—"), pos=(r, 1), flag=wx.ALIGN_CENTER_VERTICAL)
            return

        status = "exists" if item.exists else "new"
        grid.Add(wx.StaticText(panel, label=f"{label}  ({status})"), pos=(r, 0), flag=wx.ALIGN_CENTER_VERTICAL)

        name_ctrl = wx.TextCtrl(panel, value=item.name)
        grid.Add(name_ctrl, pos=(r, 1), flag=wx.EXPAND)

        actions = []
        if item.exists:
            actions.append(("Overwrite", ACTION_IMPORT))
            actions.append(("Skip (keep existing)", ACTION_SKIP))
        else:
            actions.append(("Import", ACTION_IMPORT))
            actions.append(("Skip (don't import)", ACTION_SKIP))
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
        grid.Add(choice, pos=(r, 2), flag=wx.ALIGN_CENTER_VERTICAL)

        existing_combo = wx.Choice(panel, choices=list(item.existing or []))
        if item.existing:
            existing_combo.SetSelection(0)
        existing_combo.Enable(False)
        grid.Add(existing_combo, pos=(r, 3), flag=wx.EXPAND)
        existing_combo.Bind(wx.EVT_CHOICE, lambda _e: self._sync_chips())

        self._rows[kind] = {
            "item": item,
            "name": name_ctrl,
            "choice": choice,
            "actions": actions,
            "existing": existing_combo,
        }
        choice.Bind(wx.EVT_CHOICE, lambda _e, k=kind: self._on_action(k))
        self._sync_row(kind)

    def _add_match_row(self, panel, grid, item) -> None:
        """'Similar in this library:' + one chip per suggested footprint."""
        self._chips: list = []
        if item is None or not getattr(item, "suggestions", None) or "footprint" not in self._rows:
            return
        r = self._grid_row
        self._grid_row += 1
        label = wx.StaticText(panel, label="Similar in this library:")
        label.SetForegroundColour(_muted_colour())
        grid.Add(label, pos=(r, 0), flag=wx.ALIGN_CENTER_VERTICAL)
        row = wx.BoxSizer(wx.HORIZONTAL)
        for name, level, why in item.suggestions:
            chip = _MatchChip(panel, name, level, why, self._pick_suggestion)
            self._chips.append(chip)
            row.Add(chip, 0, wx.RIGHT, 6)
        grid.Add(row, pos=(r, 1), span=(1, 3), flag=wx.ALIGN_CENTER_VERTICAL)

    def _pick_suggestion(self, name: str) -> None:
        """Chip clicked: link that footprint (and skip the 3D model). Clicking
        the chip that is already linked goes back to importing a new one."""
        row = self._rows["footprint"]
        def index_of(action):
            return next((i for i, (_l, a) in enumerate(row["actions"]) if a == action), None)
        linked = (self._row_action("footprint") == ACTION_USE_EXISTING
                  and row["existing"].GetStringSelection() == name)
        if linked:
            row["choice"].SetSelection(index_of(ACTION_IMPORT))
        else:
            use = index_of(ACTION_USE_EXISTING)
            if use is None or not row["existing"].SetStringSelection(name):
                return
            row["choice"].SetSelection(use)
        self._on_action("footprint")

    def _sync_chips(self) -> None:
        row = self._rows.get("footprint")
        for chip in getattr(self, "_chips", []):
            chip.set_selected(row is not None
                              and self._row_action("footprint") == ACTION_USE_EXISTING
                              and row["existing"].GetStringSelection() == chip.name)

    def _on_action(self, kind) -> None:
        """An Action was changed by the user."""
        self._sync_row(kind)
        self._sync_chips()
        model = self._rows.get("model")
        if kind == "model":
            self._model_before_auto_skip = None  # the user decides the 3D row now
        elif kind == "footprint" and model is not None:
            skip = next((i for i, (_l, a) in enumerate(model["actions"]) if a == ACTION_SKIP), None)
            if self._row_action("footprint") == ACTION_USE_EXISTING:
                # An existing footprint already has (or doesn't need) its 3D model:
                # don't import a new one unless the user switches it back.
                if skip is not None and self._row_action("model") != ACTION_SKIP:
                    self._model_before_auto_skip = model["choice"].GetSelection()
                    model["choice"].SetSelection(skip)
                    self._sync_row("model")
            elif self._model_before_auto_skip is not None:
                # Footprint is imported again: restore the 3D choice we changed.
                model["choice"].SetSelection(self._model_before_auto_skip)
                self._model_before_auto_skip = None
                self._sync_row("model")

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


LAYOUT_INFO = """Default — Single folder will store everything

Libraries/
 ├── AddedParts.kicad_sym
 ├── AddedParts.pretty/
 └── AddedParts.3dshapes/


Organized — Separate folders keep symbols, footprints and 3D models apart,
which makes a large library much easier to manage.

Libraries/
 ├── symbols/
 │    ├── Capacitors.kicad_sym
 │    └── Resistors.kicad_sym
 ├── footprints/
 │    ├── Capacitors.pretty/
 │    └── Resistors.pretty/
 └── 3dmodels/
      ├── Capacitors.3dshapes/
      └── Resistors.3dshapes/


The layout is remembered per library directory, together with the symbol,
footprint and 3D library you last used there.
"""


class LayoutInfoDialog(wx.Dialog):
    """Explains the two library layouts, with an on-disk example of each."""

    def __init__(self, parent):
        super().__init__(
            parent,
            title=f"{BRAND} — library layouts",
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        pad = 8
        panel = wx.Panel(self)
        root = wx.BoxSizer(wx.VERTICAL)
        txt = wx.TextCtrl(
            panel, value=LAYOUT_INFO, style=wx.TE_MULTILINE | wx.TE_READONLY | wx.HSCROLL
        )
        # Monospaced so the folder trees line up.
        try:
            txt.SetFont(
                wx.Font(
                    9, wx.FONTFAMILY_TELETYPE, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL
                )
            )
        except Exception:  # noqa: BLE001
            pass
        txt.SetMinSize(wx.Size(520, 430))
        root.Add(txt, 1, wx.EXPAND | wx.ALL, pad)
        btns = wx.BoxSizer(wx.HORIZONTAL)
        btns.AddStretchSpacer(1)
        close = wx.Button(panel, wx.ID_OK, "Close")
        close.SetDefault()
        btns.Add(close, 0)
        root.Add(btns, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, pad)
        panel.SetSizer(root)
        outer = wx.BoxSizer(wx.VERTICAL)
        outer.Add(panel, 1, wx.EXPAND)
        self.SetSizer(outer)
        self.Fit()
        self.CentreOnParent()

    def ShowAndDestroy(self) -> None:
        try:
            self.ShowModal()
        finally:
            self.Destroy()


class ParthiveDialog(wx.Dialog):
    def __init__(self, parent: wx.Window | None = None):
        super().__init__(
            parent,
            title=_TITLE,
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        self.config = ParthiveConfig.load()
        self._scope = "global"  # set properly in _load_into_widgets
        self._project: ProjectSettings | None = None
        self._global_dir = ""
        self._importing = False
        # None = no layout chosen yet, True = Organized, False = Default(flat).
        self._layout: bool | None = None
        self._focus_applied = False
        self._build()
        self._load_into_widgets()
        self.SetMinSize(wx.Size(560, 720))
        self.Fit()
        self.CentreOnParent()
        self._set_initial_focus()

    # --------------------------------------------------------------- layout
    def _build(self) -> None:
        pad = 6
        panel = wx.Panel(self)
        self._panel = panel
        root = wx.BoxSizer(wx.VERTICAL)

        # Helper: a StaticBoxSizer whose children are parented to the StaticBox
        # (the wx-recommended pattern; avoids "should be created as child of its
        # wxStaticBox" assertions).
        def static_box(title, orient=wx.VERTICAL):
            box = wx.StaticBox(panel, label=title)
            return box, wx.StaticBoxSizer(box, orient)

        # --- Library location -------------------------------------------
        loc_sb, loc_box = static_box("Library location")
        # Two cards: Global library | Project library. The choice is remembered
        # per project (a project never seen before opens on Global).
        cards = wx.BoxSizer(wx.HORIZONTAL)
        self.card_global = _ScopeCard(loc_sb, "Global library", self._on_card_global)
        self.card_project = _ScopeCard(loc_sb, "Project library", self._on_card_project)
        self.card_global.SetToolTip(
            "Registered in KiCad's global library tables — available in every "
            "project. Restart KiCad after adding a new library."
        )
        self.card_project.SetToolTip(
            "Registered in this project's own library tables (next to the "
            ".kicad_pro), using ${KIPRJMOD} paths. Reopen the project after adding "
            "a new library."
        )
        cards.Add(self.card_global, 1, wx.EXPAND | wx.RIGHT, pad)
        cards.Add(self.card_project, 1, wx.EXPAND)
        loc_box.Add(cards, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, pad)

        loc_row = wx.BoxSizer(wx.HORIZONTAL)
        loc_row.Add(wx.StaticText(loc_sb, label="Library dir:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, pad)
        # Pick-only: choose a recent dir from the dropdown or use Browse... - no
        # typing, so a half-typed path can never become the library dir.
        self.txt_dir = wx.ComboBox(loc_sb, style=wx.CB_READONLY)
        # Fixed minimum width: otherwise Windows sizes the box to its longest
        # path and the whole dialog grows. The open list still shows full paths
        # (see _fit_dir_dropdown).
        self.txt_dir.SetMinSize(wx.Size(260, -1))
        loc_row.Add(self.txt_dir, 1, wx.EXPAND | wx.RIGHT, pad)
        self.btn_forget = wx.Button(loc_sb, label="✕", size=wx.Size(26, 24), style=wx.BU_EXACTFIT)
        self.btn_forget.SetToolTip(
            "Remove the shown directory from this list (the folder and its files are NOT deleted)"
        )
        loc_row.Add(self.btn_forget, 0, wx.RIGHT, pad)
        self.btn_browse = wx.Button(loc_sb, label="Browse…")
        loc_row.Add(self.btn_browse, 0, wx.RIGHT, pad)
        self.btn_change_project = wx.Button(loc_sb, label="Change project…")
        self.btn_change_project.SetToolTip("Pick a different KiCad project (.kicad_pro)")
        loc_row.Add(self.btn_change_project, 0)
        loc_box.Add(loc_row, 0, wx.EXPAND | wx.ALL, pad)
        # Broken library links in the schematic (e.g. a pasted circuit) — shown
        # only when there are some.
        self.fix_row = wx.BoxSizer(wx.HORIZONTAL)
        self.fix_icon = wx.StaticBitmap(loc_sb, bitmap=wx.ArtProvider.GetBitmap(
            wx.ART_WARNING, wx.ART_MESSAGE_BOX, wx.Size(16, 16)))
        self.fix_text = wx.StaticText(loc_sb, label="")
        self.btn_fix = wx.Button(loc_sb, label="Fix links…")
        self.fix_row.Add(self.fix_icon, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, pad)
        self.fix_row.Add(self.fix_text, 1, wx.ALIGN_CENTER_VERTICAL)
        self.fix_row.Add(self.btn_fix, 0, wx.LEFT, pad)
        loc_box.Add(self.fix_row, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, pad)
        self._missing: dict = {}
        root.Add(loc_box, 0, wx.EXPAND | wx.ALL, pad)

        # --- Target libraries -------------------------------------------
        name_sb, name_box = static_box("Target libraries")
        # Library layout — remembered per dir. Flat = all libraries in one folder;
        # Organized = separate symbols/ footprints/ 3dmodels/ subfolders.
        layout_row = wx.BoxSizer(wx.HORIZONTAL)
        layout_row.Add(
            wx.StaticText(name_sb, label="Layout:"),
            0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, pad,
        )
        self.rb_flat = wx.RadioButton(
            name_sb,
            label="Default — Single folder: .kicad_sym + .pretty + .3dshapes (same directory)",
            style=wx.RB_GROUP,
        )
        self.rb_org = wx.RadioButton(
            name_sb,
            label="Organized — Separate folders: symbols/ + footprints/ + 3dmodels/ (dedicated subfolders)",
        )
        self.rb_flat.SetToolTip(
            "Default (flat): every library sits directly in the library dir — the "
            "layout used by most KiCad libraries."
        )
        self.rb_org.SetToolTip(
            "Organized: libraries are split by type into symbols/, footprints/ and "
            "3dmodels/ subfolders — tidier for large libraries."
        )
        # No layout is pre-selected: the user picks one (see _apply_dir_layout).
        self.rb_flat.SetValue(False)
        self.rb_org.SetValue(False)
        rb_col = wx.BoxSizer(wx.VERTICAL)
        rb_col.Add(self.rb_flat, 0, wx.BOTTOM, 2)
        rb_col.Add(self.rb_org, 0)
        layout_row.Add(rb_col, 0, wx.ALIGN_TOP)
        self.btn_layout_info = wx.Button(
            name_sb, label="i", size=wx.Size(26, 24), style=wx.BU_EXACTFIT
        )
        self.btn_layout_info.SetToolTip("What do these layouts look like on disk?")
        layout_row.Add(self.btn_layout_info, 0, wx.LEFT | wx.ALIGN_TOP, pad)
        name_box.Add(layout_row, 0, wx.ALL, 3)

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

        # Live notice: new library / needs a KiCad restart / name clash.
        self.notice_row = wx.BoxSizer(wx.HORIZONTAL)
        self.notice_icon = wx.StaticBitmap(name_sb, bitmap=wx.ArtProvider.GetBitmap(
            wx.ART_INFORMATION, wx.ART_MESSAGE_BOX, wx.Size(16, 16)))
        self.notice_text = wx.StaticText(name_sb, label="")
        self.notice_row.Add(self.notice_icon, 0, wx.TOP | wx.RIGHT, 2)
        self.notice_row.Add(self.notice_text, 1, wx.EXPAND)
        name_box.Add(self.notice_row, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, pad)
        self._session_registered: set = set()  # (kind, name, scope) added this session

        hint_row = wx.BoxSizer(wx.HORIZONTAL)
        self.btn_refresh = wx.Button(name_sb, label="Refresh lists")
        hint_row.Add(self.btn_refresh, 0, wx.RIGHT, pad)
        self.btn_show_paths = wx.Button(name_sb, label="Show paths")
        hint_row.Add(self.btn_show_paths, 0, wx.RIGHT, pad)
        self.btn_check_lib = wx.Button(name_sb, label="Check library")
        self.btn_check_lib.SetToolTip(
            "Check every footprint in the target library for problems that stop KiCad "
            "loading it. Nothing is changed unless you confirm the repair."
        )
        hint_row.Add(self.btn_check_lib, 0, wx.RIGHT, pad)
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

        # --- Add parts ---------------------------------------------------
        # One grid, so both rows line up: label | field | button (same width).
        imp_sb, imp_box = static_box("Add parts")
        add_grid = wx.FlexGridSizer(cols=3, vgap=pad, hgap=pad)
        add_grid.AddGrowableCol(1, 1)
        add_grid.Add(wx.StaticText(imp_sb, label="From EasyEDA / JLCPCB (LCSC):"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.txt_component = wx.TextCtrl(imp_sb, style=wx.TE_PROCESS_ENTER)
        self.txt_component.SetHint("e.g. C25804")
        add_grid.Add(self.txt_component, 1, wx.EXPAND | wx.ALIGN_CENTER_VERTICAL)
        self.btn_import = wx.Button(imp_sb, label="Import")
        self.btn_import.SetDefault()
        add_grid.Add(self.btn_import, 0, wx.EXPAND)
        add_grid.Add(wx.StaticText(imp_sb, label="From another project:"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.cmb_source = wx.Choice(imp_sb)
        self.cmb_source.SetToolTip("Where to copy parts from: another project's library, "
                                   "your global libraries, or any project (Browse...).")
        add_grid.Add(self.cmb_source, 1, wx.EXPAND | wx.ALIGN_CENTER_VERTICAL)
        self.btn_copy = wx.Button(imp_sb, label="Copy parts…")
        self.btn_copy.SetToolTip("Pick parts from that library and copy them — symbol, footprint "
                                 "and 3D model, with their links fixed — into the Target libraries.")
        add_grid.Add(self.btn_copy, 0, wx.EXPAND)
        width = max(self.btn_import.GetBestSize().width, self.btn_copy.GetBestSize().width)
        for b in (self.btn_import, self.btn_copy):
            b.SetMinSize(wx.Size(width, -1))
        imp_box.Add(add_grid, 0, wx.EXPAND | wx.ALL, pad)
        root.Add(imp_box, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, pad)

        # --- Options -----------------------------------------------------
        # Two aligned columns of short labels; the details are in the tooltips.
        opt_sb, opt_box = static_box("Options")

        def option(label: str, tip: str) -> wx.CheckBox:
            c = wx.CheckBox(opt_sb, label=label)
            c.SetToolTip(tip)
            return c

        self.chk_confirm = option("Confirm each import",
                                  "Show the per-item dialog (Import / Skip / Use existing, similar "
                                  "footprints) before writing. Turn off to import in one shot.")
        self.chk_skip3d = option("Skip 3D model", "Don't download the 3D model (faster, less bandwidth).")
        self.chk_step = option("Prefer STEP (skip WRL)", "Keep only the STEP 3D model and drop the WRL.")
        self.chk_compress = option("Compress STEP (.step.gz)", "Save the STEP model compressed.")
        self.chk_overwrite = option("Overwrite existing parts",
                                    "When 'Confirm each import' is off: replace parts that already exist.")
        self.chk_close = option("Close window after import",
                                "Close PartHive after a successful import, so it stops blocking KiCad.")
        self.chk_register = option("Register libraries in KiCad",
                                   "Add new libraries to KiCad's library list (global or the project's).")
        self.chk_pin = option(
            "Pin project libraries on top",
            "Pin project libraries at the top of Add Symbol / Add Footprint. KiCad only keeps a pin "
            "written while the project is closed, so PartHive pins a new project library when you "
            "close the project (or right away if it is closed). Each library is pinned once — if "
            "you unpin it in KiCad, PartHive leaves it unpinned.")
        opt_grid = wx.FlexGridSizer(cols=2, vgap=4, hgap=pad * 4)
        opt_grid.AddGrowableCol(0, 1)
        opt_grid.AddGrowableCol(1, 1)
        for c in (self.chk_confirm, self.chk_skip3d, self.chk_step, self.chk_compress,
                  self.chk_overwrite, self.chk_close, self.chk_register, self.chk_pin):
            opt_grid.Add(c, 0, wx.ALIGN_CENTER_VERTICAL)
        # Text sizes: one per column, under the checkboxes, labels the same width.
        self.spin_sym = wx.SpinCtrlDouble(opt_sb, min=0.1, max=20.0, inc=0.01)
        self.spin_sym.SetDigits(2)
        self.spin_fp = wx.SpinCtrlDouble(opt_sb, min=0.1, max=20.0, inc=0.01)
        self.spin_fp.SetDigits(2)
        labels = [wx.StaticText(opt_sb, label=t)
                  for t in ("Symbol text (mm):", "Footprint text (mm):", "Pin types:")]
        lw = max(lbl.GetBestSize().width for lbl in labels)
        self.cmb_pins = wx.Choice(opt_sb, choices=[PIN_TYPE_LABELS[m] for m in PIN_TYPE_MODES])
        self.cmb_pins.SetToolTip(
            "Electrical pin types for imported symbols (EasyEDA's are mostly undefined or wrong).\n"
            "Smart: discrete parts (R, C, L, D, Q, ...) passive; IC pins by name — GND/VCC/VDD/VIN... "
            "power in, VOUT power out (repeats passive), NC no connect, others EasyEDA's type or "
            "bidirectional. Nets fed only by connectors or resistors then need a PWR_FLAG.")
        for lbl, ctrl in zip(labels, (self.spin_sym, self.spin_fp, self.cmb_pins)):
            lbl.SetMinSize(wx.Size(lw, -1))
            cell = wx.BoxSizer(wx.HORIZONTAL)
            cell.Add(lbl, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, pad)
            cell.Add(ctrl, 0)
            opt_grid.Add(cell, 0, wx.TOP, 4)
        opt_box.Add(opt_grid, 0, wx.EXPAND | wx.ALL, pad)
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
        self.btn_forget.Bind(wx.EVT_BUTTON, self._on_forget_dir)
        self.btn_layout_info.Bind(wx.EVT_BUTTON, self._on_layout_info)
        self.btn_import.Bind(wx.EVT_BUTTON, self.on_import)
        self.txt_component.Bind(wx.EVT_TEXT_ENTER, self.on_import)
        self.btn_open_dir.Bind(wx.EVT_BUTTON, self.on_open_dir)
        self.btn_close.Bind(wx.EVT_BUTTON, lambda _e: self.Close())
        self.Bind(wx.EVT_CLOSE, self.on_close)
        self.Bind(wx.EVT_SHOW, self._on_shown)
        self.Bind(wx.EVT_CHAR_HOOK, self._on_char_hook)

        self.txt_dir.Bind(wx.EVT_COMBOBOX, self._on_dir_change)
        self.rb_flat.Bind(wx.EVT_RADIOBUTTON, self._on_layout_change)
        self.rb_org.Bind(wx.EVT_RADIOBUTTON, self._on_layout_change)
        self.btn_refresh.Bind(wx.EVT_BUTTON, lambda _e: self._refresh_library_choices())
        self.btn_show_paths.Bind(wx.EVT_BUTTON, self._on_show_paths)
        self.btn_check_lib.Bind(wx.EVT_BUTTON, self._on_check_library)
        for _cmb in (self.cmb_sym, self.cmb_fp, self.cmb_3d):
            _cmb.Bind(wx.EVT_COMBOBOX, self._on_lib_combo)

        self.btn_change_project.Bind(wx.EVT_BUTTON, self._on_change_project)
        self.btn_copy.Bind(wx.EVT_BUTTON, self._on_copy_parts)
        self.btn_fix.Bind(wx.EVT_BUTTON, self._on_fix_links)
        self.chk_register.Bind(wx.EVT_CHECKBOX, lambda _e: self._repoint_libs())

    # ----------------------------------------------------------- data <-> UI
    def _load_into_widgets(self) -> None:
        c = self.config
        # Global library dir lives in settings.json; the project's choice and its
        # library dir live in that project's own file (see ProjectSettings).
        self._global_dir = c.library_dir or str(default_library_dir(c.model_lib_name))
        self._project = None
        found = detect_open_project()
        if found is not None and is_project_dir(found):
            self._project = ProjectSettings.load(found)
        self._scope = self._project.scope if self._project else "global"
        active = self._active_dir()
        self._reload_dir_history(active)
        self._apply_dir_layout(active)
        self._update_scope_ui()
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
        self.chk_confirm.SetValue(c.confirm_import)
        self.chk_overwrite.SetValue(c.overwrite)
        self.chk_register.SetValue(c.auto_register)
        self.chk_pin.SetValue(c.pin_project_libs)
        mode = c.pin_types if c.pin_types in PIN_TYPE_MODES else PIN_TYPE_MODES[0]
        self.cmb_pins.SetSelection(PIN_TYPE_MODES.index(mode))
        self.chk_close.SetValue(c.close_after_import)
        self.spin_sym.SetValue(c.symbol_text_size)
        self.spin_fp.SetValue(c.footprint_text_size)
        # Intentionally start with an empty part-number field on each open.
        self.txt_component.SetValue("")
        self._repoint_libs()
        # Safety net: project libraries PartHive added earlier but never pinned.
        if c.pin_project_libs and self._project is not None:
            self._report_pins(ensure_pins(self._project.project_dir))
        self._fill_sources()
        self._scan_links()

    def _reload_dir_history(self, current: str | None = None) -> None:
        """Fill the Library-dir dropdown with recent dirs (+ the default),
        keeping ``current`` (or the field's text) as the shown value."""
        c = self.config
        cur = (current if current is not None else self.txt_dir.GetValue()).strip()
        items: list[str] = []
        if self._scope == "project" and self._project is not None:
            # Project card: offer the project's default folder and recent dirs
            # that sit inside this project.
            proj = self._project.project_dir
            inside = [d for d in c.library_dir_history if _is_inside(d, proj)]
            candidates = [cur, str(Path(proj) / PROJECT_LIB_SUBDIR)] + inside
        else:
            candidates = [cur] + list(c.library_dir_history) + [str(default_library_dir())]
        for d in candidates:
            d = (d or "").strip()
            if d and d not in items:
                items.append(d)
        self.txt_dir.Set(items)
        if cur:
            self.txt_dir.SetStringSelection(cur)
        self._fit_dir_dropdown(items)

    def _fit_dir_dropdown(self, items: list) -> None:
        """Let the open dropdown be wider than the box, so every full path shows
        on one line (Windows clips the list to the box width by default)."""
        if os.name != "nt" or not items:
            return
        try:
            dc = wx.ClientDC(self.txt_dir)
            dc.SetFont(self.txt_dir.GetFont())
            longest = max(dc.GetTextExtent(i)[0] for i in items)
            want = longest + 40  # padding + room for a scrollbar
            limit = wx.GetDisplaySize().width - 40
            width = max(self.txt_dir.GetSize().width, min(want, limit))
            import ctypes

            CB_SETDROPPEDWIDTH = 0x0160
            ctypes.windll.user32.SendMessageW(
                ctypes.c_void_p(int(self.txt_dir.GetHandle())), CB_SETDROPPEDWIDTH, width, 0
            )
        except Exception:  # noqa: BLE001 - cosmetic only
            pass

    def _remember_dir(self, path: str) -> None:
        """Push a directory to the front of the persisted history (dedup, cap 12).
        Only global dirs go in this list; a project keeps its own dir."""
        path = (path or "").strip()
        if not path or self._scope == "project":
            return
        rest = [d for d in self.config.library_dir_history if (d or "").strip() and d != path]
        self.config.library_dir_history = [path] + rest[:11]

    def _on_shown(self, evt) -> None:
        """wx assigns focus to the first control when the dialog is shown, so
        re-apply our choice the first time it becomes visible."""
        try:
            shown = evt.IsShown()
        except Exception:  # noqa: BLE001
            shown = True
        if shown and not self._focus_applied:
            self._focus_applied = True
            wx.CallAfter(self._set_initial_focus)
        evt.Skip()

    def _dir_configured(self) -> bool:
        """True when a library directory is filled in (whatever its contents)."""
        return bool(self.txt_dir.GetValue().strip())

    def _set_initial_focus(self) -> None:
        """Put the caret where the user most likely wants it: the part-number box
        (so an LCSC number can be pasted straight away) whenever a library dir is
        already filled in — otherwise the empty library-dir field."""
        try:
            if self._dir_configured():
                self.txt_component.SetFocus()
                self.txt_component.SetInsertionPointEnd()
            else:
                self.txt_dir.SetFocus()
        except Exception:  # noqa: BLE001
            pass

    def _remember_libs(self) -> None:
        """Persist the three currently selected libraries for the current dir, so
        coming back to that dir restores them (not just the layout)."""
        libdir = self.txt_dir.GetValue().strip()
        if not libdir or self._layout is None:
            return
        sym, fp, td = self._current_names()
        self.config.dir_libs[libdir] = {"symbols": sym, "footprints": fp, "models": td}

    def _on_forget_dir(self, _evt=None) -> None:
        """Remove the shown directory from the history dropdown (and its remembered
        layout/libraries). Files on disk are never touched."""
        libdir = self.txt_dir.GetValue().strip()
        if not libdir:
            return
        c = self.config
        c.library_dir_history = [d for d in c.library_dir_history if d != libdir]
        c.dir_layouts.pop(libdir, None)
        c.dir_libs.pop(libdir, None)
        try:
            c.save()
        except Exception:  # noqa: BLE001
            pass
        if self._scope == "project" and self._project is not None:
            nxt = str(Path(self._project.project_dir) / PROJECT_LIB_SUBDIR)
        else:
            nxt = next((d for d in c.library_dir_history if d), str(default_library_dir()))
        self._reload_dir_history(nxt)
        self.append_log(f"Removed from the list (files kept): {libdir}")
        self._on_dir_change()

    def _on_layout_info(self, _evt=None) -> None:
        LayoutInfoDialog(self).ShowAndDestroy()

    def _current_advanced(self) -> bool:
        """True if the 'Organized' layout is selected (False = Default/flat).

        Only meaningful when ``self._layout is not None``; callers that must not
        act before a choice is made check ``_layout`` first.
        """
        return bool(self._layout)

    def _apply_dir_layout(self, libdir: str) -> None:
        """Point the Flat/Organized radios at the layout for ``libdir``: the value
        remembered for that dir, else auto-detected from disk, else the global
        default."""
        libdir = (libdir or "").strip()
        remembered = self.config.dir_layouts.get(libdir) if libdir else None
        if remembered is None:
            # Unknown dir: leave BOTH radios unselected so the user must choose.
            self._layout = None
            self.rb_flat.SetValue(False)
            self.rb_org.SetValue(False)
            return
        adv = bool(remembered)
        self._layout = adv
        self.rb_org.SetValue(adv)
        self.rb_flat.SetValue(not adv)

    def _on_layout_change(self, _evt=None) -> None:
        """Layout radio toggled: remember it for this dir and rescan. Nothing is
        written to disk — folders and files are created on the first import."""
        self._layout = bool(self.rb_org.GetValue())
        adv = self._layout
        libdir = self.txt_dir.GetValue().strip()
        self.config.advanced_mode = adv  # global default for new dirs
        if libdir:
            self.config.dir_layouts[libdir] = adv
        self._repoint_libs()

    # ------------------------------------------------ library name suggestions
    def _scope_project_dir(self) -> str | None:
        if self._scope == "project" and self._project is not None:
            return self._project.project_dir
        return None

    def _kicad_conflict(self, name: str, libdir: str, kinds=("sym", "fp")) -> str | None:
        """Path of a DIFFERENT library KiCad already knows as ``name`` — in the
        global table or the open project's table, whichever card is selected and
        whether or not registration is on (rename/delete edit both tables)."""
        if not libdir:
            return None
        proj = self._project.project_dir if self._project is not None else None
        p = library_paths(libdir, name, name, name, advanced=self._current_advanced())
        targets = {"sym": p["symbol_lib"], "fp": p["footprint_dir"]}
        for kind in kinds:
            other = nickname_conflict(name, targets[kind], conflict_tables(kind, proj))
            if other:
                return other
        return None

    def _suggest_lib_name(self, libdir: str) -> str:
        """Default library name for a folder with no libraries yet: the folder's
        own name (the project's name for a generic 'Libraries' folder inside a
        project), made unique against the libraries KiCad already has."""
        libdir = (libdir or "").strip()
        base = Path(libdir).name if libdir else "AddedParts"
        proj = self._scope_project_dir()
        if proj and base.lower() in ("libraries", "library", "libs", "lib") and _is_inside(libdir, proj):
            base = Path(proj).name
        base = safe_lib_name(base)
        for i in range(1, 100):
            name = base if i == 1 else f"{base}_{i}"
            if not self._kicad_conflict(name, libdir):
                return name
        return base

    def _clean_name(self, value: str, default: str) -> str:
        # Return the name as shown (no silent changes): a name that breaks the
        # rules is reported by the live notice and refused at import.
        value = (value or "").strip()
        if not value or value in _ACTIONS or value == _PICK_LAYOUT:
            return default
        return value

    def _current_names(self) -> tuple[str, str, str]:
        c = self.config
        return (
            self._clean_name(self.cmb_sym.GetStringSelection(), c.symbol_lib_name or "AddedParts"),
            self._clean_name(self.cmb_fp.GetStringSelection(), c.footprint_lib_name or "AddedParts"),
            self._clean_name(self.cmb_3d.GetStringSelection(), c.model_lib_name or "AddedParts"),
        )

    def _harvest_widgets(self) -> ParthiveConfig:
        """Copy the widgets into self.config (what settings.json keeps). The
        shown library dir belongs to the selected card: the global dir goes to
        settings.json, a project dir to that project's own settings file."""
        c = self.config
        active = self.txt_dir.GetValue().strip()
        self._store_active_dir(active)
        c.library_dir = self._global_dir
        self._remember_dir(active)
        if self._layout is not None:
            c.advanced_mode = self._current_advanced()
            if active:
                c.dir_layouts[active] = c.advanced_mode
                self._remember_libs()
        c.symbol_lib_name, c.footprint_lib_name, c.model_lib_name = self._current_names()
        c.skip_3d = self.chk_skip3d.GetValue()
        c.prefer_step = self.chk_step.GetValue()
        c.compress_models = self.chk_compress.GetValue()
        c.confirm_import = self.chk_confirm.GetValue()
        c.overwrite = self.chk_overwrite.GetValue()
        c.auto_register = self.chk_register.GetValue()
        c.pin_project_libs = self.chk_pin.GetValue()
        c.pin_types = PIN_TYPE_MODES[max(0, self.cmb_pins.GetSelection())]
        c.close_after_import = self.chk_close.GetValue()
        c.symbol_text_size = round(float(self.spin_sym.GetValue()), 3)
        c.footprint_text_size = round(float(self.spin_fp.GetValue()), 3)
        c.last_component = self.txt_component.GetValue().strip()
        return c

    # -------------------------------------------------- target-library combos
    def _on_show_paths(self, _evt) -> None:
        if self._layout is None:
            wx.MessageBox(
                "Select a layout first — Default (one folder) or Organized (subfolders).",
                "PartHive", wx.OK | wx.ICON_INFORMATION,
            )
            return
        libdir = self.txt_dir.GetValue().strip() or str(default_library_dir())
        advanced = self._current_advanced()
        sym, fp, td = self._current_names()
        p = library_paths(libdir, sym, fp, td, advanced=advanced)
        wx.MessageBox(
            f"Symbol:    {p['symbol_lib']}\n"
            f"Footprint: {p['footprint_dir']}\n"
            f"3D model:  {p['model_dir']}",
            "PartHive — target paths",
            wx.OK | wx.ICON_INFORMATION,
        )

    def _on_check_library(self, _evt=None) -> None:
        """Check the target footprint library and offer to repair broken files.

        PartHive never rewrites other people's footprints on its own — an import
        only ever touches the footprint being imported — so this is the explicit,
        opt-in way to fix the rest.
        """
        if self._layout is None:
            wx.MessageBox(
                "Select a layout first — Default (one folder) or Organized (subfolders).",
                "PartHive", wx.OK | wx.ICON_INFORMATION,
            )
            return
        libdir = self.txt_dir.GetValue().strip() or str(default_library_dir())
        sym, fp, td = self._current_names()
        fp_dir = library_paths(libdir, sym, fp, td, advanced=self._current_advanced())[
            "footprint_dir"
        ]
        if not Path(fp_dir).is_dir():
            wx.MessageBox(
                "This footprint library does not exist yet:\n%s" % fp_dir,
                "PartHive — check library", wx.OK | wx.ICON_INFORMATION,
            )
            return

        checked, broken = scan_footprint_dir(fp_dir)
        self.append_log(f"Checked {checked} footprint(s) in {Path(fp_dir).name}.")
        if not broken:
            wx.MessageBox(
                "All %d footprint(s) are fine — KiCad can load this library." % checked,
                "PartHive — check library", wx.OK | wx.ICON_INFORMATION,
            )
            return

        listing = "\n".join("  - %s: %s" % (n, p) for n, p in broken[:12])
        if len(broken) > 12:
            listing += "\n  ... and %d more" % (len(broken) - 12)
        for name, problem in broken:
            self.append_log(f"  BROKEN: {name}: {problem}")
        answer = wx.MessageBox(
            "%d of %d footprint(s) cannot be loaded by KiCad, which hides the whole "
            "library:\n\n%s\n\nRepair them now? Only quoting/naming is corrected — "
            "geometry is never changed." % (len(broken), checked, listing),
            "PartHive — repair library?",
            wx.YES_NO | wx.ICON_QUESTION,
        )
        if answer != wx.YES:
            self.append_log("Repair cancelled — no files were changed.")
            return
        checked2, repaired = repair_footprint_dir(fp_dir, self.append_log)
        wx.MessageBox(
            "Repaired %d of %d footprint(s)." % (repaired, checked2),
            "PartHive — check library", wx.OK | wx.ICON_INFORMATION,
        )

    def _refresh_library_choices(self) -> None:
        libdir = self.txt_dir.GetValue().strip()
        # Until a layout is chosen the target libraries are unknown — show the
        # prompt in all three dropdowns and disable them (avoids bogus names).
        if self._layout is None:
            for cmb in self._combo_kind:
                cmb.Set([_PICK_LAYOUT])
                cmb.SetSelection(0)
                cmb.Enable(False)
                self._prev_lib[cmb] = ""
            self._update_lib_notice()
            return
        for cmb in self._combo_kind:
            cmb.Enable(True)
        advanced = self._current_advanced()
        try:
            found = (
                scan_libraries(libdir, advanced)
                if libdir
                else {"symbols": [], "footprints": [], "models": []}
            )
        except Exception:  # noqa: BLE001
            found = {"symbols": [], "footprints": [], "models": []}

        fallback = None
        for cmb, kind in self._combo_kind.items():
            names = list(found[kind])
            desired = (self._prev_lib.get(cmb) or "").strip()
            # Empty dir with the create sentinel pending -> show only "create new".
            if not names and desired == _CREATE_NEW:
                cmb.Set([_CREATE_NEW])
                cmb.SetSelection(0)
                self._prev_lib[cmb] = _CREATE_NEW
                continue
            if not desired or desired == _CREATE_NEW:
                fallback = fallback or self._suggest_lib_name(libdir)
                desired = fallback
            # A read-only combo can only display a value present in its list, so
            # ensure the pending target name is selectable even if it does not
            # exist on disk yet.
            if desired not in names:
                names = [desired] + names
            actions = [_CREATE_NEW]
            if found[kind]:
                actions += [_RENAME, _DELETE]  # manage a library that exists on disk
            cmb.Set(actions + names)
            if not cmb.SetStringSelection(desired):
                cmb.SetSelection(0)  # never leave a read-only combo blank
            self._prev_lib[cmb] = desired
        self._update_lib_notice()

    def _on_dir_change(self, _evt=None) -> None:
        """A different library dir was picked/typed — restore its remembered layout,
        rescan, and re-point each target-library combo at that dir (keep the
        previously selected library if present, else the first found, else the
        "+ Create new library..." sentinel for an empty dir)."""
        self._store_active_dir(self.txt_dir.GetValue().strip(), persist=True)
        self._apply_dir_layout(self.txt_dir.GetValue().strip())
        self._repoint_libs()
        self._update_scope_ui()
        # Full path on hover, even when the box is too narrow to show it.
        self.txt_dir.SetToolTip(self.txt_dir.GetValue().strip())

    def _repoint_libs(self) -> None:
        """Re-scan the current dir in the selected layout and re-point each
        target-library combo: keep the previous library if it exists there, else
        the first found, else the "+ Create new library..." sentinel."""
        if self._layout is None:
            self._refresh_library_choices()  # shows the "select a layout" prompt
            return
        libdir = self.txt_dir.GetValue().strip()
        advanced = self._current_advanced()
        try:
            found = (
                scan_libraries(libdir, advanced)
                if libdir
                else {"symbols": [], "footprints": [], "models": []}
            )
        except Exception:  # noqa: BLE001
            found = {"symbols": [], "footprints": [], "models": []}
        remembered = dict(self.config.dir_libs.get(libdir) or {})
        base = safe_lib_name(Path(libdir).name) if libdir else ""
        suggested = self._suggest_lib_name(libdir)  # folder name, unique in KiCad

        def usable(kind: str, name: str) -> bool:
            """A name KiCad doesn't already use for a different library."""
            tk = {"symbols": ("sym",), "footprints": ("fp",)}.get(kind)
            return bool(name) and (not tk or not self._kicad_conflict(name, libdir, tk))

        chosen: dict = {}
        for kind in ("symbols", "footprints"):
            names = list(found[kind])
            was = (remembered.get(kind) or "").strip()
            # Preference: the library last used for this dir, the library named
            # after the folder, any library in the dir, a name picked for this
            # (still empty) dir before — skipping every name that clashes with
            # a different library in KiCad. Nothing usable -> the folder name.
            candidates = ([was] if was in names else []) + ([base] if base in names else [])
            candidates += names + ([was] if was else [])
            chosen[kind] = next((n for n in candidates if usable(kind, n)), suggested)

        sym = chosen["symbols"]
        models = list(found["models"])
        was = (remembered.get("models") or "").strip()
        if was and was in models and (remembered.get("symbols") or "").strip() == sym:
            chosen["models"] = was  # remembered set still in use for this dir
        elif sym in models or sym not in found["symbols"]:
            chosen["models"] = sym  # same name as the symbol library (new or existing)
        else:
            chosen["models"] = models[0] if models else sym

        for cmb, kind in self._combo_kind.items():
            self._prev_lib[cmb] = chosen[kind]
        self._refresh_library_choices()

    def _ask_new_library(self, kind: str, name: str = "", also=None):
        """Show the "Create library" dialog. Returns (name, [kinds]) or None."""
        dlg = NewLibraryDialog(self, kind, name, also)
        try:
            if dlg.ShowModal() != wx.ID_OK:
                return None
            name, kinds = dlg.get_result()
        finally:
            dlg.Destroy()
        name = (name or "").strip()
        return (name, kinds) if name else None

    def _name_conflict(self, kind: str, name: str) -> str | None:
        """Path of a different KiCad library already called ``name`` (symbol and
        footprint libraries only — 3D folders have no KiCad name)."""
        libdir = self.txt_dir.GetValue().strip() or str(default_library_dir())
        table_kind = {"symbols": ("sym",), "footprints": ("fp",), "models": ()}[kind]
        return self._kicad_conflict(name, libdir, table_kind) if table_kind else None

    def _create_library(self, kind: str, name: str) -> bool:
        """Accept a new library name — nothing is written until the first import.
        Refuses a name KiCad already uses for a different library."""
        libdir = self.txt_dir.GetValue().strip() or str(default_library_dir())
        other = self._name_conflict(kind, name)
        if other:
            wx.MessageBox(
                f"'{name}' is already used in KiCad for another library:\n\n{other}\n\n"
                "Library names must be unique — choose a different name.",
                "PartHive", wx.OK | wx.ICON_WARNING,
            )
            return False
        p = library_paths(libdir, name, name, name, advanced=self._current_advanced())
        path = {"symbols": p["symbol_lib"], "footprints": p["footprint_dir"], "models": p["model_dir"]}[kind]
        label = {"symbols": "symbol", "footprints": "footprint", "models": "3D"}[kind]
        if path.exists():
            self.append_log(f"Selected existing {label} library '{name}'.")
        else:
            self.append_log(f"New {label} library '{name}' — created on the first import.")
        return True

    # ------------------------------------------------- rename / delete library
    def _tables(self, table_kind: str) -> list:
        """[(table file, project dir for ${KIPRJMOD}, scope)] — the global table
        and, when a project is known, the project's own table."""
        fname = "sym-lib-table" if table_kind == "sym" else "fp-lib-table"
        out = []
        settings = find_settings_dir()
        if settings is not None:
            out.append((settings / fname, None, "global"))
        if self._project is not None:
            pdir = Path(self._project.project_dir)
            out.append((pdir / fname, pdir, "project"))
        return out

    def _globally_pinned(self, table_kind: str, name: str) -> bool:
        """Is ``name`` pinned in KiCad's own (global) settings? PartHive can't
        change that list while KiCad runs, so it only tells the user."""
        settings = find_settings_dir()
        if settings is None:
            return False
        key = "pinned_symbol_libs" if table_kind == "sym" else "pinned_fp_libs"
        try:
            data = json.loads((settings / "kicad_common.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                if name in (node.get(key) or []):
                    return True
                stack.extend(node.values())
            elif isinstance(node, list):
                stack.extend(node)
        return False

    def _after_manage(self, combo_names: dict, scopes: set, summary: list) -> None:
        """Re-point the dropdowns, refresh, log and tell the user what's next."""
        combo_of = {k: c for c, k in self._combo_kind.items()}
        for kind, name in combo_names.items():
            self._prev_lib[combo_of[kind]] = name
        self._refresh_library_choices()
        self._remember_libs()
        for line in summary:
            self.append_log(line)
        if scopes:
            todo = []
            if "global" in scopes:
                todo.append("Restart KiCad so the Symbol and Footprint editors pick up the change.")
            if "project" in scopes:
                todo.append("Close and reopen the project so it picks up the change.")
            summary = summary + [""] + todo
        wx.MessageBox("\n".join(summary), "PartHive", wx.OK | wx.ICON_INFORMATION)

    def _existing(self, kind: str) -> list:
        """Libraries of ``kind`` that exist on disk in the current dir/layout."""
        libdir = self.txt_dir.GetValue().strip()
        try:
            return scan_libraries(libdir, self._current_advanced())[kind] if libdir else []
        except Exception:  # noqa: BLE001
            return []

    def _rename_library(self, kind: str, name: str) -> None:
        libdir = self.txt_dir.GetValue().strip()
        adv = self._current_advanced()
        new, also = "", None
        while True:
            dlg = LibraryActionDialog(self, "rename", kind, self._existing(kind), name,
                                      lambda n: existing_kinds(libdir, adv, n), new, also)
            try:
                if dlg.ShowModal() != wx.ID_OK:
                    return
                name, new, kinds = dlg.get_result()
            finally:
                dlg.Destroy()
            if not name:
                return
            also = [k for k in kinds if k != kind]
            new = self._clean_name(new, "")
            if not new or new == name:
                return
            problem = lib_name_problem(new)
            case_only = new.casefold() == name.casefold()  # e.g. alpha -> Alpha
            for k in kinds if problem is None else []:
                if not case_only and lib_path(libdir, adv, k, new).exists():
                    problem = f"A {_KIND_LABEL[k]} library called '{new}' already exists in this folder."
                    break
                other = self._name_conflict(k, new)
                if other:
                    problem = f"'{new}' is already used in KiCad for another library:\n\n{other}"
                    break
            if problem is None:
                break
            wx.MessageBox(problem + "\n\nChoose a different name.", "PartHive",
                          wx.OK | wx.ICON_WARNING)

        summary, scopes, renamed = [], set(), {}
        for k in kinds:
            old_p, new_p = lib_path(libdir, adv, k, name), lib_path(libdir, adv, k, new)
            try:
                old_p.rename(new_p)
            except OSError as exc:
                summary.append(f"Could not rename the {_KIND_LABEL[k]} library: {exc}")
                continue
            renamed[k] = new
            summary.append(f"Renamed the {_KIND_LABEL[k]} library '{name}' to '{new}'.")
            tk = TABLE_KIND.get(k)
            if tk is None:
                continue
            for table, pdir, scope in self._tables(tk):
                try:
                    changed = table_rename(table, old_p, new, old_p.name, new_p.name, pdir)
                except OSError as exc:
                    summary.append(f"  Could not update {table}: {exc} — fix this library's "
                                   "entry in Preferences > Manage Libraries.")
                    continue
                if changed:
                    scopes.add(scope)
                    self._session_registered.add((tk, new, scope))
                    where = "KiCad's" if scope == "global" else "the project's"
                    summary.append(f"  Updated {where} library list.")
                    if scope == "project":
                        moved = move_pin(self._project.project_dir, tk, name, new)
                        if moved == "scheduled":
                            summary.append("  The pin moves to the new name when you close the project.")
                        elif moved == "pinned":
                            summary.append("  The pin moved to the new name.")
            if self._globally_pinned(tk, name):
                summary.append(f"  '{name}' was pinned in KiCad's global list — pin it again "
                               f"under its new name (KiCad doesn't allow changing that while it runs).")
        if "footprints" in renamed:
            try:
                n = relink_footprints(Path(libdir), adv, name, new)
                if n:
                    summary.append(f"  Updated footprint links in {n} symbol librar{'y' if n == 1 else 'ies'}.")
            except OSError as exc:
                summary.append(f"  Could not update all footprint links ({exc}) — some symbols "
                               f"may still point to '{name}:'.")
        if "models" in renamed:
            try:
                n = relink_models(Path(libdir), adv, name, new)
                if n:
                    summary.append(f"  Updated 3D-model paths in {n} footprint{'' if n == 1 else 's'}.")
            except OSError as exc:
                summary.append(f"  Could not update all 3D-model paths ({exc}) — some footprints "
                               f"may still point to '{name}.3dshapes'.")
        # Lists that were showing the renamed library follow it to its new name.
        showing = {k: self._prev_lib.get(c) for c, k in self._combo_kind.items()}
        follow = {k: new for k in renamed if showing.get(k) == name}
        self._rename_in_memory(libdir, name, renamed)
        self._after_manage(follow, scopes, summary)

    def _delete_library(self, kind: str, name: str) -> None:
        libdir = self.txt_dir.GetValue().strip()
        adv = self._current_advanced()
        dlg = LibraryActionDialog(self, "delete", kind, self._existing(kind), name,
                                  lambda n: existing_kinds(libdir, adv, n))
        try:
            if dlg.ShowModal() != wx.ID_OK:
                return
            name, _unused, kinds = dlg.get_result()
        finally:
            dlg.Destroy()
        if not name:
            return
        summary, scopes, gone = [], set(), []
        for k in kinds:
            p = lib_path(libdir, adv, k, name)
            tk = TABLE_KIND.get(k)
            if tk is not None:
                for table, pdir, scope in self._tables(tk):
                    try:
                        removed = table_remove(table, p, pdir)
                    except OSError as exc:
                        summary.append(f"  Could not update {table}: {exc} — remove '{name}' "
                                       "in Preferences > Manage Libraries.")
                        continue
                    if removed:
                        scopes.add(scope)
                        self._session_registered.discard((tk, name, scope))
                        where = "KiCad's" if scope == "global" else "the project's"
                        summary.append(f"  Removed '{name}' from {where} {_KIND_LABEL[k]} library list.")
                        if scope == "project":
                            drop_pin(self._project.project_dir, tk, name)
            if not p.exists():
                continue
            try:
                where = to_recycle_bin(p, config_dir() / "deleted")
            except (OSError, shutil.Error) as exc:
                summary.insert(0, f"Could not delete the {_KIND_LABEL[k]} library: {exc}")
                continue
            gone.append(k)
            summary.insert(0, f"Deleted the {_KIND_LABEL[k]} library '{name}' (moved to {where}).")
        remembered = self.config.dir_libs.get(libdir) or {}
        for k in gone:
            if remembered.get(k) == name:
                remembered.pop(k, None)
        # let each dropdown pick another library (or a fresh name) for this dir
        self._repoint_libs()
        self._after_manage({}, scopes, summary or [f"Nothing was deleted for '{name}'."])

    def _rename_in_memory(self, libdir: str, old: str, renamed: dict) -> None:
        remembered = self.config.dir_libs.get(libdir)
        if remembered:
            for k, new in renamed.items():
                if remembered.get(k) == old:
                    remembered[k] = new

    def _on_lib_combo(self, evt) -> None:
        cmb = evt.GetEventObject()
        selection = cmb.GetStringSelection().strip()
        if selection in (_RENAME, _DELETE):
            target = (self._prev_lib.get(cmb) or "").strip()
            kind = self._combo_kind[cmb]
            self._refresh_library_choices()  # show the library again while the dialog is up
            if self._importing or not self._existing(kind):
                return
            if selection == _RENAME:
                self._rename_library(kind, target)
            else:
                self._delete_library(kind, target)
            return
        if selection != _CREATE_NEW:
            self._prev_lib[cmb] = selection
            self._remember_libs()
            self._update_lib_notice()
            return
        # "+ Create new library..." chosen — prompt (optionally for the other two
        # library types too, same name), check the name, and select it.
        kind = self._combo_kind[cmb]
        name, also = "", None
        while True:
            answer = self._ask_new_library(kind, name, also)
            if answer is None:
                self._refresh_library_choices()  # cancelled: restore previous selection
                return
            name, kinds = answer
            also = [k for k in kinds if k != kind]
            problem = lib_name_problem(name)
            if problem:
                wx.MessageBox(problem, "PartHive", wx.OK | wx.ICON_WARNING)
                continue
            clash = next(((k, o) for k in kinds if (o := self._name_conflict(k, name))), None)
            if clash is None:
                break
            wx.MessageBox(
                f"'{name}' is already used in KiCad for another library:\n\n{clash[1]}\n\n"
                "Library names must be unique — choose a different name.",
                "PartHive", wx.OK | wx.ICON_WARNING,
            )
        combo_of = {k: c for c, k in self._combo_kind.items()}
        for k in kinds:
            self._create_library(k, name)  # logs "new" / "existing" per library
            self._prev_lib[combo_of[k]] = name
        self._refresh_library_choices()
        self._remember_libs()

    # ---------------------------------------------------------------- events
    def on_browse(self, _evt) -> None:
        start = self.txt_dir.GetValue().strip() or str(default_library_dir())
        with wx.DirDialog(self, "Choose the library directory", start) as dlg:
            if dlg.ShowModal() == wx.ID_OK:
                problem = lib_dir_problem(dlg.GetPath())
                if problem:
                    wx.MessageBox(problem, "PartHive", wx.OK | wx.ICON_WARNING)
                    return
                self._select_dir(dlg.GetPath())

    def _select_dir(self, path: str) -> None:
        """Make ``path`` the library dir of the selected card (Browse...)."""
        self._remember_dir(path)
        self._reload_dir_history(path)
        self._on_dir_change()

    # ------------------------------------------------- global / project cards
    def _active_dir(self) -> str:
        """The library dir of the selected card."""
        if self._scope == "project" and self._project is not None:
            return self._project.library_dir
        return self._global_dir

    def _store_active_dir(self, path: str, persist: bool = False) -> None:
        """Remember ``path`` as the selected card's library dir. For a project
        it is saved to that project's file right away (``persist``)."""
        path = (path or "").strip()
        if not path:
            return
        if self._scope == "project" and self._project is not None:
            if path != self._project.library_dir:
                self._project.library_dir = path
                if persist:
                    self._project.save()
        else:
            self._global_dir = path

    def _switch_scope(self, scope: str) -> None:
        """Card clicked: switch, remember it for this project, show its dir."""
        self._store_active_dir(self.txt_dir.GetValue().strip())
        if self._layout is not None:
            self._remember_libs()
        self._scope = scope
        if self._project is not None and self._project.scope != scope:
            self._project.scope = scope
            self._project.save()  # only a manual change creates/updates the file
        active = self._active_dir()
        self._reload_dir_history(active)
        self._on_dir_change()

    def _on_card_global(self) -> None:
        if self._scope != "global":
            self._switch_scope("global")
        else:
            self._update_scope_ui()

    def _on_card_project(self) -> None:
        if self._project is None and not self._choose_project():
            self._update_scope_ui()  # cancelled: stay on Global
            return
        if self._scope != "project":
            self._switch_scope("project")
            self.append_log(
                f"Project library for {self._project.name} — remembered for this project."
            )
        else:
            self._update_scope_ui()

    def _choose_project(self) -> bool:
        """Ask for a .kicad_pro; load that project's remembered settings."""
        start = ""
        if self._project is not None:
            start = self._project.project_dir
        elif find_project_root(self.txt_dir.GetValue().strip()):
            start = str(find_project_root(self.txt_dir.GetValue().strip()))
        with wx.FileDialog(
            self,
            "Choose the KiCad project",
            defaultDir=start,
            wildcard="KiCad project (*.kicad_pro)|*.kicad_pro",
            style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST,
        ) as dlg:
            if dlg.ShowModal() != wx.ID_OK:
                return False
            folder = Path(dlg.GetPath()).parent
        self._store_active_dir(self.txt_dir.GetValue().strip())
        self._project = ProjectSettings.load(folder)
        self.append_log(f"Project: {self._project.project_dir}")
        wx.CallAfter(self._fill_sources)   # another project is "other" now
        wx.CallAfter(self._scan_links)
        return True

    def _on_change_project(self, _evt=None) -> None:
        if not self._choose_project():
            return
        # Use what this project remembered (Global if it was never changed).
        self._scope = self._project.scope
        active = self._active_dir()
        self._reload_dir_history(active)
        self._on_dir_change()

    def _update_scope_ui(self) -> None:
        """Refresh both cards, the hint line and the Change-project button."""
        project = self._project
        self.card_global.set_content(_short_path(self._global_dir))
        if project is not None:
            self.card_project.set_content(_short_path(project.library_dir, project.project_dir))
        else:
            self.card_project.set_content("No project detected — click to choose one")
        self.card_global.set_selected(self._scope == "global")
        self.card_project.set_selected(self._scope == "project")
        self.btn_change_project.Show(project is not None)
        try:
            self._panel.Layout()
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------- live library notice
    @staticmethod
    def _names_label(names: list) -> str:
        names = list(dict.fromkeys(names))  # unique, keep order
        return " and ".join(f"'{n}'" for n in names)

    def _restart_message(self, added: list, pin_status: str = "") -> str:
        project = any(scope == "project" for _k, _n, scope in added)
        label = self._names_label([n for _k, n, _s in added])
        if project:
            pin = {
                "scheduled": "\n\nIt will be pinned at the top of Add Symbol / Add "
                             "Footprint when you close the project.",
                "pinned": "\n\nIt is pinned at the top of Add Symbol / Add Footprint.",
            }.get(pin_status, "")
            return (f"Added {label} to the project's library list.\n\n"
                    "Close and reopen the project in KiCad to use it in the Symbol "
                    f"and Footprint editors.{pin}")
        return (f"Added {label} to KiCad's library list.\n\n"
                "Restart KiCad to use it in the Symbol and Footprint editors.")

    def _lib_notice(self) -> tuple:
        """(message, art id) for the selected libraries, or ("", None)."""
        if self._layout is None:
            return "", None
        libdir = self.txt_dir.GetValue().strip()
        if not libdir:
            return "", None
        problem = lib_dir_problem(libdir)
        if problem:
            return f"Library folder: {problem}", wx.ART_ERROR
        sym, fp, td = self._current_names()
        for label, nick in (("Symbol", sym), ("Footprint", fp), ("3D", td)):
            problem = lib_name_problem(nick)
            if problem:
                return (f"{label} library '{nick}': {problem} Pick or create another "
                        "library, or rename this one."), wx.ART_ERROR
        proj = self._scope_project_dir()
        scope = "project" if proj else "global"
        p = library_paths(libdir, sym, fp, sym, advanced=self._current_advanced())
        names = {"sym": sym, "fp": fp}
        states = {
            "sym": registration_state(sym, p["symbol_lib"], "sym", scope, proj),
            "fp": registration_state(fp, p["footprint_dir"], "fp", scope, proj),
        }
        # Same check the import uses: on the Global card a name can also clash
        # with a library of the open project (which would hide it there).
        for k, nick in names.items():
            if states[k] != "conflict" and self._kicad_conflict(nick, libdir, (k,)):
                states[k] = "conflict"
        listed = "the project's library list" if proj else "KiCad's library list"
        restart = "close and reopen the project" if proj else "restart KiCad"

        clash = [names[k] for k, st in states.items() if st == "conflict"]
        if clash:
            return (f"{self._names_label(clash)} is already used in KiCad for another "
                    "library. Pick a different name with \"+ Create new library...\"."), wx.ART_ERROR

        missing = [names[k] for k, st in states.items() if st == "missing"]
        if missing:
            label = self._names_label(missing)
            if not self.chk_register.GetValue():
                return (f"{label} is not in {listed}, and automatic registration is "
                        "off. Add it in Preferences > Manage Symbol / Footprint "
                        "Libraries."), wx.ART_WARNING
            on_disk = p["symbol_lib"].exists() or p["footprint_dir"].exists()
            lead = f"{label} will be added to {listed}" if on_disk else (
                f"New library {label}: it will be created and added to {listed}")
            return (f"{lead} on the first import. After that, {restart} to use it "
                    "in the Symbol and Footprint editors."), wx.ART_INFORMATION

        # In the table on disk — is the running KiCad already using it?
        loaded = loaded_footprint_libs()  # known only inside the PCB editor
        pending = loaded is not None and fp not in loaded
        pending = pending or any((k, names[k], scope) in self._session_registered for k in names)
        if pending:
            return (f"{self._names_label([sym, fp])} has been added to {listed}. "
                    f"{restart[0].upper() + restart[1:]} to use it in the Symbol and "
                    "Footprint editors."), wx.ART_WARNING
        return "", None

    def _update_lib_notice(self) -> None:
        if not hasattr(self, "notice_text"):
            return
        try:
            msg, art = self._lib_notice()
        except Exception:  # noqa: BLE001 - a notice must never break the dialog
            msg, art = "", None
        if msg:
            self.notice_icon.SetBitmap(
                wx.ArtProvider.GetBitmap(art, wx.ART_MESSAGE_BOX, wx.Size(16, 16))
            )
            self.notice_text.SetLabel(msg)
            width = self.notice_text.GetParent().GetClientSize().width - 60
            # wx skips Wrap() when the width equals the previous call's, so a new
            # (longer) notice would stay on one line with its end cut off: reset
            # the wrapping first, then wrap, then re-measure.
            self.notice_text.Wrap(-1)
            self.notice_text.Wrap(width if width > 200 else 560)
            self.notice_text.SetMinSize(wx.DefaultSize)  # forget the last notice's height
            self.notice_text.InvalidateBestSize()
            self.notice_text.SetMinSize(self.notice_text.GetBestSize())
        self.notice_row.ShowItems(bool(msg))
        try:
            self._panel.Layout()
        except Exception:  # noqa: BLE001
            pass

    def _save_project_if_known(self) -> None:
        """Save the project's file only if it is on the Project card or already
        has a file — a project the user never changed stays on the default
        (Global) with no file written."""
        project = self._project
        if project is None:
            return
        if self._scope == "project" or project_settings_path(project.project_dir).is_file():
            project.save()

    def _run_config(self) -> ParthiveConfig:
        """A copy of the settings for one import: the selected card's library
        dir and scope (self.config itself keeps the global dir)."""
        project = self._project
        return dataclasses.replace(
            self.config,
            library_dir=self._active_dir(),
            register_scope="project" if (self._scope == "project" and project) else "global",
            project_dir=project.project_dir if project else "",
        )

    # ------------------------------------------------ copy parts / fix links
    def _fill_sources(self) -> None:
        """'From another project' choices: recent / remembered projects (not
        this one), then the global libraries."""
        current = self._project.project_dir if self._project is not None else None
        self._source_projects = copylib.known_projects(exclude=current)
        self.cmb_source.Set([p.name for p in self._source_projects] + ["Global libraries"])
        self.cmb_source.SetSelection(0)

    def _scan_links(self) -> None:
        """Look for symbols that link to libraries the project can't see."""
        self._missing = {}
        if self._project is not None:
            try:
                self._missing = fixlinks.scan(Path(self._project.project_dir), self._source_projects)
            except Exception:  # noqa: BLE001 - a scan must never break the dialog
                self._missing = {}
        n = sum(len(ml.refs) for ml in self._missing.values())
        if n:
            libs = ", ".join(f"{k} ({len(v.refs)})" for k, v in self._missing.items())
            self.fix_text.SetLabel(f"{n} symbol{'' if n == 1 else 's'} link to libraries this "
                                   f"project doesn't have: {libs}")
        self.fix_row.ShowItems(bool(n))
        self._relayout_fix_text()
        wx.CallAfter(self._relayout_fix_text)  # again once the window has its real width

    def _relayout_fix_text(self) -> None:
        """Wrap the notice to the space left of the Fix links button."""
        try:
            if self.fix_text.IsShown():
                space = (self.fix_text.GetParent().GetClientSize().width
                         - self.btn_fix.GetSize().width - 60)
                self.fix_text.Wrap(-1)  # wx skips Wrap() with an unchanged width
                self.fix_text.Wrap(max(200, space))
                self.fix_text.SetMinSize(wx.DefaultSize)
                self.fix_text.InvalidateBestSize()
                self.fix_text.SetMinSize(self.fix_text.GetBestSize())
            self._panel.Layout()
        except Exception:  # noqa: BLE001
            pass

    def _target_importer(self):
        """A ComponentImporter for the Target libraries, or None (reason logged)."""
        if self._layout is None:
            self.append_log("Select a layout first — Default (one folder) or Organized (subfolders).")
            return None
        self._harvest_widgets().save()
        self._save_project_if_known()
        imp = ComponentImporter(self._run_config(), self.append_log)
        problem = lib_dir_problem(imp.library_dir)
        for label, nick in (("Symbol", imp.sym_name), ("Footprint", imp.fp_name), ("3D", imp.model_name)):
            problem = problem or (lambda pr: pr and f"{label} library '{nick}': {pr}")(lib_name_problem(nick))
        problem = problem or "; ".join(imp.name_conflicts())
        if problem:
            self.append_log(problem)
            wx.MessageBox(problem, "PartHive", wx.OK | wx.ICON_WARNING)
            return None
        return imp

    def _after_write(self, imp) -> None:
        """New libraries: remember for the restart notice, pin project ones."""
        added = list(imp.registered)
        self._session_registered.update(added)
        project_added = [(k, n) for k, n, scope in added if scope == "project"]
        if project_added and self.chk_pin.GetValue() and self._project is not None:
            names = {k: [n for kk, n in project_added if kk == k] for k in ("sym", "fp")}
            self._report_pins(request_pins(self._project.project_dir, names))
        self._refresh_library_choices()

    def _on_copy_parts(self, _evt=None) -> None:
        if self._importing:
            return
        imp = self._target_importer()
        if imp is None:
            return
        target_names = {pt.name for pt in copylib.read_parts(imp.symbol_lib)}
        text = (f"symbol library '{imp.sym_name}', footprint library '{imp.fp_name}', "
                f"3D library '{imp.model_name}' (Target libraries)")
        dlg = CopyPartsDialog(self, self._source_projects, self.cmb_source.GetSelection(), text, target_names)
        try:
            if dlg.ShowModal() != wx.ID_OK:
                return
            src, parts = dlg.source(), dlg.chosen()
        finally:
            dlg.Destroy()
        if not src or not parts:
            return
        try:
            report = copylib.copy_parts(parts, src, imp)
        except OSError as exc:
            wx.MessageBox(f"Copy failed: {exc}", "PartHive", wx.OK | wx.ICON_ERROR)
            return
        self.append_log(f"Copied from {src.label}: {len(report.symbols)} symbol(s), "
                        f"{len(report.footprints)} footprint(s), {len(report.models)} 3D model(s).")
        for line in report.skipped + report.problems:
            self.append_log(f"  {line}")
        if self.chk_register.GetValue():
            imp._register_libraries()
        self._after_write(imp)
        self._scan_links()

    def _on_fix_links(self, _evt=None) -> None:
        if self._importing or not self._missing or self._project is None:
            return
        pdir = Path(self._project.project_dir)
        imp = self._target_importer()
        if imp is None:
            return
        dlg = FixLinksDialog(self, self._missing, imp.sym_name)
        try:
            if dlg.ShowModal() != wx.ID_OK:
                return
            dlg.apply_choices()
        finally:
            dlg.Destroy()
        busy = fixlinks.open_sheets(pdir)
        if busy:
            wx.MessageBox("Save and close the Schematic Editor first, then press Fix links again.\n\n"
                          "PartHive edits the schematic file, and KiCad would overwrite those "
                          "changes when it saves the open schematic.", "PartHive",
                          wx.OK | wx.ICON_INFORMATION)
            return
        try:
            lines, changed, board = fixlinks.apply_fixes(pdir, self._missing, imp)
        except OSError as exc:
            wx.MessageBox(f"Fixing links failed: {exc}", "PartHive", wx.OK | wx.ICON_ERROR)
            return
        for line in lines:
            self.append_log(line)
        summary = f"Re-linked {changed} symbol(s) in the schematic (backup kept)."
        if board:
            summary += f" Re-linked {board} footprint(s) on the board — save the board."
        self.append_log(summary)
        self._after_write(imp)
        self._scan_links()
        wx.MessageBox("\n".join(lines + ["", summary]), "PartHive", wx.OK | wx.ICON_INFORMATION)

    def on_open_dir(self, _evt) -> None:
        path = Path(self.txt_dir.GetValue().strip()).expanduser()
        if not path.is_dir():
            # Not created yet (that happens on the first import): open the
            # nearest folder that exists instead of creating it now.
            self.append_log(f"{path} doesn't exist yet — it's created on the first import.")
            path = next((p for p in path.parents if p.is_dir()), None)
            if path is None:
                return
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
        # Nothing that changes WHERE the import goes may change while it runs.
        for w in (self.card_global, self.card_project, self.btn_change_project,
                  self.txt_dir, self.btn_browse, self.btn_forget, self.rb_flat,
                  self.rb_org, self.cmb_sym, self.cmb_fp, self.cmb_3d, self.chk_register,
                  self.chk_pin, self.btn_copy, self.cmb_source, self.btn_fix):
            w.Enable(not busy)

    def on_import(self, _evt) -> None:
        if self._importing:
            return
        if self._layout is None:
            self.append_log(
                "Select a layout first — Default (one folder) or Organized (subfolders)."
            )
            return
        self._harvest_widgets().save()
        self._save_project_if_known()
        config = self._run_config()
        self._import_project = self._project  # pins go to THIS project, see _finish
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
        if not config.confirm_import:
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
        try:
            dlg = ResolutionDialog(self, plan)
            try:
                proceed = dlg.ShowModal() == wx.ID_OK
                resolution = dlg.get_resolution() if proceed else None
            finally:
                dlg.Destroy()
        except Exception as exc:  # noqa: BLE001 - never leave the dialog stuck "busy"
            self.append_log(f"Unexpected error: {exc}")
            self._set_busy(False)
            return
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
        added = list(getattr(result, "registered", []) or [])
        self._session_registered.update(added)
        pin_status = ""
        project_added = [(k, n) for k, n, scope in added if scope == "project"]
        # The project the import STARTED in (captured in on_import), even if the
        # user switched project meanwhile; else the open one.
        project = getattr(self, "_import_project", None) or self._project
        if project_added and self.chk_pin.GetValue() and project is not None:
            names = {k: [n for kk, n in project_added if kk == k] for k in ("sym", "fp")}
            pin_status = request_pins(project.project_dir, names)
            self._report_pins(pin_status)
        # a new library may have been created during import — reflect it
        self._refresh_library_choices()
        if result.ok and self.chk_close.GetValue():
            if added:
                wx.MessageBox(self._restart_message(added, pin_status), "PartHive",
                              wx.OK | wx.ICON_INFORMATION)
            self.Close()

    def _report_pins(self, status: str) -> None:
        if status == "pinned":
            self.append_log("Pinned the new project library at the top of Add Symbol / "
                            "Add Footprint (applies when the project is opened).")
        elif status == "scheduled":
            self.append_log("The new project library will be pinned at the top of Add "
                            "Symbol / Add Footprint as soon as you close the project.")
        elif status == "failed":
            self.append_log("Could not pin the project library — pin it by hand: "
                            "right-click it in Add Symbol / Add Footprint > Pin Library.")


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
            self._save_project_if_known()
        except Exception:  # noqa: BLE001
            pass
        self.Destroy()


def show_dialog(parent: wx.Window | None = None) -> None:
    """Show the dialog. Assumes a wx.App already exists (KiCad provides one)."""
    dlg = ParthiveDialog(parent)
    wx.CallAfter(dlg._set_initial_focus)
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
