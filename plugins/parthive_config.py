"""PartHive – persistent user settings.

Settings live in a user-writable location (never inside the plugin install
directory, which may be read-only for PCM installs):

* Windows: ``%APPDATA%/PartHive/settings.json``
* macOS/Linux: ``$XDG_CONFIG_HOME/PartHive/settings.json`` (or ``~/.config/...``)

PartHive is licensed under GPL-3.0-or-later (see LICENSE).
"""

from __future__ import annotations

import json
import logging
import os
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
    symbol_lib_name: str = DEFAULT_LIB_NAME
    footprint_lib_name: str = DEFAULT_LIB_NAME
    model_lib_name: str = DEFAULT_LIB_NAME

    # Advanced mode: organise the library dir into symbols/ footprints/
    # 3dmodels/ subfolders and pick (or create) the target library per type.
    advanced_mode: bool = False

    skip_3d: bool = False
    prefer_step: bool = True
    compress_models: bool = False

    symbol_text_size: float = 1.27  # mm
    footprint_text_size: float = 1.0  # mm

    overwrite: bool = False
    auto_register: bool = True
    # Close the (modal) plugin dialog after a successful import, so it stops
    # blocking the other KiCad windows.
    close_after_import: bool = False

    # Not persisted-critical but handy to remember.
    last_component: str = ""

    _extra: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.library_dir:
            self.library_dir = str(default_library_dir(self.model_lib_name))

    # ---- persistence -----------------------------------------------------
    @classmethod
    def load(cls) -> "ParthiveConfig":
        path = config_path()
        if not path.is_file():
            return cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            known = {f for f in cls.__dataclass_fields__ if not f.startswith("_")}
            clean = {k: v for k, v in data.items() if k in known}
            return cls(**clean)
        except (OSError, ValueError, TypeError) as exc:
            logger.warning("Could not read settings (%s); using defaults.", exc)
            return cls()

    def save(self) -> None:
        path = config_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            data = {k: v for k, v in asdict(self).items() if not k.startswith("_")}
            path.write_text(json.dumps(data, indent=2), encoding="utf-8")
            logger.info("Saved settings to %s", path)
        except OSError as exc:
            logger.warning("Could not save settings (%s).", exc)
