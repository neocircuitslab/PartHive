"""PartHive – pin project libraries at the top of KiCad's Add Symbol /
Add Footprint lists.

KiCad keeps the pinned-library lists of a project in its ``.kicad_pro``
(``libraries.pinned_symbol_libs`` / ``pinned_footprint_libs``). While the
project is open, KiCad holds that file in memory and writes it back whenever
the project is saved or closed — so a pin written while the project is open is
wiped. A pin written while the project is CLOSED is loaded and kept.

PartHive therefore:

* pins right away when the project is closed (no ``~<name>.kicad_pro.lck``);
* otherwise starts this file as a small hidden helper (``--watch``) that waits
  until the project is closed, lets KiCad finish saving, writes the pins and
  exits — so the libraries are pinned when the project is reopened (which a new
  project library needs anyway);
* pins each library only once: if the user unpins it later, it stays unpinned.

State per project: ``%APPDATA%/PartHive/projects/<name>-<hash>.pins.json``.

PartHive is licensed under GPL-3.0-or-later (see LICENSE).
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:  # also when run as the helper with python -I
    sys.path.insert(0, str(_HERE))

from parthive_config import (  # noqa: E402
    _write_json,
    config_dir,
    project_settings_path,
    write_text_atomic,
)

logger = logging.getLogger("parthive.pins")

KINDS = ("sym", "fp")
_PIN_KEY = {"sym": "pinned_symbol_libs", "fp": "pinned_footprint_libs"}
_TABLE = {"sym": "sym-lib-table", "fp": "fp-lib-table"}
HEARTBEAT_STALE = 90  # s — a helper silent for longer than this is gone
WATCH_LIMIT = 24 * 3600  # s — a helper gives up after a day


# --------------------------------------------------------------------------
# Project files
# --------------------------------------------------------------------------
def project_file(project_dir: Path | str) -> Path | None:
    """The project's ``.kicad_pro`` (the one named after the folder if several)."""
    d = Path(project_dir)
    named = d / f"{d.name}.kicad_pro"
    if named.is_file():
        return named
    found = sorted(d.glob("*.kicad_pro"))
    return found[0] if found else None


def lock_file(pro: Path) -> Path:
    """KiCad holds ``~<name>.kicad_pro.lck`` while the project is open."""
    return pro.with_name(f"~{pro.name}.lck")


def is_open_in_kicad(pro: Path) -> bool:
    return lock_file(pro).exists()


def parthive_project_libs(project_dir: Path | str) -> dict:
    """Nicknames PartHive added to the project's own library tables."""
    out: dict = {k: [] for k in KINDS}
    for kind in KINDS:
        table = Path(project_dir) / _TABLE[kind]
        try:
            content = table.read_text(encoding="utf-8")
        except OSError:
            continue
        for chunk in re.split(r"\(\s*lib\s", content)[1:]:
            if "Added by PartHive" not in chunk:
                continue
            m = re.search(r'\(\s*name\s+(?:"((?:[^"\\]|\\.)*)"|([^\s()]+))\s*\)', chunk)
            if m:
                out[kind].append(m.group(1) if m.group(1) is not None else m.group(2))
    return out


def pinned_in_file(pro: Path) -> dict:
    try:
        libs = json.loads(pro.read_text(encoding="utf-8")).get("libraries") or {}
    except (OSError, ValueError):
        return {k: set() for k in KINDS}
    return {k: set(libs.get(_PIN_KEY[k]) or []) for k in KINDS}


