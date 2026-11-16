"""PartHive – fix symbols that link to libraries the project doesn't have.

Typical cause: a circuit copied from another project. Its symbols still say
``OtherLib:AO3400A`` and ``OtherLib:SOT-23-3_...``; ERC then warns that the
library isn't found. PartHive can:

* **copy**    the used parts from the project/library they came from into the
              Target libraries, then re-link the symbols to them;
* **relink**  to a part this project already has with the same LCSC Part #;
* **recover** the symbol from the copy every schematic keeps of it
              (``lib_symbols``) when no source library exists.

Re-linking rewrites the ``.kicad_sch`` files (only while the schematic is
closed — KiCad would overwrite edits made behind its back) and the open board's
footprint links through KiCad's own API.

PartHive is licensed under GPL-3.0-or-later (see LICENSE).
"""

from __future__ import annotations

import logging
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

from parthive_config import write_text_atomic
from parthive_copy import LCSC_FIELDS, Part, Source, read_parts, sources_for_project, global_sources
from parthive_lib_table import find_settings_dir, table_entries
from parthive_sexpr import blocks, field as sx_field, head_string, prop, set_head_string, set_prop

logger = logging.getLogger("parthive.fixlinks")

FIX_COPY, FIX_RELINK, FIX_RECOVER, FIX_NONE = "copy", "relink", "recover", "none"


@dataclass
class BrokenRef:
    sheet: Path
    reference: str
    lib_id: str          # "Lib:Name"
    cache_key: str       # key of its copy in lib_symbols
    footprint: str
    lcsc: str


@dataclass
class MissingLib:
    nickname: str
    refs: list = field(default_factory=list)
    source: Source | None = None          # where the library still exists
    relink: dict = field(default_factory=dict)   # lib_id -> (Lib:Name, footprint) by LCSC
    fix: str = FIX_NONE

    def options(self) -> list:
        out = []
        if self.source is not None:
            out.append(FIX_COPY)
        if self.relink and len(self.relink) == len({r.lib_id for r in self.refs}):
            out.append(FIX_RELINK)
        out.append(FIX_RECOVER)
        out.append(FIX_NONE)
        return out


def sheets(project_dir: Path) -> list:
    return [p for p in sorted(Path(project_dir).rglob("*.kicad_sch"))
            if not p.name.startswith("~") and ".parthive-bak" not in p.name]


def open_sheets(project_dir: Path) -> list:
    """Schematic files KiCad has open (it holds ~<name>.kicad_sch.lck)."""
    return [s for s in sheets(project_dir) if s.with_name(f"~{s.name}.lck").exists()]


def visible_nicknames(kind: str, project_dir: Path) -> set:
    fname = "sym-lib-table" if kind == "sym" else "fp-lib-table"
    names = set()
    settings = find_settings_dir()
    if settings is not None:
        names |= set(table_entries(settings / fname))
    names |= set(table_entries(Path(project_dir) / fname, Path(project_dir)))
    return names


def scan(project_dir: Path, other_projects: list) -> dict:
    """``{missing nickname: MissingLib}`` for the project's schematic."""
    pdir = Path(project_dir)
    visible = visible_nicknames("sym", pdir)
    missing: dict = {}
    for sheet in sheets(pdir):
        try:
            text = sheet.read_text(encoding="utf-8")
        except OSError:
            continue
        for s, e in blocks(text, "symbol", 1):
            block = text[s:e]
            lib_id = sx_field(block, "lib_id") or ""
            if ":" not in lib_id:
                continue
            nick = lib_id.split(":", 1)[0]
            if nick in visible:
                continue
            ref = BrokenRef(
                sheet=sheet, reference=prop(block, "Reference") or "?", lib_id=lib_id,
                cache_key=sx_field(block, "lib_name") or lib_id,
                footprint=prop(block, "Footprint") or "",
                lcsc=next((v for v in (prop(block, f) for f in LCSC_FIELDS) if v), ""),
            )
            missing.setdefault(nick, MissingLib(nick)).refs.append(ref)
    if not missing:
        return {}
    # Where do these libraries still exist? (other projects, then global)
    pools = [src for p in other_projects for src in sources_for_project(p)] + global_sources()
    for nick, ml in missing.items():
        ml.source = next((src for src in pools if src.nickname == nick), None)
    # Same parts already reachable here (by LCSC Part #)?
    index = lcsc_index(pdir)
    for ml in missing.values():
        for r in ml.refs:
            if r.lcsc and r.lcsc in index:
                ml.relink[r.lib_id] = index[r.lcsc]
        ml.fix = ml.options()[0]
    return missing


