#!/usr/bin/env python3
"""Build a KiCad PCM-compatible package archive from the repo root.

Usage:
    python tools/build_zip.py <repo_root> <output.zip>

Only the PCM package items are included (metadata.json, plugins/, resources/),
so repo-only files (README.md, LICENSE, .gitignore, tools/) are left out. The
archive uses forward-slash paths, as PCM expects.

The EasyEDA->KiCad converter is bundled as a git submodule under
`plugins/easyeda2kicad-ph/`. Only its importable package (`easyeda2kicad_ph/`)
and its AGPL LICENSE/NOTICE are shipped — the fork's packaging/dev files
(pyproject.toml, README, .git, tests, ...) are dropped.
"""
from __future__ import annotations

import sys
import zipfile
from pathlib import Path

INCLUDE = ["metadata.json", "plugins", "resources"]


def _keep(f: Path, root: Path) -> bool:
    rel = f.relative_to(root).as_posix()
    if "__pycache__" in rel:
        return False
    if f.suffix in (".pyc", ".parthive-bak", ".backup"):
        return False
    if f.name in ("parthive.log", "plugin.log", "settings.json"):
        return False
    # Bundled converter submodule: ship ONLY the importable package plus its
    # license/notice; drop the fork repo's packaging/dev files.
    parts = rel.split("/")
    if "easyeda2kicad-ph" in parts:
        sub = parts[parts.index("easyeda2kicad-ph") + 1:]
        if sub[:1] == ["easyeda2kicad_ph"]:
            return True
        if sub in (["LICENSE"], ["NOTICE"]):
            return True
        return False
    return True


def main() -> None:
    root = Path(sys.argv[1]).resolve()
    out = Path(sys.argv[2]).resolve()
    if out.exists():
        out.unlink()

    files: list[Path] = []
    for item in INCLUDE:
        p = root / item
        if p.is_file():
            files.append(p)
        elif p.is_dir():
            files.extend(f for f in p.rglob("*") if f.is_file())
    files = sorted(f for f in files if _keep(f, root))

    if not any("easyeda2kicad_ph" in f.relative_to(root).as_posix() for f in files):
        print(
            "WARNING: converter package 'easyeda2kicad_ph' not found — did you run "
            "'git submodule update --init'? The zip will be missing the converter."
        )

    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for f in files:
            z.write(f, f.relative_to(root).as_posix())
    print(f"wrote {out} ({out.stat().st_size} bytes, {len(files)} files)")


if __name__ == "__main__":
    main()