def write_pins(pro: Path, names: dict, remove: dict | None = None) -> bool:
    """Add ``names`` and drop ``remove`` ({kind: [nickname]}) in the project's
    pinned lists. Only call while the project is closed. Returns True if the
    file now matches."""
    remove = remove or {}
    try:
        data = json.loads(pro.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("Cannot read %s: %s", pro.name, exc)
        return False
    libs = data.setdefault("libraries", {})
    changed = False
    for kind in KINDS:
        current = list(libs.get(_PIN_KEY[kind]) or [])
        for name in remove.get(kind, []):
            if name in current:
                current.remove(name)
                changed = True
        for name in names.get(kind, []):
            if name not in current:
                current.append(name)
                changed = True
        libs[_PIN_KEY[kind]] = current
    if changed:
        backup = pro.with_name(pro.name + ".parthive-bak")
        try:
            if not backup.exists():
                backup.write_bytes(pro.read_bytes())
        except OSError:
            pass
        try:
            # atomic, and keeps the file's line endings (KiCad writes LF here)
            write_text_atomic(pro, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
        except OSError as exc:
            logger.warning("Cannot write %s: %s", pro.name, exc)
            return False
    have = pinned_in_file(pro)
    added = all(n in have[k] for k in KINDS for n in names.get(k, []))
    dropped = not any(n in have[k] for k in KINDS for n in remove.get(k, []))
    return added and dropped


# --------------------------------------------------------------------------
# Per-project state: what is pending, what PartHive already pinned once
# --------------------------------------------------------------------------
def _state_path(project_dir: Path | str) -> Path:
    p = project_settings_path(project_dir)
    return p.with_name(p.stem + ".pins.json")


def _load_state(project_dir: Path | str) -> dict:
    try:
        raw = json.loads(_state_path(project_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {}
    state = {
        "pending": {k: list((raw.get("pending") or {}).get(k) or []) for k in KINDS},
        "done": {k: list((raw.get("done") or {}).get(k) or []) for k in KINDS},
        "unpin": {k: list((raw.get("unpin") or {}).get(k) or []) for k in KINDS},
        "heartbeat": float(raw.get("heartbeat") or 0),
    }
    return state


def _save_state(project_dir: Path | str, state: dict) -> None:
    _write_json(_state_path(project_dir), state)


def _outstanding(state: dict) -> dict:
    return {k: [n for n in state["pending"][k] if n not in state["done"][k]] for k in KINDS}


def _unpins(state: dict) -> dict:
    return {k: list(state["unpin"][k]) for k in KINDS}


def _anything(todo: dict, drop: dict) -> bool:
    return any(todo.values()) or any(drop.values())


def _mark_done(state: dict, names: dict) -> None:
    for k in KINDS:
        for n in names.get(k, []):
            if n not in state["done"][k]:
                state["done"][k].append(n)
        state["pending"][k] = [n for n in state["pending"][k] if n not in state["done"][k]]


# --------------------------------------------------------------------------
# Public API (used by the dialog)
# --------------------------------------------------------------------------
def request_pins(project_dir: Path | str, names: dict, remove: dict | None = None) -> str:
    """Ask for ``names`` ({kind: [nickname]}) to be pinned, and ``remove`` to be
    unpinned, in this project.

    Returns "pinned" (done now), "scheduled" (when the project is closed),
    "nothing" (already pinned once / nothing to do) or "failed".
    """
    remove = remove or {}
    if not any(names.get(k) or remove.get(k) for k in KINDS):
        return "nothing"  # nothing asked: don't even create a state file
    pro = project_file(project_dir)
    if pro is None:
        return "failed"
    state = _load_state(project_dir)
    before = json.dumps(state, sort_keys=True)
    have = pinned_in_file(pro)
    for k in KINDS:
        for n in remove.get(k, []):
            # Forget it, so a later library with this name can be pinned again.
            state["pending"][k] = [x for x in state["pending"][k] if x != n]
            state["done"][k] = [x for x in state["done"][k] if x != n]
            if n in have[k] and n not in state["unpin"][k]:
                state["unpin"][k].append(n)
    for k in KINDS:
        for n in names.get(k, []):
            if n in state["done"][k]:
                continue
            if n in have[k]:  # already pinned (e.g. by hand): nothing to do, ever
                state["done"][k].append(n)
            elif n not in state["pending"][k]:
                state["pending"][k].append(n)
    todo, drop = _outstanding(state), _unpins(state)
    if not _anything(todo, drop):
        if json.dumps(state, sort_keys=True) != before:
            _save_state(project_dir, state)
        return "nothing"
    if not is_open_in_kicad(pro):
        if write_pins(pro, todo, drop):
            _mark_done(state, todo)
            state["unpin"] = {k: [] for k in KINDS}
            _save_state(project_dir, state)
            return "pinned"
        _save_state(project_dir, state)
        return "failed"
    _save_state(project_dir, state)
    return "scheduled" if _ensure_watcher(project_dir, state) else "failed"


def is_pinned(project_dir: Path | str, kind: str, name: str) -> bool:
    """Pinned in the project file, or waiting to be pinned by PartHive."""
    pro = project_file(project_dir)
    if pro is None:
        return False
    return name in pinned_in_file(pro)[kind] or name in _outstanding(_load_state(project_dir))[kind]


def move_pin(project_dir: Path | str, kind: str, old: str, new: str) -> str:
    """After a rename: if ``old`` was pinned, pin ``new`` in its place. If it
    wasn't (e.g. the user unpinned it), ``new`` stays unpinned for good."""
    if is_pinned(project_dir, kind, old):
        return request_pins(project_dir, {kind: [new]}, {kind: [old]})
    status = request_pins(project_dir, {}, {kind: [old]})
    # Mark the new name as already handled, so the safety net that pins
    # "never pinned" PartHive libraries leaves it alone.
    state = _load_state(project_dir)
    if new not in state["done"][kind]:
        state["done"][kind].append(new)
        _save_state(project_dir, state)
    return status


def drop_pin(project_dir: Path | str, kind: str, name: str) -> str:
    """After a delete: unpin ``name`` and forget it."""
    return request_pins(project_dir, {}, {kind: [name]})


def ensure_pins(project_dir: Path | str) -> str:
    """Safety net when the dialog opens: any project library PartHive added that
    was never pinned gets pinned (now, or when the project is closed)."""
    return request_pins(project_dir, parthive_project_libs(project_dir))


def pending_names(project_dir: Path | str) -> dict:
    return _outstanding(_load_state(project_dir))


def _helper_python() -> Path | None:
    exe = Path(sys.executable)
    candidates = [
        exe.with_name("pythonw.exe"),
        Path(sys.base_prefix) / "pythonw.exe",
        Path(sys.prefix) / "pythonw.exe",
        exe if exe.name.lower().startswith("python") else None,
        Path(sys.base_prefix) / "python.exe",
        Path(sys.base_prefix) / "bin" / "python3",
    ]
    return next((c for c in candidates if c is not None and c.is_file()), None)


def _ensure_watcher(project_dir: Path | str, state: dict) -> bool:
    if time.time() - state.get("heartbeat", 0) < HEARTBEAT_STALE:
        return True  # a helper is already waiting for this project
    py = _helper_python()
    if py is None:
        logger.warning("No Python found to start the pin helper.")
        return False
    flags = 0
    if os.name == "nt":
        flags = 0x00000008 | 0x00000200 | 0x08000000  # DETACHED | NEW_GROUP | NO_WINDOW
    try:
        subprocess.Popen(
            [str(py), "-I", str(Path(__file__).resolve()), "--watch", str(project_dir)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            close_fds=True, creationflags=flags, cwd=str(config_dir()),
        )
    except OSError as exc:
        logger.warning("Could not start the pin helper: %s", exc)
        return False
    state["heartbeat"] = time.time()  # claim it, so a second request doesn't start another
    _save_state(project_dir, state)
    return True


# --------------------------------------------------------------------------
# The helper: wait until the project is closed, then pin
# --------------------------------------------------------------------------
def _mtime(pro: Path) -> int:
    try:
        return pro.stat().st_mtime_ns
    except OSError:
        return -1


def watch(project_dir: str, poll: float = 0.25, settle: float = 0.5) -> int:
    """Wait for the project to close, then pin. Reacts within about a second:
    restarting KiCad reopens the last project after a few seconds, and the pin
    has to land in that gap or it waits for the next close."""
    pro = project_file(project_dir)
    if pro is None:
        return 1
    deadline = time.time() + WATCH_LIMIT
    last_beat = 0.0
    while time.time() < deadline:
        if time.time() - last_beat > 30:
            state = _load_state(project_dir)
            if not _anything(_outstanding(state), _unpins(state)):
                return 0
            state["heartbeat"] = last_beat = time.time()
            _save_state(project_dir, state)
        if not is_open_in_kicad(pro):
            # Closed. KiCad saves the project as it closes: wait until the file
            # has stopped changing (two equal looks, ``settle`` apart).
            state = _load_state(project_dir)
            todo, drop = _outstanding(state), _unpins(state)
            if not _anything(todo, drop):
                return 0
            first = _mtime(pro)
            time.sleep(settle)
            if is_open_in_kicad(pro) or _mtime(pro) != first or first < 0:
                continue
            # write_pins verifies the file; if KiCad reopened meanwhile it read
            # the file after our write (the lock is created as it opens).
            if write_pins(pro, todo, drop):
                state = _load_state(project_dir)
                _mark_done(state, todo)
                state["unpin"] = {k: [n for n in state["unpin"][k] if n not in drop[k]] for k in KINDS}
                state["heartbeat"] = 0
                _save_state(project_dir, state)
                logger.info("Pinned %s, unpinned %s in %s", todo, drop, pro.name)
                return 0
        time.sleep(poll)
    state = _load_state(project_dir)
    state["heartbeat"] = 0
    _save_state(project_dir, state)
    return 2


if __name__ == "__main__" and len(sys.argv) >= 3 and sys.argv[1] == "--watch":
    logging.basicConfig(
        filename=str(config_dir() / "pin-helper.log"), level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    try:
        sys.exit(watch(sys.argv[2]))
    except Exception:  # noqa: BLE001
        logging.exception("pin helper failed")
        sys.exit(3)
