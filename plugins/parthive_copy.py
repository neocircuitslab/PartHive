"""PartHive – copy parts from another project's (or a global) library.

A part = a symbol, the footprint its ``Footprint`` field points at, and the 3D
model(s) that footprint uses. Copying it into the Target libraries also
re-points the links: the symbol's ``Footprint`` field to the target footprint
library, and the footprint's ``(model ...)`` path to the target 3D folder.
The source is only read, never changed.

PartHive is licensed under GPL-3.0-or-later (see LICENSE).
"""

from __future__ import annotations

import json
import logging
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from parthive_config import projects_dir, write_text_atomic
from parthive_lib_table import _field, expand_uri, find_settings_dir, table_entries
from parthive_sexpr import blocks, field as sx_field, head_string, prop, set_prop

logger = logging.getLogger("parthive.copy")

LCSC_FIELDS = ("LCSC Part #", "LCSC Part", "LCSC")
_KICAD_STD_VAR = re.compile(r"\$\{KICAD\d*_3DMODEL_DIR\}", re.IGNORECASE)


# --------------------------------------------------------------------------
# Finding projects and libraries
# --------------------------------------------------------------------------
def known_projects(exclude: Path | str | None = None) -> list:
    """Project folders worth offering as a source, newest first: KiCad's
    recent-projects list, then every project PartHive has remembered."""
    out, seen = [], set()

    def add(pro_or_dir: str) -> None:
        p = Path(pro_or_dir)
        folder = p.parent if p.suffix == ".kicad_pro" else p
        key = str(folder).casefold()
        if key in seen or not folder.is_dir() or not any(folder.glob("*.kicad_pro")):
            return
        if exclude and key == str(Path(exclude)).casefold():
            return
        seen.add(key)
        out.append(folder)

    settings = find_settings_dir()
    try:
        data = json.loads((settings / "kicad.json").read_text(encoding="utf-8")) if settings else {}
        for f in (data.get("system") or {}).get("file_history") or []:
            add(f)
    except (OSError, ValueError):
        pass
    try:
        for f in sorted(projects_dir().glob("*.json"), key=lambda p: -p.stat().st_mtime):
            if f.name.endswith(".pins.json"):
                continue
            try:
                add(json.loads(f.read_text(encoding="utf-8")).get("project_dir", ""))
            except (OSError, ValueError):
                continue
    except OSError:
        pass
    return out


def table_libs(table: Path, project_dir: Path | None = None) -> list:
    """``[(nickname, path, descr)]`` of a table's own entries (no nested
    ``Table`` entries — KiCad's built-in libraries are not offered)."""
    try:
        content = Path(table).read_text(encoding="utf-8")
    except OSError:
        return []
    out = []
    for chunk in re.split(r"\(\s*lib\s", content)[1:]:
        name, uri = _field(chunk, "name"), _field(chunk, "uri")
        if not name or not uri or _field(chunk, "type").lower() == "table":
            continue
        out.append((name, Path(expand_uri(uri, project_dir)), _field(chunk, "descr")))
    return out


@dataclass
class Source:
    """A symbol library to copy from, plus where to look up its footprints."""

    label: str                       # shown in the dialog
    nickname: str
    sym_path: Path
    project_dir: Path | None = None  # for ${KIPRJMOD}
    fp_tables: list = field(default_factory=list)  # [(table, project_dir)]


def sources_for_project(project_dir: Path) -> list:
    pdir = Path(project_dir)
    fp_tables = [(pdir / "fp-lib-table", pdir)]
    settings = find_settings_dir()
    if settings is not None:
        fp_tables.append((settings / "fp-lib-table", None))
    return [Source(f"{nick} (project {pdir.name})", nick, path, pdir, fp_tables)
            for nick, path, _d in table_libs(pdir / "sym-lib-table", pdir)
            if path.suffix == ".kicad_sym" and path.is_file()]


def global_sources() -> list:
    settings = find_settings_dir()
    if settings is None:
        return []
    fp_tables = [(settings / "fp-lib-table", None)]
    return [Source(f"{nick} (global)", nick, path, None, fp_tables)
            for nick, path, _d in table_libs(settings / "sym-lib-table")
            if path.suffix == ".kicad_sym" and path.is_file()]


# --------------------------------------------------------------------------
# Reading parts
# --------------------------------------------------------------------------
@dataclass
class Part:
    name: str
    block: str
    footprint: str = ""   # "Lib:FP" as written in the symbol
    lcsc: str = ""
    extends: str = ""     # parent symbol (derived symbols)


def read_parts(sym_path: Path) -> list:
    try:
        text = Path(sym_path).read_text(encoding="utf-8")
    except OSError:
        return []
    parts = []
    for s, e in blocks(text, "symbol", 1):
        block = text[s:e]
        parts.append(Part(
            name=head_string(block),
            block=block,
            footprint=prop(block, "Footprint") or "",
            lcsc=next((v for v in (prop(block, f) for f in LCSC_FIELDS) if v), ""),
            extends=sx_field(block, "extends") or "",
        ))
    return parts


