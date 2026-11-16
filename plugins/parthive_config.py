"""PartHive – persistent user settings.

Settings live in a user-writable location (never inside the plugin install
directory, which may be read-only for PCM installs):

* Windows: ``%APPDATA%/PartHive/settings.json``
* macOS/Linux: ``$XDG_CONFIG_HOME/PartHive/settings.json`` (or ``~/.config/...``)

Per-project choices (global vs. project library, and the project's library
dir) live next to it in ``projects/<name>-<hash>.json``, one file per project.

PartHive is licensed under GPL-3.0-or-later (see LICENSE).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from parthive_paths import DEFAULT_LIB_NAME, default_library_dir

logger = logging.getLogger("parthive.config")


def config_dir() -> Path:
    if os.name == "nt":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "PartHive"
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else (Path.home() / ".config")
    return base / "PartHive"


def config_path() -> Path:
    return config_dir() / "settings.json"


@dataclass
class ParthiveConfig:
    """All user-facing settings, with their defaults."""

    library_dir: str = ""
    # Recently-used library directories (most-recent first) — shown as a dropdown
    # history in the "Library dir" picker so project-specific dirs are easy to reuse.
    library_dir_history: list = field(default_factory=list)
    # Remembered library layout per directory: {dir_path: advanced_bool}. Lets dir
    # swapping restore the layout (flat vs organized) last used for that dir.
    dir_layouts: dict = field(default_factory=dict)
    # Remembered target libraries per directory:
    #   {dir_path: {"symbols": name, "footprints": name, "models": name}}
    dir_libs: dict = field(default_factory=dict)
    symbol_lib_name: str = DEFAULT_LIB_NAME
    footprint_lib_name: str = DEFAULT_LIB_NAME
    model_lib_name: str = DEFAULT_LIB_NAME

    # Default library layout for new / empty dirs, chosen via the Flat/Organized
    # radios (True = organized symbols/ footprints/ 3dmodels/ subfolders, False =
    # flat). Per-dir choices are remembered in `dir_layouts`.
    advanced_mode: bool = True

    skip_3d: bool = False
    prefer_step: bool = True
    compress_models: bool = False

    symbol_text_size: float = 1.27  # mm
    footprint_text_size: float = 1.0  # mm

    overwrite: bool = False
    # Show the per-item confirmation dialog before writing (works in both
    # layouts; previously this was implicitly tied to "advanced" mode).
    confirm_import: bool = True
    auto_register: bool = True
    # Where to register for THIS import: "global" or "project". Not saved here —
    # the choice is remembered per project (see ProjectSettings).
    register_scope: str = "global"
    # Project folder used when register_scope == "project". Not saved here.
    project_dir: str = ""
    # Pin project libraries PartHive adds at the top of KiCad's Add Symbol /
    # Add Footprint lists (written into the .kicad_pro while it is closed).
    pin_project_libs: bool = True
    # Electrical pin types: "smart" (KiCad conventions; EasyEDA's are mostly
    # undefined or wrong), "keep" (EasyEDA's as they are) or "passive" (all).
    pin_types: str = "smart"
    # Close the (modal) plugin dialog after a successful import, so it stops
    # blocking the other KiCad windows.
    close_after_import: bool = False

    # Not persisted-critical but handy to remember.
    last_component: str = ""

    _extra: dict = field(default_factory=dict)
    # What was on disk when loaded; save() writes only what changed since, so
    # two PartHive dialogs (two KiCad instances) don't undo each other's edits.
    _snapshot: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.library_dir:
            self.library_dir = str(default_library_dir(self.model_lib_name))

    # ---- persistence -----------------------------------------------------
    def _persisted(self) -> dict:
        return {
            k: v
            for k, v in asdict(self).items()
            if not k.startswith("_") and k not in _TRANSIENT
        }

    @staticmethod
    def _read_disk() -> dict:
        path = config_path()
        if not path.is_file():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError) as exc:
            logger.warning("Could not read settings (%s); using defaults.", exc)
            return {}

    @classmethod
    def load(cls) -> "ParthiveConfig":
        data = cls._read_disk()
        known = {
            f for f in cls.__dataclass_fields__ if not f.startswith("_") and f not in _TRANSIENT
        }
        clean = {k: v for k, v in data.items() if k in known}
        try:
            cfg = cls(**clean)
        except TypeError as exc:
            logger.warning("Bad settings (%s); using defaults.", exc)
            cfg = cls()
        cfg._snapshot = json.loads(json.dumps(cfg._persisted()))
        return cfg

    def save(self) -> None:
        """Merge this session's changes into the file on disk.

        Only fields (and, for dict fields, keys) that changed since load() are
        written, on top of whatever another PartHive instance saved meanwhile.
        """
        mine = self._persisted()
        disk = self._read_disk()
        merged = dict(disk) if disk else dict(mine)
        for key, value in mine.items():
            before = self._snapshot.get(key)
            if value == before and key in disk:
                continue  # untouched here: keep what's on disk
            if isinstance(value, dict) and isinstance(before, dict):
                target = dict(disk.get(key) or {}) if isinstance(disk.get(key), dict) else {}
                for k in set(value) | set(before):
                    if value.get(k) != before.get(k):
                        if k in value:
                            target[k] = value[k]
                        else:
                            target.pop(k, None)
                merged[key] = target
            else:
                merged[key] = value
        if _write_json(config_path(), merged):
            self._snapshot = json.loads(json.dumps(mine))
            logger.info("Saved settings to %s", config_path())


# Runtime-only fields (never written to settings.json).
_TRANSIENT = {"register_scope", "project_dir"}


def write_text_atomic(path: Path, text: str) -> None:
    """Write ``text`` (with ``\\n`` line ends) to ``path`` safely:

    * atomic — temp file + ``os.replace``, so KiCad / another PartHive never
      reads a half-written file and a failure leaves the old file intact;
    * keeps the file's existing line endings (KiCad writes its library tables
      with CRLF on Windows but ``.kicad_pro`` with LF) — so a change doesn't
      show up as "every line changed" in git. New files get LF.

    Raises OSError on failure (callers decide how to report it).
    """
    path = Path(path)
    eol = "\n"
    try:
        if b"\r\n" in path.read_bytes()[:65536]:
            eol = "\r\n"
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8", newline=eol) as fh:
            fh.write(text.replace("\r\n", "\n"))
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def _write_json(path: Path, data: dict) -> bool:
    """Write a settings/state JSON file atomically. Returns False on failure."""
    try:
        write_text_atomic(path, json.dumps(data, indent=2))
        return True
    except OSError as exc:
        logger.warning("Could not save %s (%s).", path.name, exc)
        return False


# --------------------------------------------------------------------------
# Per-project memory
# --------------------------------------------------------------------------
PROJECT_LIB_SUBDIR = "Libraries"


def projects_dir() -> Path:
    return config_dir() / "projects"


def _norm_project(project_dir: Path | str) -> Path:
    p = Path(project_dir).expanduser()
    try:
        return p.resolve()
    except OSError:
        return p


def project_settings_path(project_dir: Path | str) -> Path:
    """One small file per project: ``projects/<name>-<hash>.json``. The hash is
    of the normalised project path, so two projects with the same folder name
    never share settings."""
    p = _norm_project(project_dir)
    digest = hashlib.sha1(os.path.normcase(str(p)).encode("utf-8")).hexdigest()[:10]
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", p.name)[:40] or "project"
    return projects_dir() / f"{name}-{digest}.json"


@dataclass
class ProjectSettings:
    """What PartHive remembers for one KiCad project.

    A project with no saved file opens on the **global** library; the file is
    only written once the user changes something for that project.
    """

    project_dir: str
    scope: str = "global"  # "global" | "project"
    library_dir: str = ""  # the project library dir; default <project>/Libraries

    def __post_init__(self) -> None:
        self.project_dir = str(_norm_project(self.project_dir))
        if not self.library_dir:
            self.library_dir = str(Path(self.project_dir) / PROJECT_LIB_SUBDIR)

    @property
    def name(self) -> str:
        return Path(self.project_dir).name

    @classmethod
    def load(cls, project_dir: Path | str) -> "ProjectSettings":
        path = project_settings_path(project_dir)
        data: dict = {}
        if path.is_file():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                data = raw if isinstance(raw, dict) else {}
            except (OSError, ValueError) as exc:
                logger.warning("Could not read project settings (%s).", exc)
        scope = data.get("scope") if data.get("scope") in ("global", "project") else "global"
        return cls(
            project_dir=str(project_dir),
            scope=scope,
            library_dir=str(data.get("library_dir") or ""),
        )

    def save(self) -> None:
        _write_json(project_settings_path(self.project_dir), asdict(self))
