"""PartHive – filesystem path helpers.

Two responsibilities:

1.  Work out sensible default locations (the user's *Documents* folder, and the
    default *library dir* inside it) in a way that survives Windows folder
    redirection (e.g. OneDrive-backed "Documents").

2.  Produce the string that is written into a footprint's ``(model ...)`` line.
    When the 3D-model directory lives inside the user profile we emit a
    *portable* reference using the ``${USERPROFILE}`` / ``${HOME}`` environment
    variable (KiCad expands OS environment variables in paths), so the library
    keeps working when copied to another machine or a different user account.
    When the directory is *outside* the user profile we fall back to the fixed
    absolute path.

PartHive is licensed under GPL-3.0-or-later (see LICENSE).
"""

from __future__ import annotations

import os
from pathlib import Path

BRAND = "PartHive"
# Shown in the dialog title. Keep in step with metadata.json "version" (the
# PCM zip build checks they match).
VERSION = "1.1.0"
DEFAULT_LIB_NAME = "AddedParts"

# Advanced mode organises the library dir into per-type subfolders.
SUBDIR_SYMBOLS = "symbols"
SUBDIR_FOOTPRINTS = "footprints"
SUBDIR_3D = "3dmodels"


def _posix(path: str) -> str:
    """KiCad stores paths with forward slashes on every platform."""
    return path.replace("\\", "/")


def get_documents_dir() -> Path:
    """Return the user's Documents folder, honouring Windows redirection.

    On Windows the real location can be redirected (OneDrive, a different
    drive, a localized name), so we ask the shell registry first and only then
    fall back to guesses.
    """
    if os.name == "nt":
        for hive_key in (
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders",
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders",
        ):
            try:
                import winreg

                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, hive_key) as key:
                    raw, _ = winreg.QueryValueEx(key, "Personal")
                candidate = Path(os.path.expandvars(raw))
                if candidate.is_dir():
                    return candidate
            except OSError:
                continue

    for candidate in (
        Path.home() / "Documents",
        Path.home() / "OneDrive" / "Documents",
    ):
        if candidate.is_dir():
            return candidate
    return Path.home() / "Documents"


def default_library_dir(lib_name: str = DEFAULT_LIB_NAME) -> Path:
    """Default *library dir*: ``<Documents>/KiCad/<lib_name>``.

    This single folder holds ``<name>.kicad_sym``, ``<name>.pretty`` and
    ``<name>.3dshapes`` — matching the layout used across the KiCad ecosystem.
    """
    return get_documents_dir() / "KiCad" / lib_name


def _home_relative_parts(path: Path) -> tuple[str, ...] | None:
    """If ``path`` is inside the user's home dir, return the remaining parts
    (with their real casing); otherwise return None.

    Uses case-insensitive comparison on Windows (``C:\\Users\\Rafiu`` and
    ``c:\\users\\rafiu`` denote the same folder) while preserving the original
    path casing in the returned parts.
    """
    try:
        home = Path.home().resolve()
        target = path.resolve()
    except OSError:
        home = Path.home()
        target = path

    home_parts = home.parts
    target_parts = target.parts
    if len(target_parts) < len(home_parts):
        return None

    def same(a: str, b: str) -> bool:
        return os.path.normcase(a) == os.path.normcase(b)

    if all(same(target_parts[i], home_parts[i]) for i in range(len(home_parts))):
        return target_parts[len(home_parts):]
    return None


def portable_path(path: Path) -> str:
    """Render ``path`` for storage in a KiCad file.

    * Inside the user profile  ->  ``${USERPROFILE}/<relative>`` (Windows) or
      ``${HOME}/<relative>`` (macOS/Linux) — portable across machines/users,
      since KiCad expands OS environment variables in stored paths.
    * Outside the user profile ->  the fixed absolute path.
    """
    rel_parts = _home_relative_parts(path)
    if rel_parts is None:
        # Fixed absolute location (target sits outside the user profile).
        return _posix(str(path.resolve() if path.is_absolute() else path))

    var = "USERPROFILE" if os.name == "nt" else "HOME"
    return "${" + var + "}/" + "/".join(rel_parts)


def model_dir_reference(threed_dir: Path) -> str:
    """Return the ``.3dshapes`` directory path for a footprint's ``(model ...)``
    line. The footprint exporter appends ``/<model_name>.<ext>``."""
    return portable_path(threed_dir)


def _type_bases(library_dir: Path, advanced: bool) -> tuple[Path, Path, Path]:
    """Return (symbols_base, footprints_base, models_base).

    * simple mode   -> all three are the library dir itself (flat layout)
    * advanced mode -> library_dir/symbols, /footprints, /3dmodels
    """
    library_dir = Path(library_dir).expanduser()
    if advanced:
        return (
            library_dir / SUBDIR_SYMBOLS,
            library_dir / SUBDIR_FOOTPRINTS,
            library_dir / SUBDIR_3D,
        )
    return (library_dir, library_dir, library_dir)


def library_paths(
    library_dir: Path,
    sym_name: str,
    fp_name: str,
    model_name: str,
    advanced: bool = False,
) -> dict:
    """Compute the concrete file/dir paths for the three library components.

    In advanced mode each component lives in its own subfolder of the library
    dir (symbols/, footprints/, 3dmodels/); in simple mode all three sit
    directly in the library dir.
    """
    library_dir = Path(library_dir).expanduser()
    sym_base, fp_base, model_base = _type_bases(library_dir, advanced)
    return {
        "library_dir": library_dir,
        "symbol_lib": sym_base / f"{sym_name}.kicad_sym",
        "footprint_dir": fp_base / f"{fp_name}.pretty",
        "model_dir": model_base / f"{model_name}.3dshapes",
    }


