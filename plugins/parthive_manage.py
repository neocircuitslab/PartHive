"""PartHive – rename and delete libraries.

Everything KiCad needs is kept consistent:

* the files/folders themselves (``<name>.kicad_sym``, ``<name>.pretty``,
  ``<name>.3dshapes``) — deleted ones go to the Recycle Bin;
* KiCad's library tables (global and/or the project's) — the entry pointing at
  the library is renamed (name + path) or removed;
* references inside this library dir — symbols' ``Footprint`` fields
  (``Old:FP`` -> ``New:FP``) and footprints' 3D-model paths
  (``.../Old.3dshapes/...`` -> ``.../New.3dshapes/...``).

Pins and PartHive's own memory are handled by the caller (the dialog).

PartHive is licensed under GPL-3.0-or-later (see LICENSE).
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import time
from pathlib import Path

from parthive_config import write_text_atomic
from parthive_lib_table import _same_path, expand_uri
from parthive_paths import library_paths

logger = logging.getLogger("parthive.manage")

KINDS = ("symbols", "footprints", "models")
TABLE_KIND = {"symbols": "sym", "footprints": "fp"}  # 3D folders have no table
_SUFFIX = {"symbols": ".kicad_sym", "footprints": ".pretty", "models": ".3dshapes"}
_PATH_KEY = {"symbols": "symbol_lib", "footprints": "footprint_dir", "models": "model_dir"}


def lib_path(libdir: str | Path, advanced: bool, kind: str, name: str) -> Path:
    return library_paths(Path(libdir), name, name, name, advanced=advanced)[_PATH_KEY[kind]]


def existing_kinds(libdir: str | Path, advanced: bool, name: str) -> dict:
    """{kind: True if a library of that kind called ``name`` exists here}."""
    return {k: lib_path(libdir, advanced, k, name).exists() for k in KINDS}


# --------------------------------------------------------------------------
# Library tables
# --------------------------------------------------------------------------
_NAME_RE = re.compile(r'\(\s*name\s+(?:"(?:[^"\\]|\\.)*"|[^\s()]+)\s*\)')
_URI_RE = re.compile(r'\(\s*uri\s+(?:"((?:[^"\\]|\\.)*)"|([^\s()]+))\s*\)')


def _lib_blocks(content: str) -> list:
    """(start, end) spans of every top-level ``(lib ...)`` entry."""
    spans = []
    pos = 0
    pattern = re.compile(r"\(\s*lib\s")
    while True:
        m = pattern.search(content, pos)
        if not m:
            return spans
        depth, j, in_quote = 0, m.start(), False
        while j < len(content):
            ch = content[j]
            if in_quote:
                if ch == "\\":
                    j += 2
                    continue
                if ch == '"':
                    in_quote = False
            elif ch == '"':
                in_quote = True
            elif ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        spans.append((m.start(), j + 1))
        pos = j + 1


def _block_uri(block: str) -> str:
    m = _URI_RE.search(block)
    if not m:
        return ""
    return m.group(1) if m.group(1) is not None else (m.group(2) or "")


def _write_table(table_file: Path, content: str) -> None:
    try:
        shutil.copy2(table_file, table_file.with_name(table_file.name + ".parthive-bak"))
    except OSError:
        pass
    write_text_atomic(table_file, content)  # atomic, keeps KiCad's line endings


def table_rename(table_file: Path, old_target: Path, new_name: str,
                 old_base: str, new_base: str, project_dir: Path | None) -> list:
    """Rename every entry of ``table_file`` that points at ``old_target``.
    Returns the old nicknames that were renamed."""
    if not table_file.is_file():
        return []
    content = table_file.read_text(encoding="utf-8")
    renamed = []
    for start, end in reversed(_lib_blocks(content)):
        block = content[start:end]
        uri = _block_uri(block)
        if not uri or not _same_path(expand_uri(uri, project_dir), expand_uri(str(old_target))):
            continue
        nick = re.search(r'\(\s*name\s+(?:"((?:[^"\\]|\\.)*)"|([^\s()]+))\s*\)', block)
        renamed.append((nick.group(1) if nick.group(1) is not None else nick.group(2)) if nick else "")
        # The entry matched by PATH (case-insensitively, trailing slash or not),
        # so swap the last path element whatever its spelling.
        new_uri = re.sub(r"(^|[/\\])[^/\\]+([/\\]?)$",
                         lambda m: m.group(1) + new_base + m.group(2), uri)
        block = _NAME_RE.sub(f'(name "{new_name}")', block, count=1)
        block = _URI_RE.sub(lambda m: f'(uri "{new_uri}")', block, count=1)
        content = content[:start] + block + content[end:]
    if renamed:
        _write_table(table_file, content)
    return renamed


def table_remove(table_file: Path, target: Path, project_dir: Path | None) -> int:
    """Remove every entry of ``table_file`` that points at ``target``."""
    if not table_file.is_file():
        return 0
    content = table_file.read_text(encoding="utf-8")
    removed = 0
    for start, end in reversed(_lib_blocks(content)):
        uri = _block_uri(content[start:end])
        if not uri or not _same_path(expand_uri(uri, project_dir), expand_uri(str(target))):
            continue
        # take the whole line when the entry sits on a line of its own
        line_start = content.rfind("\n", 0, start) + 1
        if content[line_start:start].strip() == "":
            start = line_start
            nl = content.find("\n", end)
            if nl != -1 and content[end:nl].strip() == "":
                end = nl + 1
        content = content[:start] + content[end:]
        removed += 1
    if removed:
        _write_table(table_file, content)
    return removed


# --------------------------------------------------------------------------
# References inside the library dir
# --------------------------------------------------------------------------
def _rewrite(path: Path, pattern: re.Pattern, repl) -> bool:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    new = pattern.sub(repl, text)
    if new == text:
        return False
    write_text_atomic(path, new)
    return True


def relink_footprints(libdir: Path, advanced: bool, old: str, new: str) -> int:
    """Symbols in this dir whose Footprint field is ``old:...`` -> ``new:...``."""
    base = lib_path(libdir, advanced, "symbols", "x").parent
    pattern = re.compile(r'(\(property\s+"Footprint"\s+")' + re.escape(old) + r":")
    count = 0
    for f in sorted(base.glob("*.kicad_sym")) if base.is_dir() else []:
        count += _rewrite(f, pattern, lambda m: m.group(1) + new + ":")
    return count


def relink_models(libdir: Path, advanced: bool, old: str, new: str) -> int:
    """Footprints in this dir whose 3D model sits in ``old.3dshapes`` ->
    ``new.3dshapes`` (only inside ``(model ...)`` paths)."""
    base = lib_path(libdir, advanced, "footprints", "x").parent
    pattern = re.compile(r'(\(model\s+"?[^"\n]*[/\\])' + re.escape(old) + r"(\.3dshapes[/\\])")
    count = 0
    for pretty in sorted(base.glob("*.pretty")) if base.is_dir() else []:
        for f in sorted(pretty.glob("*.kicad_mod")):
            count += _rewrite(f, pattern, lambda m: m.group(1) + new + m.group(2))
    return count


# --------------------------------------------------------------------------
# Deleting
# --------------------------------------------------------------------------
def _on_local_fixed_drive(path: Path) -> bool:
    """Only local fixed drives have a real Recycle Bin. On network shares,
    mapped/USB/removable drives Windows deletes PERMANENTLY yet reports success."""
    try:
        import ctypes

        resolved = str(Path(path).resolve())
        if resolved.startswith("\\\\"):  # UNC path (\\server\share or \\?\UNC)
            return False
        root = os.path.splitdrive(resolved)[0] + "\\"
        DRIVE_FIXED = 3
        return ctypes.windll.kernel32.GetDriveTypeW(ctypes.c_wchar_p(root)) == DRIVE_FIXED
    except Exception:  # noqa: BLE001
        return False


def to_recycle_bin(path: Path, fallback_dir: Path) -> str:
    """Send ``path`` to the Recycle Bin (Windows, local fixed drives only).
    Otherwise — network/removable drive, other OS, or the bin refused it — move
    it into ``fallback_dir`` instead of destroying it. Returns where it went."""
    path = Path(path)
    if os.name == "nt" and _on_local_fixed_drive(path):
        try:
            import ctypes
            from ctypes import wintypes

            class SHFILEOPSTRUCTW(ctypes.Structure):
                _fields_ = [
                    ("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT),
                    ("pFrom", wintypes.LPCWSTR), ("pTo", wintypes.LPCWSTR),
                    ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted", wintypes.BOOL),
                    ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wintypes.LPCWSTR),
                ]

            FO_DELETE = 3
            # ALLOWUNDO | NOCONFIRMATION | SILENT | NOERRORUI | WANTNUKEWARNING
            # (the last one makes Windows ask instead of silently deleting for
            # good, e.g. when an item is too big for the bin).
            flags = 0x0040 | 0x0010 | 0x0004 | 0x0400 | 0x4000
            op = SHFILEOPSTRUCTW(None, FO_DELETE, str(path.resolve()) + "\0", None, flags,
                                 False, None, None)
            if ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op)) == 0 \
                    and not op.fAnyOperationsAborted and not path.exists():
                return "Recycle Bin"
        except Exception:  # noqa: BLE001
            logger.exception("recycle bin")
    dest = Path(fallback_dir) / time.strftime("%Y%m%d-%H%M%S")
    dest.mkdir(parents=True, exist_ok=True)
    shutil.move(str(path), str(dest / path.name))
    return str(dest)
