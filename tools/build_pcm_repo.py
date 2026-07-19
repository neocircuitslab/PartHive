#!/usr/bin/env python3
"""Generate a self-hosted KiCad PCM repository into ``docs/`` (for GitHub Pages).

Usage:
    python tools/build_pcm_repo.py <repo_root> <zip_path> <github_user> <repo_name> <tag>

Example:
    python tools/build_pcm_repo.py . PartHive-1.0.0.zip neocircuitslab PartHive v1.0.0

Produces docs/{repository.json, packages.json, resources.zip}. Users add
`https://<github_user>.github.io/<repo_name>/repository.json` in KiCad's
Plugin & Content Manager. Re-run after each new GitHub Release.
"""
from __future__ import annotations

import hashlib
import json
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = "https://go.kicad.org/pcm/schemas/v1"


def sha256_file(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def main() -> None:
    root = Path(sys.argv[1]).resolve()
    zip_path = Path(sys.argv[2]).resolve()
    user, repo, tag = sys.argv[3], sys.argv[4], sys.argv[5]

    pages_base = f"https://{user}.github.io/{repo}"
    dl_url = f"https://github.com/{user}/{repo}/releases/download/{tag}/{zip_path.name}"

    docs = root / "docs"
    docs.mkdir(parents=True, exist_ok=True)

    # Base metadata from the repo; add download_* to the version entry.
    meta = json.loads((root / "metadata.json").read_text(encoding="utf-8"))
    ident = meta["identifier"]
    with zipfile.ZipFile(zip_path) as zf:
        install_size = sum(i.file_size for i in zf.infolist())
    v = meta["versions"][0]
    v["download_url"] = dl_url
    v["download_sha256"] = sha256_file(zip_path)
    v["download_size"] = zip_path.stat().st_size
    v["install_size"] = install_size

    # packages.json
    packages_bytes = (json.dumps({"$schema": SCHEMA, "packages": [meta]}, indent=2) + "\n").encode("utf-8")
    (docs / "packages.json").write_bytes(packages_bytes)

    # resources.zip — <identifier>/icon.png at 64x64
    try:
        from PIL import Image

        tmp = docs / "_icon64.png"
        Image.open(root / "plugins" / "icon.png").resize((64, 64), Image.LANCZOS).save(tmp)
        with zipfile.ZipFile(docs / "resources.zip", "w", zipfile.ZIP_DEFLATED) as z:
            z.write(tmp, f"{ident}/icon.png")
        tmp.unlink()
    except Exception as exc:  # Pillow optional
        print("warning: skipped resources.zip icon:", exc)

    now = datetime.now(timezone.utc)
    stamp = {"update_time_utc": now.strftime("%Y-%m-%d %H:%M:%S"), "update_timestamp": int(now.timestamp())}
    repository = {
        "$schema": SCHEMA,
        "name": f"{repo} Repository",
        "maintainer": {"name": user, "contact": {"web": f"https://github.com/{user}/{repo}"}},
        "packages": {"url": f"{pages_base}/packages.json", "sha256": hashlib.sha256(packages_bytes).hexdigest(), **stamp},
    }
    res = docs / "resources.zip"
    if res.exists():
        repository["resources"] = {"url": f"{pages_base}/resources.zip", "sha256": sha256_file(res), **stamp}
    (docs / "repository.json").write_text(json.dumps(repository, indent=2) + "\n", encoding="utf-8")

    print("wrote", docs)
    print("repository URL for users:", f"{pages_base}/repository.json")


if __name__ == "__main__":
    main()