def lcsc_index(project_dir: Path) -> dict:
    """``{LCSC Part #: ("Lib:Name", footprint)}`` over this project's libraries
    and the global libraries PartHive added."""
    from parthive_copy import table_libs

    pdir = Path(project_dir)
    libs = [(n, p) for n, p, _d in table_libs(pdir / "sym-lib-table", pdir)]
    settings = find_settings_dir()
    if settings is not None:
        libs += [(n, p) for n, p, d in table_libs(settings / "sym-lib-table") if "PartHive" in (d or "")]
    index = {}
    for nick, path in libs:
        if path.suffix != ".kicad_sym":
            continue
        for part in read_parts(path):
            if part.lcsc and part.lcsc not in index:
                index[part.lcsc] = (f"{nick}:{part.name}", part.footprint)
    return index


def cached_symbol(project_dir: Path, ref: BrokenRef) -> str | None:
    """The schematic's own copy of a symbol, as a library symbol block."""
    try:
        text = ref.sheet.read_text(encoding="utf-8")
    except OSError:
        return None
    for ls, le in blocks(text, "lib_symbols", 1):
        for s, e in blocks(text, "symbol", 1, ls, le):  # depth counts from ls
            block = text[s:e]
            if head_string(block) == ref.cache_key:
                name = ref.lib_id.split(":", 1)[1]
                return set_head_string(block, name)
    return None


# --------------------------------------------------------------------------
# Applying
# --------------------------------------------------------------------------
def relink_schematic(project_dir: Path, plan: dict, lib_blocks: dict) -> int:
    """Rewrite the sheets. ``plan``: ``{old lib_id: (new lib_id, new footprint
    or None)}``. ``lib_blocks``: ``{new lib_id: library symbol block}`` — used
    to refresh the schematic's own copy so ERC sees it matches the library.
    Returns the number of symbols changed. Backs up each changed sheet."""
    changed = 0
    for sheet in sheets(project_dir):
        text = sheet.read_text(encoding="utf-8")
        new = text
        # placed symbols (depth 1)
        for s, e in reversed(blocks(new, "symbol", 1)):
            block = new[s:e]
            lib_id = sx_field(block, "lib_id") or ""
            if lib_id not in plan:
                continue
            new_id, new_fp = plan[lib_id]
            nb = block.replace(f'(lib_id "{lib_id}")', f'(lib_id "{new_id}")', 1)
            if new_fp:
                nb = set_prop(nb, "Footprint", new_fp)
            if '(lib_name "' in nb:  # the copy is now keyed by the new lib_id
                nb = _drop_lib_name(nb)
            new = new[:s] + nb + new[e:]
            changed += 1
        # the schematic's own copies (lib_symbols, depth 2)
        for ls, le in blocks(new, "lib_symbols", 1):
            spans = blocks(new, "symbol", 1, ls, le)  # depth counts from ls
            keys = {head_string(new[s:e]) for s, e in spans}
            for s, e in reversed(spans):
                key = head_string(new[s:e])
                target = plan.get(key, (None, None))[0]
                if not target:
                    continue
                if target in keys and target != key:
                    new = new[:s] + new[e:].lstrip(" \t").lstrip("\r\n")  # copy already there
                    continue
                lib_block = lib_blocks.get(target)
                repl = (set_head_string(lib_block, target) if lib_block
                        else set_head_string(new[s:e], target))
                new = new[:s] + repl + new[e:]
                keys.add(target)
            break
        if new != text:
            backup = sheet.with_name(sheet.name + ".parthive-bak")
            shutil.copy2(sheet, backup)
            write_text_atomic(sheet, new)
    return changed


def _drop_lib_name(block: str) -> str:
    import re
    return re.sub(r'\s*\(lib_name\s+"(?:[^"\\]|\\.)*"\)', "", block, count=1)


def relink_board(fp_map: dict) -> int:
    """Re-link footprints on the board open in KiCad (SWIG only). ``fp_map``:
    ``{old "Lib:FP": new "Lib:FP"}``. Returns how many were changed (the user
    saves the board)."""
    pcbnew = sys.modules.get("pcbnew")
    if pcbnew is None or not fp_map:
        return 0
    try:
        board = pcbnew.GetBoard()
        n = 0
        for fp in board.GetFootprints():
            old = str(fp.GetFPIDAsString())
            if old in fp_map:
                fp.SetFPIDAsString(fp_map[old])
                n += 1
        if n:
            pcbnew.Refresh()
        return n
    except Exception:  # noqa: BLE001
        logger.exception("board relink")
        return 0