# --------------------------------------------------------------------------
# Name rules
#
# A library name becomes a file/folder name (<name>.kicad_sym, <name>.pretty,
# <name>.3dshapes), a KiCad library nickname (the "Lib" in "Lib:R0603"), and
# part of a path in KiCad's library tables. Footprint / 3D-model names from
# EasyEDA become file names too. So a name must be safe for all of them:
#
# * no  < > : " / \ | ? *   (illegal on Windows; ':' splits "Lib:Item")
# * no  $ %                 (KiCad / Windows read ${VAR}, $(VAR), %VAR% as variables)
# * no  control characters  (tabs, newlines, ...)
# * not a Windows device name: CON PRN AUX NUL COM1-9 LPT1-9 (with any extension)
# * no leading/trailing space, no leading or trailing dot
# * at most MAX_NAME characters (Windows paths stop at 260)
# --------------------------------------------------------------------------
_BAD_CHARS = '<>:"/\\|?*$%'
_RESERVED = (
    {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)
MAX_NAME = 64
MAX_DIR_PATH = 180  # leaves room for subfolders + "<name>.pretty\<footprint>.kicad_mod"


def _is_reserved(name: str) -> bool:
    return name.split(".")[0].strip().upper() in _RESERVED


def lib_name_problem(name: str) -> str | None:
    """Why ``name`` can't be used as a library name, or None if it's fine."""
    if not (name or "").strip():
        return "Enter a name."
    bad = sorted({c for c in name if c in _BAD_CHARS})
    if bad:
        return "Library names can't contain " + " ".join(bad) + "."
    if any(ord(c) < 32 or ord(c) == 127 for c in name):
        return "Library names can't contain tabs or other control characters."
    if name != name.strip():
        return "Library names can't start or end with a space."
    if name.startswith(".") or name.endswith("."):
        return "Library names can't start or end with a dot."
    if _is_reserved(name):
        return f"'{name}' is a reserved device name on Windows — choose another name."
    if len(name) > MAX_NAME:
        return f"Keep library names to {MAX_NAME} characters or fewer."
    return None


def _clean(name: str, limit: int) -> str:
    name = "".join("_" if (c in _BAD_CHARS or ord(c) < 32 or ord(c) == 127) else c
                   for c in (name or ""))
    name = name.replace("..", "_").strip(" .")
    name = name[:limit].rstrip(" .")
    if name and _is_reserved(name):
        stem, dot, rest = name.partition(".")
        name = f"{stem}_{dot}{rest}"  # "CON" -> "CON_", "lpt9.old" -> "lpt9_.old"
    return name


def safe_lib_name(name: str, default: str = DEFAULT_LIB_NAME) -> str:
    """Turn any text (e.g. a folder name) into a valid library name by replacing
    what :func:`lib_name_problem` would reject. Also neutralises path traversal,
    so even a hand-edited config can't escape the library dir."""
    return _clean(name, MAX_NAME) or default


def safe_item_name(name: str, default: str = "part") -> str:
    """The same rules for a footprint / 3D-model / symbol name that comes from
    EasyEDA and becomes a file name or the part after ':' in "Lib:Item"."""
    return _clean(name, 120) or default


def lib_dir_problem(path: str | Path) -> str | None:
    """Why ``path`` can't be used as a library folder, or None if it's fine."""
    text = str(path or "").strip()
    if not text:
        return "Choose a library folder."
    if "$" in text or "%" in text:
        return ("This folder's path contains '$' or '%', which KiCad reads as a "
                "variable in library paths. Choose a folder without them.")
    if any(ord(c) < 32 for c in text):
        return "This folder's path contains control characters. Choose another folder."
    if len(text) > MAX_DIR_PATH:
        return (f"This folder's path is {len(text)} characters long. Windows stops at "
                f"260 for the files inside it — choose a folder with a path under "
                f"{MAX_DIR_PATH} characters.")
    parts = Path(text).parts[1:]  # skip the drive
    bad = next((p for p in parts if _is_reserved(p)), None)
    if bad:
        return f"The folder name '{bad}' is reserved on Windows. Choose another folder."
    return None


def _stems(base: Path, suffix: str) -> list[str]:
    """Names (without suffix) of the libraries of one type found in `base`."""
    if not base.is_dir():
        return []
    names = [
        entry.name[: -len(suffix)]
        for entry in base.iterdir()
        # require a non-empty stem: skip an entry named exactly the suffix
        # (e.g. a stray file/folder literally called ".pretty")
        if entry.name.endswith(suffix) and len(entry.name) > len(suffix)
    ]
    return sorted(set(names), key=str.lower)


def scan_libraries(library_dir: Path, advanced: bool = False) -> dict:
    """List existing library names of each type inside the library dir.

    Returns {"symbols": [...], "footprints": [...], "models": [...]}.
    """
    sym_base, fp_base, model_base = _type_bases(library_dir, advanced)
    return {
        "symbols": _stems(sym_base, ".kicad_sym"),
        "footprints": _stems(fp_base, ".pretty"),
        "models": _stems(model_base, ".3dshapes"),
    }


def detect_advanced_layout(library_dir: Path, default: bool = True) -> bool:
    """Auto-detect the on-disk layout of ``library_dir`` (replaces the old manual
    "Advanced" toggle), so PartHive adapts to whatever a dir already uses:

    * subfolder libraries present (symbols/ footprints/ 3dmodels/) -> True  (advanced)
    * flat libraries present directly in the dir                   -> False (simple)
    * empty or missing dir                                         -> ``default``
    """
    if any(scan_libraries(library_dir, advanced=True).values()):
        return True
    if any(scan_libraries(library_dir, advanced=False).values()):
        return False
    return default
