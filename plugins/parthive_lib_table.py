"""PartHive – register libraries in KiCad's library tables.

Adds a symbol-library entry to ``sym-lib-table`` and a footprint-library entry
to ``fp-lib-table`` so the imported parts show up in KiCad automatically.

Two scopes are supported:

* **global**  – the tables in KiCad's settings dir (visible in every project).
* **project** – the tables next to a project's ``.kicad_pro`` (visible only in
  that project). Paths inside the project folder are written relative to
  ``${KIPRJMOD}`` so the project stays portable.

Design goals:
* **Safe**   – never corrupt an existing table: a timestamped backup is written
  before any change, and entries are only *added* (never rewritten/removed).
* **Idempotent** – if a nickname already exists, nothing is done.
* **Honest** – editing a table while KiCad has it loaded means the new library
  only appears after KiCad is restarted (global) or the project is reopened
  (project); the caller surfaces that.

PartHive is licensed under GPL-3.0-or-later (see LICENSE).
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from parthive_config import write_text_atomic  # noqa: E402

logger = logging.getLogger("parthive.libtable")


def kicad_config_root() -> Path | None:
    """Base KiCad configuration directory (version-independent parent)."""
    if os.name == "nt":
        base = os.environ.get("APPDATA")
        root = Path(base) / "kicad" if base else Path.home() / "AppData" / "Roaming" / "kicad"
    elif sys.platform == "darwin":
        root = Path.home() / "Library" / "Preferences" / "kicad"
    else:
        xdg = os.environ.get("XDG_CONFIG_HOME")
        root = (Path(xdg) if xdg else Path.home() / ".config") / "kicad"
    return root if root.is_dir() else None


def _running_kicad_settings_dir() -> Path | None:
    """The settings folder of the KiCad that is running PartHive, if known."""
    # KiCad honours KICAD_CONFIG_HOME instead of %APPDATA%\kicad\<version>.
    env = os.environ.get("KICAD_CONFIG_HOME")
    if env and Path(env).is_dir():
        return Path(env)
    # Inside KiCad (SWIG): ask KiCad itself.
    pcbnew = sys.modules.get("pcbnew")
    sm = getattr(pcbnew, "SETTINGS_MANAGER", None) if pcbnew else None
    if sm is not None:
        try:
            path = Path(str(sm.GetUserSettingsPath()))
            if path.is_dir():
                return path
        except Exception:  # noqa: BLE001
            logger.debug("GetUserSettingsPath failed", exc_info=True)
    # IPC plugin: KiCad runs us from ...\KiCad\<version>\python-environments\...
    m = re.search(r"[\\/]kicad[\\/](\d+\.\d+)[\\/]python-environments[\\/]",
                  sys.prefix, re.IGNORECASE)
    root = kicad_config_root()
    if m and root is not None and (root / m.group(1)).is_dir():
        return root / m.group(1)
    return None


def find_settings_dir() -> Path | None:
    """Return the KiCad settings dir holding the global library tables.

    The running KiCad's own folder when it can be determined (so KiCad 9 and
    10 installed side by side each get their own tables); otherwise the newest
    version directory that contains a ``sym-lib-table``/``fp-lib-table``.
    """
    running = _running_kicad_settings_dir()
    if running is not None:
        return running
    root = kicad_config_root()
    if root is None:
        return None

    candidates: list[Path] = []
    for child in root.iterdir():
        if child.is_dir() and re.fullmatch(r"\d+\.\d+", child.name):
            if (child / "sym-lib-table").exists() or (child / "fp-lib-table").exists():
                candidates.append(child)
    if not candidates:
        # Some installs keep the tables directly in the root.
        if (root / "sym-lib-table").exists() or (root / "fp-lib-table").exists():
            return root
        return None

    def version_key(p: Path) -> tuple[int, int]:
        major, minor = p.name.split(".")
        return (int(major), int(minor))

    return sorted(candidates, key=version_key)[-1]


def _nickname_present(content: str, nickname: str) -> bool:
    # Matches (name "Nick") or (name Nick)
    pattern = r'\(\s*name\s+"?' + re.escape(nickname) + r'"?\s*\)'
    return re.search(pattern, content) is not None


# --------------------------------------------------------------------------
# Reading tables: which nicknames are taken, and by what
# --------------------------------------------------------------------------
_FIELD = r'\(\s*{key}\s+(?:"((?:[^"\\]|\\.)*)"|([^\s()]+))\s*\)'


def _field(chunk: str, key: str) -> str:
    m = re.search(_FIELD.format(key=key), chunk)
    if not m:
        return ""
    return (m.group(1) if m.group(1) is not None else m.group(2) or "").replace('\\"', '"')


_KICAD_VAR = re.compile(r"\$\{([^}]*)\}|\$\(([^)]*)\)")


def expand_uri(uri: str, project_dir: Path | None = None) -> str:
    """Expand a library-table path the way KiCad does: ``${VAR}`` / ``$(VAR)``
    only (``${KIPRJMOD}`` = the project folder). Unknown variables are left as
    they are; ``%VAR%`` and bare ``$VAR`` are NOT variables to KiCad."""
    def repl(m: re.Match) -> str:
        var = m.group(1) if m.group(1) is not None else m.group(2)
        if var == "KIPRJMOD" and project_dir is not None:
            return str(project_dir)
        value = os.environ.get(var)
        return value if value is not None else m.group(0)

    text = _KICAD_VAR.sub(repl, uri or "")
    try:
        return os.path.normpath(text)
    except (TypeError, ValueError):
        return text


def _same_path(a: str, b: str) -> bool:
    """Same file? Resolves mapped / subst drives and links first (PartHive
    writes resolved paths, e.g. a mapped Z:\\ becomes \\\\server\\share)."""
    def norm(p: str) -> str:
        try:
            p = os.path.realpath(p)
        except (OSError, ValueError):
            pass
        return os.path.normcase(os.path.normpath(p))
    if os.path.normcase(a) == os.path.normcase(b):
        return True
    return norm(a) == norm(b)


_TABLE_CACHE: dict = {}


def table_entries(
    table_file: Path, project_dir: Path | None = None, _seen: set | None = None
) -> dict[str, str]:
    """``{nickname: expanded uri}`` for every library in ``table_file``,
    including libraries pulled in through nested ``(type "Table")`` entries
    (KiCad 10 lists its built-in libraries that way)."""
    try:
        key = os.path.normcase(str(Path(table_file).resolve()))
    except OSError:
        key = str(table_file)
    if _seen is None:
        # Top-level call: reuse the parse while the file is unchanged.
        try:
            stamp = (key, Path(table_file).stat().st_mtime_ns, str(project_dir))
        except OSError:
            return {}
        cached = _TABLE_CACHE.get(stamp)
        if cached is None:
            cached = table_entries(table_file, project_dir, set())
            if len(_TABLE_CACHE) > 32:
                _TABLE_CACHE.clear()
            _TABLE_CACHE[stamp] = cached
        return dict(cached)
    seen = _seen
    if key in seen or not Path(table_file).is_file():
        return {}
    seen.add(key)
    try:
        content = Path(table_file).read_text(encoding="utf-8")
    except OSError:
        return {}
    entries: dict[str, str] = {}
    for chunk in re.split(r"\(\s*lib\s", content)[1:]:
        name = _field(chunk, "name")
        uri = _field(chunk, "uri")
        if not name:
            continue
        if _field(chunk, "type").lower() == "table":
            nested = expand_uri(uri, project_dir)
            for n, u in table_entries(Path(nested), project_dir, seen).items():
                entries.setdefault(n, u)
            continue
        entries.setdefault(name, expand_uri(uri, project_dir))
    return entries


def scope_tables(kind: str, scope: str, project_dir: Path | str | None) -> list:
    """Tables whose nicknames a new ``kind`` ("sym"/"fp") library must not
    collide with: the global table, plus the project's table for project scope
    (KiCad shows both together, so a name must be unique across them)."""
    fname = "sym-lib-table" if kind == "sym" else "fp-lib-table"
    out: list = []
    pdir = Path(project_dir) if project_dir else None
    settings = find_settings_dir()
    if settings is not None:
        out.append((settings / fname, pdir))
    if scope == "project" and pdir is not None:
        out.append((pdir / fname, pdir))
    return out


def conflict_tables(kind: str, project_dir: Path | str | None) -> list:
    """Every table a library name must be unique across: the global table and,
    when a project is open, the project's table — whichever card is selected
    (inside that project KiCad shows both, and a project library hides a global
    one with the same name)."""
    return scope_tables(kind, "project" if project_dir else "global", project_dir)


def nickname_conflict(nickname: str, target: Path, tables: list) -> str | None:
    """If ``nickname`` is already registered for a DIFFERENT library than
    ``target``, return that library's path; else None (free, or it is already
    this very library)."""
    want = expand_uri(str(target))
    key = nickname.casefold()
    for table_file, pdir in tables:
        # Case-insensitive: "parts" next to "Parts" is a recipe for confusion,
        # and on Windows both names map to the same files.
        for name, uri in table_entries(table_file, pdir).items():
            if name.casefold() == key and not _same_path(uri, want):
                return uri
    return None


def registration_state(
    nickname: str, target: Path, kind: str, scope: str, project_dir: Path | str | None
) -> str:
    """Where ``nickname`` (for the library at ``target``) stands in the table
    PartHive registers it in:

    * "conflict"   – the name is used for a different library
    * "registered" – already in the table
    * "missing"    – not in the table yet (added on the next import)
    * "unknown"    – KiCad's settings dir could not be found
    """
    tables = scope_tables(kind, scope, project_dir)
    if not tables:
        return "unknown"
    if nickname_conflict(nickname, target, tables):
        return "conflict"
    table_file, pdir = tables[-1]  # global table, or the project's own table
    return "registered" if nickname in table_entries(table_file, pdir) else "missing"


def loaded_footprint_libs() -> set | None:
    """Footprint library names the RUNNING KiCad has loaded, when PartHive runs
    inside the PCB editor (SWIG). A library that is in the table on disk but
    not in this set needs a KiCad restart (or project reopen). None = unknown."""
    pcbnew = sys.modules.get("pcbnew")
    getter = getattr(pcbnew, "GetFootprintLibraries", None) if pcbnew else None
    if getter is None:
        return None
    try:
        return {str(n) for n in getter()}
    except Exception:  # noqa: BLE001
        return None


def _add_entry(table_file: Path, root_token: str, entry: str,
               create: bool = True) -> tuple[bool, str]:
    """Insert ``entry`` before the final ``)`` of ``table_file``.

    ``create=False`` (global tables): never create a missing table — KiCad would
    then find a table holding only PartHive's entry and lose its default
    libraries. Returns (changed, message).
    """
    if table_file.exists():
        content = table_file.read_text(encoding="utf-8")
    elif not create:
        return False, (f"no {table_file.name} in {table_file.parent} yet — start KiCad "
                       "once so it sets up its libraries, or add this library in "
                       "Preferences > Manage Libraries")
    else:
        content = f"({root_token}\n  (version 7)\n)\n"

    # Extract nickname from the entry for the idempotency check.
    name_match = re.search(r'\(name "([^"]+)"\)', entry)
    nickname = name_match.group(1) if name_match else ""
    if nickname and _nickname_present(content, nickname):
        return False, f"'{nickname}' already registered"

    close = content.rstrip().rfind(")")
    if close == -1:
        return False, f"malformed {table_file.name}"

    # Back up before touching the file.
    try:
        backup = table_file.with_suffix(table_file.suffix + ".parthive-bak")
        if table_file.exists():
            shutil.copy2(table_file, backup)
    except OSError as exc:
        logger.warning("Could not back up %s: %s", table_file.name, exc)

    new_content = content[:close] + "  " + entry + "\n" + content[close:]
    write_text_atomic(table_file, new_content)  # atomic, keeps KiCad's line endings
    return True, f"added '{nickname}'"


def register_symbol_library(settings_dir: Path, nickname: str, uri: str,
                            create: bool = True) -> tuple[bool, str]:
    """``settings_dir`` is the folder holding the table: KiCad's settings dir
    (global, ``create=False``) or the project folder (project)."""
    entry = (
        f'(lib (name "{nickname}")(type "KiCad")(uri "{uri}")'
        f'(options "")(descr "Added by PartHive"))'
    )
    return _add_entry(settings_dir / "sym-lib-table", "sym_lib_table", entry, create)


def register_footprint_library(settings_dir: Path, nickname: str, uri: str,
                               create: bool = True) -> tuple[bool, str]:
    entry = (
        f'(lib (name "{nickname}")(type "KiCad")(uri "{uri}")'
        f'(options "")(descr "Added by PartHive"))'
    )
    return _add_entry(settings_dir / "fp-lib-table", "fp_lib_table", entry, create)


# --------------------------------------------------------------------------
# Project scope
# --------------------------------------------------------------------------
def find_project_root(start: Path | str | None) -> Path | None:
    """Walk up from ``start`` and return the first folder that holds a
    ``*.kicad_pro`` file, or None."""
    if not start:
        return None
    try:
        p = Path(start).expanduser().resolve()
    except OSError:
        return None
    if p.is_file():
        p = p.parent
    for folder in (p, *p.parents):
        try:
            if folder.is_dir() and any(folder.glob("*.kicad_pro")):
                return folder
        except OSError:
            continue
    return None


def is_project_dir(path: Path | str | None) -> bool:
    """True if ``path`` is a folder that directly contains a ``*.kicad_pro``."""
    if not path:
        return False
    try:
        folder = Path(path).expanduser()
        return folder.is_dir() and any(folder.glob("*.kicad_pro"))
    except OSError:
        return False


def detect_open_project() -> Path | None:
    """Best-effort: the folder of the project currently open in KiCad.

    * SWIG action plugin (runs inside pcbnew): the open board's file name.
    * IPC plugin: the KiCad IPC API via ``kipy``, if that package is installed.
    Returns None when neither is available (the user then picks the folder).
    """
    # 1) Inside KiCad's own process (SWIG). Only use pcbnew if KiCad already
    #    loaded it — importing it fresh in the IPC subprocess is pointless.
    pcbnew = sys.modules.get("pcbnew")
    if pcbnew is not None:
        try:
            board = pcbnew.GetBoard()
            name = board.GetFileName() if board is not None else ""
            if name:
                # A KiCad project is the folder holding the board + .kicad_pro.
                return Path(name).resolve().parent
        except Exception:  # noqa: BLE001
            logger.debug("pcbnew project lookup failed", exc_info=True)

    # 2) IPC API (KiCad 9+). KiCad starts the plugin with KICAD_API_SOCKET set to
    #    the instance that launched it, so with two KiCads open each plugin run
    #    talks to its own KiCad and gets its own project.
    if os.environ.get("KICAD_API_SOCKET"):
        try:
            from kipy import KiCad  # type: ignore[import-not-found]
            from kipy.proto.common.types import DocumentType  # type: ignore[import-not-found]

            kicad = KiCad()
            for doc_type in (DocumentType.DOCTYPE_PCB, DocumentType.DOCTYPE_SCHEMATIC):
                try:
                    docs = kicad.get_open_documents(doc_type)
                except Exception:  # noqa: BLE001 - e.g. schematic API not supported
                    continue
                for doc in docs:
                    proj_path = getattr(getattr(doc, "project", None), "path", "")
                    if proj_path:
                        return Path(proj_path)
        except Exception:  # noqa: BLE001
            logger.debug("IPC project lookup failed", exc_info=True)
    return None


def project_relative_uri(target: Path, project_dir: Path) -> str | None:
    """``${KIPRJMOD}/<relative>`` if ``target`` is inside ``project_dir``,
    else None."""
    try:
        rel = Path(target).expanduser().resolve().relative_to(
            Path(project_dir).expanduser().resolve()
        )
    except (OSError, ValueError):
        return None
    rel_s = rel.as_posix()
    return "${KIPRJMOD}" if rel_s in ("", ".") else "${KIPRJMOD}/" + rel_s
