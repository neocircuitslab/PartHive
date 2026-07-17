#!/usr/bin/env python3
"""Build a KiCad PCM-compatible package archive from the repo root.

Usage:
    python tools/build_zip.py <repo_root> <output.zip>

Only the PCM package items are included (metadata.json, plugins/, resources/),
so repo-only files (README.md, LICENSE, .gitignore, tools/) are left out. The
archive uses forward-slash paths, as PCM expects.
"""
from __future__ import annotations

import sys
import zipfile
from pathlib import Path

INCLUDE = ["metadata.json", "plugins", "resources"]


def _keep(f: Path) -> bool:
    s = str(f)
    if "__pycache__" in s:
        return False
    if f.suffix in (".pyc", ".parthive-bak", ".backup"):
        return False
    if f.name in ("parthive.log", "plugin.log", "settings.json"):
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
    files = sorted(f for f in files if _keep(f))

    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for f in files:
            z.write(f, f.relative_to(root).as_posix())
    print(f"wrote {out} ({out.stat().st_size} bytes, {len(files)} files)")


if __name__ == "__main__":
    main()
