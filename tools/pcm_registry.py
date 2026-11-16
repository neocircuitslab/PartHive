#!/usr/bin/env python3
"""Release metadata and the self-hosted KiCad PCM registry for PartHive.

1. Release metadata (attached to the GitHub Release; also what an entry in
   KiCad's official add-on list needs):

       python tools/pcm_registry.py --zip dist/PartHive-1.1.0.zip \\
           --url https://github.com/neocircuitslab/PartHive/releases/download/v1.1.0/PartHive-1.1.0.zip \\
           --out dist/metadata.json

   = metadata.json plus download_url, download_sha256, download_size and
   install_size for this version.

2. Registry update (the files behind https://neocircuitslab.github.io/kicad/repository.json):

       ... --registry <pages checkout>/docs/kicad --pages-url https://neocircuitslab.github.io/kicad

   * packages.json  -- this package's entry is replaced with the new metadata;
     its older versions are kept (their download links still work), other
     packages are left alone;
   * resources.zip  -- <identifier>/icon.png (64x64); other packages' icons kept;
   * repository.json -- new sha256 and time stamps.

PartHive is licensed under GPL-3.0-or-later (see LICENSE).
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = "https://go.kicad.org/pcm/schemas/v1"


def dump(obj) -> bytes:
    return (json.dumps(obj, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def version_key(v: dict):
    """'1.10.0' > '1.9.2'; a pre-release ('1.8.0-rc1') sorts below its release."""
    main, _, pre = v["version"].partition("-")
    nums = [int(n) if n.isdigit() else 0 for n in re.split(r"[.]", main)]
    return nums, pre == "", pre


def release_metadata(zip_path: Path, url: str) -> dict:
    meta = json.loads((ROOT / "metadata.json").read_text(encoding="utf-8"))
    data = zip_path.read_bytes()
    with zipfile.ZipFile(zip_path) as z:
        install_size = sum(i.file_size for i in z.infolist())
    meta["versions"][0].update(
        download_url=url,
        download_sha256=hashlib.sha256(data).hexdigest(),
        download_size=len(data),
        install_size=install_size,
    )
    return meta


def update_registry(reg: Path, pages_url: str, meta: dict, icon: bytes) -> None:
    reg.mkdir(parents=True, exist_ok=True)
    ident = meta["identifier"]
    now = datetime.now(timezone.utc)
    stamp = {"update_time_utc": now.strftime("%Y-%m-%d %H:%M:%S"), "update_timestamp": int(now.timestamp())}

    # packages.json
    pkg_file = reg / "packages.json"
    packages = json.loads(pkg_file.read_text(encoding="utf-8")) if pkg_file.is_file() else {}
    others = [p for p in packages.get("packages", []) if p.get("identifier") != ident]
    old = next((p for p in packages.get("packages", []) if p.get("identifier") == ident), {})
    new_v = meta["versions"][0]
    versions = [v for v in old.get("versions", []) if v.get("version") != new_v["version"]] + [new_v]
    entry = dict(meta, versions=sorted(versions, key=version_key, reverse=True))
    pkg_bytes = dump({"$schema": SCHEMA, "packages": others + [entry]})
    pkg_file.write_bytes(pkg_bytes)

    # resources.zip -- only rewritten when the icon changed
    res_file = reg / "resources.zip"
    entries = {}
    if res_file.is_file():
        with zipfile.ZipFile(res_file) as z:
            entries = {i.filename: z.read(i) for i in z.infolist() if not i.is_dir()}
    name = f"{ident}/icon.png"
    res_changed = entries.get(name) != icon
    if res_changed:
        entries[name] = icon
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for n in sorted(entries):
                z.writestr(n, entries[n])
        res_file.write_bytes(buf.getvalue())

    # repository.json -- keeps its name / maintainer
    repo_file = reg / "repository.json"
    repo = json.loads(repo_file.read_text(encoding="utf-8")) if repo_file.is_file() else {
        "$schema": SCHEMA,
        "name": f"{meta['maintainer']['name']} Repository",
        "maintainer": meta["maintainer"],
    }
    base = pages_url.rstrip("/")
    repo["packages"] = {"url": f"{base}/packages.json", "sha256": hashlib.sha256(pkg_bytes).hexdigest(), **stamp}
    if res_changed or "resources" not in repo:
        repo["resources"] = {
            "url": f"{base}/resources.zip",
            "sha256": hashlib.sha256(res_file.read_bytes()).hexdigest(),
            **stamp,
        }
    repo_file.write_bytes(dump(repo))
    print(f"registry updated: {reg} ({ident} {new_v['version']}, "
          f"{len(entry['versions'])} version(s), icon {'updated' if res_changed else 'unchanged'})")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--zip", required=True, type=Path, help="the built PartHive-<version>.zip")
    ap.add_argument("--url", required=True, help="public download URL of that zip")
    ap.add_argument("--out", type=Path, help="write the release metadata.json here")
    ap.add_argument("--registry", type=Path, help="registry folder to update (…/docs/kicad)")
    ap.add_argument("--pages-url", help="public URL of that folder, e.g. https://neocircuitslab.github.io/kicad")
    args = ap.parse_args()
    if args.registry and not args.pages_url:
        ap.error("--registry needs --pages-url")

    meta = release_metadata(args.zip, args.url)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_bytes(dump(meta))
        print(f"wrote {args.out}")
    if args.registry:
        with zipfile.ZipFile(args.zip) as z:
            icon = z.read("resources/icon.png")  # the same 64x64 icon the package ships
        update_registry(args.registry, args.pages_url, meta, icon)
    return 0


if __name__ == "__main__":
    sys.exit(main())