def lib_version(sym_path: Path) -> str:
    try:
        return sx_field(Path(sym_path).read_text(encoding="utf-8")[:400], "version") or "20211014"
    except OSError:
        return "20211014"


def find_footprint(ref: str, fp_tables: list) -> Path | None:
    """``Lib:FP`` -> the ``.kicad_mod`` file, through the given tables."""
    if ":" not in ref:
        return None
    nick, name = ref.split(":", 1)
    for table, pdir in fp_tables:
        folder = table_entries(table, pdir).get(nick)
        if folder:
            f = Path(folder) / f"{name}.kicad_mod"
            if f.is_file():
                return f
    return None


_MODEL_RE = re.compile(r'(\(\s*model\s+)("(?:[^"\\]|\\.)*"|[^\s()]+)')


# --------------------------------------------------------------------------
# Copying
# --------------------------------------------------------------------------
@dataclass
class CopyReport:
    symbols: list = field(default_factory=list)
    footprints: list = field(default_factory=list)
    models: list = field(default_factory=list)
    skipped: list = field(default_factory=list)
    problems: list = field(default_factory=list)


def _append_symbols(target: Path, new_blocks: dict, version: str, overwrite: bool) -> list:
    """Add/replace ``{name: block}`` in the target .kicad_sym. Returns the
    names written."""
    if target.is_file():
        text = target.read_text(encoding="utf-8")
    else:
        text = f'(kicad_symbol_lib\n\t(version {version})\n\t(generator "PartHive")\n)\n'
    existing = {head_string(text[s:e]): (s, e) for s, e in blocks(text, "symbol", 1)}
    written = []
    for name, block in new_blocks.items():
        if name in existing:
            if not overwrite:
                continue
            s, e = existing[name]
            text = text[:s] + block + text[e:]
            existing = {head_string(text[a:b]): (a, b) for a, b in blocks(text, "symbol", 1)}
        else:
            close = text.rstrip().rfind(")")
            text = text[:close].rstrip() + "\n\t" + block + "\n" + text[close:]
        written.append(name)
    if written:
        target.parent.mkdir(parents=True, exist_ok=True)
        write_text_atomic(target, text)
    return written


def copy_parts(parts: list, source: Source, importer, overwrite: bool = False) -> CopyReport:
    """Copy ``parts`` (from ``source``) into the importer's target libraries.

    ``importer`` is a ComponentImporter for the Target libraries: it supplies
    the target paths/names and writes paths the same way an import does.
    """
    rep = CopyReport()
    by_name = {p.name: p for p in read_parts(source.sym_path)}
    want = {}
    for part in parts:  # derived symbols need their parent next to them
        want[part.name] = part
        parent = by_name.get(part.extends) if part.extends else None
        if parent is not None:
            want.setdefault(parent.name, parent)

    importer.footprint_dir.mkdir(parents=True, exist_ok=True)
    blocks_out = {}
    for part in want.values():
        block = part.block
        if part.footprint and ":" in part.footprint:
            fp_name = part.footprint.split(":", 1)[1]
            target_mod = importer.footprint_dir / f"{fp_name}.kicad_mod"
            src_mod = find_footprint(part.footprint, source.fp_tables)
            if target_mod.exists() and not overwrite:
                rep.skipped.append(f"footprint {fp_name} (already there)")
                block = set_prop(block, "Footprint", f"{importer.fp_name}:{fp_name}")
            elif src_mod is None:
                rep.problems.append(f"{part.name}: footprint {part.footprint} not found — link kept as is")
            else:
                text = _copy_models(src_mod.read_text(encoding="utf-8"), source, importer, rep)
                write_text_atomic(target_mod, text)
                rep.footprints.append(fp_name)
                block = set_prop(block, "Footprint", f"{importer.fp_name}:{fp_name}")
        blocks_out[part.name] = block
    written = _append_symbols(importer.symbol_lib, blocks_out, lib_version(source.sym_path), overwrite)
    rep.symbols.extend(written)
    rep.skipped.extend(f"symbol {n} (already there)" for n in blocks_out if n not in written)
    return rep


def _copy_models(mod_text: str, source: Source, importer, rep: CopyReport) -> str:
    def repl(m: re.Match) -> str:
        raw = m.group(2).strip('"')
        if _KICAD_STD_VAR.search(raw):
            return m.group(0)  # KiCad's own 3D library: same on every machine
        src = Path(expand_uri(raw, source.project_dir))
        if not src.is_file():
            rep.problems.append(f"3D model {src.name} not found — path kept as is")
            return m.group(0)
        importer.model_dir.mkdir(parents=True, exist_ok=True)
        dest = importer.model_dir / src.name
        if not dest.exists():
            shutil.copy2(src, dest)
            rep.models.append(src.name)
        return f'{m.group(1)}"{importer._path_ref(importer.model_dir)}/{src.name}"'
    return _MODEL_RE.sub(repl, mod_text)