def _block_for(lib_id: str, project_dir: Path) -> tuple:
    """(library symbol block, footprint) of ``Lib:Name`` reachable from the project."""
    if ":" not in lib_id:
        return None, None
    nick, name = lib_id.split(":", 1)
    pdir = Path(project_dir)
    tables = [(pdir / "sym-lib-table", pdir)]
    settings = find_settings_dir()
    if settings is not None:
        tables.append((settings / "sym-lib-table", None))
    for table, pd in tables:
        path = table_entries(table, pd).get(nick)
        if path and Path(path).suffix == ".kicad_sym":
            for part in read_parts(Path(path)):
                if part.name == name:
                    return part.block, part.footprint
    return None, None


def apply_fixes(project_dir: Path, missing: dict, importer) -> tuple:
    """Carry out each MissingLib's chosen ``fix`` into the importer's Target
    libraries. Returns (summary lines, symbols changed, board footprints changed).
    The caller must make sure the schematic is closed first."""
    from parthive_copy import _append_symbols, copy_parts, lib_version

    pdir = Path(project_dir)
    lines, plan, lib_blocks, fp_map = [], {}, {}, {}
    target_parts = lambda: {p.name: p for p in read_parts(importer.symbol_lib)}  # noqa: E731
    for nick, ml in missing.items():
        names = sorted({r.lib_id.split(":", 1)[1] for r in ml.refs})
        if ml.fix == FIX_COPY and ml.source is not None:
            wanted = [p for p in read_parts(ml.source.sym_path) if p.name in names]
            rep = copy_parts(wanted, ml.source, importer)
            lines.append(f"{nick}: copied {len(rep.symbols)} symbol(s), {len(rep.footprints)} "
                         f"footprint(s), {len(rep.models)} 3D model(s) into '{importer.sym_name}'.")
            lines += [f"  {p}" for p in rep.problems]
            have = target_parts()
            for r in ml.refs:
                name = r.lib_id.split(":", 1)[1]
                part = have.get(name)
                if part is None:
                    continue
                new_id = f"{importer.sym_name}:{name}"
                plan[r.lib_id] = plan[r.cache_key] = (new_id, part.footprint or None)
                lib_blocks[new_id] = part.block
                if r.footprint and part.footprint and r.footprint != part.footprint:
                    fp_map[r.footprint] = part.footprint
        elif ml.fix == FIX_RELINK:
            for r in ml.refs:
                target = ml.relink.get(r.lib_id)
                if not target:
                    continue
                new_id, new_fp = target
                plan[r.lib_id] = plan[r.cache_key] = (new_id, new_fp or None)
                block, _fp = _block_for(new_id, pdir)
                if block:
                    lib_blocks[new_id] = block
                if r.footprint and new_fp and r.footprint != new_fp:
                    fp_map[r.footprint] = new_fp
            lines.append(f"{nick}: re-linked to parts this project already has (same LCSC Part #).")
        elif ml.fix == FIX_RECOVER:
            recovered = {}
            for r in ml.refs:
                name = r.lib_id.split(":", 1)[1]
                if name not in recovered:
                    block = cached_symbol(pdir, r)
                    if block:
                        # the library symbol's own Footprint field follows too
                        fp_name = r.footprint.split(":", 1)[1] if ":" in r.footprint else ""
                        if fp_name and (importer.footprint_dir / f"{fp_name}.kicad_mod").is_file():
                            block = set_prop(block, "Footprint", f"{importer.fp_name}:{fp_name}")
                        recovered[name] = block
            version = lib_version(importer.symbol_lib) if importer.symbol_lib.is_file() else "20241209"
            _append_symbols(importer.symbol_lib, recovered, version, overwrite=False)
            have = target_parts()
            kept = 0
            for r in ml.refs:
                name = r.lib_id.split(":", 1)[1]
                if name not in have:
                    continue
                new_id = f"{importer.sym_name}:{name}"
                # Footprint: re-link when the Target footprint library has one of
                # that name; otherwise keep the field (reported below).
                new_fp = None
                if ":" in r.footprint:
                    fp_name = r.footprint.split(":", 1)[1]
                    if (importer.footprint_dir / f"{fp_name}.kicad_mod").is_file():
                        new_fp = f"{importer.fp_name}:{fp_name}"
                        fp_map[r.footprint] = new_fp
                    else:
                        kept += 1
                plan[r.lib_id] = plan[r.cache_key] = (new_id, new_fp)
                lib_blocks[new_id] = have[name].block
            lines.append(f"{nick}: recovered {len(recovered)} symbol(s) from the schematic into "
                         f"'{importer.sym_name}'.")
            if kept:
                lines.append(f"  {kept} footprint link(s) kept as they were — no footprint of that "
                             "name in the Target footprint library; assign them in KiCad.")
    changed = relink_schematic(pdir, plan, lib_blocks) if plan else 0
    board = relink_board(fp_map)
    if importer.config.auto_register:
        importer._register_libraries()
    return lines, changed, board
