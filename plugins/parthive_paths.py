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

PartHive is licensed under AGPL-3.0-or-later (see LICENSE).
"""

from __future__ import annotations

import os
from pathlib import Path

BRAND = "PartHive"
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


def safe_lib_name(name: str, default: str = DEFAULT_LIB_NAME) -> str:
    """Sanitise a library name for safe use as a filename and lib-table nickname.

    Removes characters illegal in KiCad nicknames and neutralises path traversal
    (``..`` / separators), so even a hand-edited config cannot escape the library
    dir when the names are joined into paths.
    """
    name = (name or "").strip()
    for ch in '<>:"/\\|?*':
        name = name.replace(ch, "_")
    name = name.replace("..", "_").strip(" .")
    return name or default


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
