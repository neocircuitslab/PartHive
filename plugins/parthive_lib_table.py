"""PartHive – register libraries in KiCad's global library tables.

Adds a symbol-library entry to ``sym-lib-table`` and a footprint-library entry
to ``fp-lib-table`` so the imported parts show up in KiCad automatically.

Design goals:
* **Safe**   – never corrupt an existing table: a timestamped backup is written
  before any change, and entries are only *added* (never rewritten/removed).
* **Idempotent** – if a nickname already exists, nothing is done.
* **Honest** – editing the *global* table while KiCad is running means the new
  library only appears after KiCad is restarted; the caller surfaces that.

PartHive is licensed under AGPL-3.0-or-later (see LICENSE).
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import sys
from pathlib import Path

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


def find_settings_dir() -> Path | None:
    """Return the KiCad version dir holding the global library tables.

    Prefers the newest version directory that actually contains a
    ``sym-lib-table``/``fp-lib-table`` pair.
    """
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


def _add_entry(table_file: Path, root_token: str, entry: str) -> tuple[bool, str]:
    """Insert ``entry`` before the final ``)`` of ``table_file``.

    Returns (changed, message).
    """
    if table_file.exists():
        content = table_file.read_text(encoding="utf-8")
    else:
        content = f"({root_token}\n)\n"

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
    table_file.write_text(new_content, encoding="utf-8")
    return True, f"added '{nickname}'"


def register_symbol_library(settings_dir: Path, nickname: str, uri: str) -> tuple[bool, str]:
    entry = (
        f'(lib (name "{nickname}")(type "KiCad")(uri "{uri}")'
        f'(options "")(descr "Added by PartHive"))'
    )
    return _add_entry(settings_dir / "sym-lib-table", "sym_lib_table", entry)


def register_footprint_library(settings_dir: Path, nickname: str, uri: str) -> tuple[bool, str]:
    entry = (
        f'(lib (name "{nickname}")(type "KiCad")(uri "{uri}")'
        f'(options "")(descr "Added by PartHive"))'
    )
    return _add_entry(settings_dir / "fp-lib-table", "fp_lib_table", entry)
