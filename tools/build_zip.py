#!/usr/bin/env python3
"""Build the KiCad Plugin & Content Manager (PCM) package for PartHive.

Usage (from the repository root, any Python 3.8+):

    python tools/build_zip.py [--tag v1.1.0] [--out dist]

Writes ``dist/PartHive-<version>.zip`` with the layout KiCad's PCM expects:

    metadata.json         (no download_* keys -- those go only in the registry)
    plugins/              (the plugin; the converter submodule reduced to its
                           importable package plus its LICENSE / NOTICE)
    resources/icon.png    (64x64; scaled with Pillow when it is installed)

Before building it checks, and stops on any problem:

* the version is the same in metadata.json, plugins/parthive_paths.py and the
  git tag (when ``--tag`` is given);
* ``plugins/easyeda2kicad-ph`` is a git submodule (a gitlink), not plain files,
  and it is checked out (``git submodule update --init``).

GitHub Actions runs this on every push and on every ``v*`` tag
(.github/workflows/release.yml).

PartHive is licensed under GPL-3.0-or-later (see LICENSE).
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLUGINS = ROOT / "plugins"
CONVERTER = "easyeda2kicad-ph"
DOWNLOAD_KEYS = ("download_url", "download_sha256", "download_size", "install_size")


def fail(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def keep(rel: str) -> bool:
    """Whether a file under the repository root goes into the zip."""
    parts = rel.split("/")
    if "__pycache__" in parts or rel.endswith((".pyc", ".pyo", ".parthive-bak", ".backup", ".log", ".tmp")):
        return False
    if parts[-1] in ("settings.json", ".gitignore", ".gitattributes", ".DS_Store", "Thumbs.db"):
        return False
    if CONVERTER in parts:
        # The fork's packaging, tests and docs stay out; only the package and
        # its AGPL license / notice are shipped.
        sub = parts[parts.index(CONVERTER) + 1:]
        return sub[:1] == ["easyeda2kicad_ph"] or sub in (["LICENSE"], ["NOTICE"])
    return True


def check_versions(meta: dict, tag: str | None) -> str:
    versions = meta.get("versions") or []
    if not versions:
        fail("metadata.json has no versions[] entry")
    version = versions[0]["version"]
    extra = [k for k in DOWNLOAD_KEYS if k in versions[0]]
    if extra:
        fail(f"metadata.json must not contain {', '.join(extra)} (the release step adds them)")

    m = re.search(r'^VERSION\s*=\s*"([^"]+)"', (PLUGINS / "parthive_paths.py").read_text(encoding="utf-8"), re.M)
    if not m or m.group(1) != version:
        fail(f"version mismatch: plugins/parthive_paths.py={m.group(1) if m else '?'} metadata.json={version}")
    if tag is not None and tag.lstrip("vV") != version:
        fail(f"version mismatch: tag {tag} but metadata.json says {version}")
    return version


def check_converter() -> None:
    sub = PLUGINS / CONVERTER
    try:
        staged = subprocess.run(
            ["git", "ls-files", "-s", f"plugins/{CONVERTER}"],
            cwd=ROOT, capture_output=True, text=True, check=True,
        ).stdout.splitlines()
    except (OSError, subprocess.CalledProcessError):
        staged = None  # not a git checkout (e.g. a source zip) -- skip this check
    if staged is not None:
        if len(staged) != 1 or not staged[0].startswith("160000 "):
            fail(
                f"plugins/{CONVERTER} is committed as plain files, not as a submodule.\n"
                "       Fix (then commit and push):\n"
                "         git rm -r --cached plugins/easyeda2kicad-ph\n"
                "         delete the folder plugins/easyeda2kicad-ph\n"
                "         git submodule add --force https://github.com/neocircuitslab/easyeda2kicad-ph plugins/easyeda2kicad-ph"
            )
    if not (sub / "easyeda2kicad_ph" / "__init__.py").is_file():
        fail(f"plugins/{CONVERTER} is empty -- run: git submodule update --init --recursive")


def icon_64() -> bytes:
    src = ROOT / "resources" / "icon.png"
    if not src.is_file():
        fail("resources/icon.png is missing")
    try:
        from PIL import Image
    except ImportError:
        print("note: Pillow not installed -- resources/icon.png is shipped unscaled")
        return src.read_bytes()
    with Image.open(src) as im:
        im = im.convert("RGBA")
        if im.size != (64, 64):
            im = im.resize((64, 64), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, "PNG", optimize=True)
        return buf.getvalue()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--tag", help="git tag being released, e.g. v1.1.0 (must match the version)")
    ap.add_argument("--out", default=str(ROOT / "dist"), help="output folder (default: dist)")
    args = ap.parse_args()

    meta = json.loads((ROOT / "metadata.json").read_text(encoding="utf-8"))
    version = check_versions(meta, args.tag)
    check_converter()

    # ".git" also catches the submodule's .git *file*
    files = sorted(
        p for p in PLUGINS.rglob("*")
        if p.is_file() and ".git" not in p.relative_to(ROOT).parts and keep(p.relative_to(ROOT).as_posix())
    )

    out_dir = Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{meta['name']}-{version}.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("metadata.json", json.dumps(meta, indent=2, ensure_ascii=False) + "\n")
        z.writestr("resources/icon.png", icon_64())
        for p in files:
            z.write(p, p.relative_to(ROOT).as_posix())

    print(f"version {version}")
    print(f"wrote {out} ({out.stat().st_size} bytes, {len(files)} plugin files)")
    gh_out = os.environ.get("GITHUB_OUTPUT")
    if gh_out:  # hand the results to the next workflow steps
        with open(gh_out, "a", encoding="utf-8") as f:
            f.write(f"version={version}\nzip={out}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
