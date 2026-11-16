"""PartHive – footprint sanitiser & validator.

The EasyEDA converter emits some strings *unquoted* inside ``.kicad_mod`` files
(footprint name, ``fp_text`` values, ``descr``/``tags``, layer names, pad
numbers). KiCad's s-expression parser then stops at the first embedded space:

    (fp_text user DATE CODE (at ...) (layer F.SilkS) ...)
                 ^^^^^^^^^ two tokens, not one string

and an unquoted name containing brackets splits into stray elements:

    (module easyeda2kicad:CRYSTAL-SMD_4P (3.2x2.5) (layer F.Cu) ...)
                                         ^^^^^^^^^ looks like a child element

KiCad answers with "Unable to load library" for the *whole* ``.pretty``
directory, so one bad footprint makes every footprint in that library disappear
from the Footprint Editor. The same happens when a footprint is renamed to
something containing a space or brackets.

This module repairs a freshly exported footprint and then validates it, so
PartHive only ever leaves loadable files on disk:

    sanitize_footprint_file(path, name) -> (changed, notes)
    validate_footprint_file(path, name) -> list of problems (empty == valid)
    repair_footprint_dir(dir, log)      -> (checked, repaired)

PartHive is licensed under GPL-3.0-or-later (see LICENSE).
"""

from __future__ import annotations

import re
from pathlib import Path

# Oldest "modern" footprint format (KiCad 6). Declaring the oldest one keeps the
# file readable by every KiCad PartHive supports (7 … 10); newer KiCads upgrade
# it in memory and on their next save.
FP_VERSION = "20211014"
GENERATOR = "PartHive"

_QUOTABLE_TEXT = ("reference", "value", "user")

# Elements that may legitimately follow the footprint name. Used to find where an
# unquoted name ends, even when the name itself contains "(...)".
_CHILD_TAGS = (
    "layer", "layers", "version", "generator", "generator_version", "tedit",
    "descr", "tags", "attr", "property", "pad", "model", "zone_connect",
    "solder_mask_margin", "solder_paste_margin", "solder_paste_margin_ratio",
    "solder_paste_ratio", "clearance", "thermal_width", "thermal_gap",
    "autoplace_cost90", "autoplace_cost180", "net_tie_pad_groups",
    "private_layers", "footprint_type", "embedded_fonts", "uuid", "tstamp",
    "at", "path", "locked", "placed", "group", "sheetname", "sheetfile",
    "allow_missing_courtyard", "duplicate_pad_numbers_are_jumpers",
    "jumper_pad_groups", "teardrops", "zone", "dimension", "target", "image",
)
_CHILD_RE = re.compile(
    r"\(\s*(?:fp_[a-z_]+|" + "|".join(_CHILD_TAGS) + r")[\s()]"
)


# --------------------------------------------------------------------------- #
# tokenizer / parser (used for validation)
# --------------------------------------------------------------------------- #


class Sym(str):
    """An unquoted atom (bare symbol/number), as opposed to a quoted string."""


def parse_sexpr(text: str):
    """Parse one s-expression. Returns nested lists; quoted strings become ``str``
    and bare atoms become ``Sym``. Raises ValueError when malformed."""
    i, n = 0, len(text)
    stack: list = []
    root = None
    while i < n:
        c = text[i]
        if c in " \t\r\n":
            i += 1
        elif c == "(":
            lst: list = []
            if stack:
                stack[-1].append(lst)
            elif root is not None:
                raise ValueError("more than one root expression")
            stack.append(lst)
            if root is None:
                root = lst
            i += 1
        elif c == ")":
            if not stack:
                raise ValueError("unbalanced ')'")
            stack.pop()
            i += 1
        elif c == '"':
            j, buf = i + 1, []
            while j < n and text[j] != '"':
                if text[j] == "\\" and j + 1 < n:
                    buf.append(text[j + 1])
                    j += 2
                else:
                    buf.append(text[j])
                    j += 1
            if j >= n:
                raise ValueError("unterminated string")
            if not stack:
                raise ValueError("string outside any list")
            stack[-1].append("".join(buf))
            i = j + 1
        else:
            j = i
            while j < n and text[j] not in ' \t\r\n()"':
                j += 1
            if not stack:
                raise ValueError("atom outside any list")
            stack[-1].append(Sym(text[i:j]))
            i = j
    if stack:
        raise ValueError("unbalanced '(' - %d list(s) left open" % len(stack))
    if root is None:
        raise ValueError("empty file")
    return root


def _list_end(text: str, open_idx: int) -> int:
    """Index just past the ')' matching the '(' at ``open_idx`` (quote-aware)."""
    depth, i, n = 0, open_idx, len(text)
    while i < n:
        c = text[i]
        if c == '"':
            i += 1
            while i < n and text[i] != '"':
                i += 2 if text[i] == "\\" else 1
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return n


# --------------------------------------------------------------------------- #
# sanitiser
# --------------------------------------------------------------------------- #


def _q(value: str) -> str:
    """Quote a KiCad string value."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _quote_layer_list(match: "re.Match") -> str:
    inner = match.group(2).strip()
    if not inner:
        return match.group(0)
    parts = [tok if tok.startswith('"') else _q(tok) for tok in inner.split()]
    return "(" + match.group(1) + " " + " ".join(parts) + ")"


def _quote_free_text(text: str, notes: list) -> str:
    """Quote unquoted ``(descr ...)`` / ``(tags ...)`` bodies, even when the body
    itself contains brackets."""
    out = text
    for tag in ("descr", "tags"):
        pos = 0
        pattern = re.compile(r"\(\s*" + tag + r"\s")
        while True:
            m = pattern.search(out, pos)
            if not m:
                break
            start = m.start()
            end = _list_end(out, start)
            inner = out[m.end():end - 1].strip()
            if inner.startswith('"') or not inner:
                pos = end
                continue
            replacement = "(" + tag + " " + _q(inner) + ")"
            out = out[:start] + replacement + out[end:]
            notes.append("quoted (%s ...)" % tag)
            pos = start + len(replacement)
    return out


def _drop_stray_children(text: str, notes: list) -> str:
    """Remove direct children of the footprint whose tag is not a keyword.

    Such elements are always debris from an unquoted name/value that got split,
    e.g. the "(3.2x2.5)" left behind by an earlier repair of
    ``CRYSTAL-SMD_4P (3.2x2.5)``. Every real footprint element starts with a
    letter, so this cannot delete legitimate content.
    """
    m = re.match(r'\s*\(\s*(?:module|footprint)\s+"(?:[^"\\]|\\.)*"', text)
    if not m:
        return text
    out, i = text, m.end()
    while i < len(out):
        c = out[i]
        if c in " \t\r\n":
            i += 1
            continue
        if c != "(":
            break
        end = _list_end(out, i)
        inner = out[i + 1:end - 1].lstrip()
        tok = re.match(r'[^\s()"]+', inner)
        if tok and not re.match(r"^[A-Za-z_]", tok.group(0)):
            notes.append("dropped stray element %r" % tok.group(0))
            j = end
            while j < len(out) and out[j] in " \t":
                j += 1
            out = out[:i] + out[j:]
            continue
        i = end
    return out


def sanitize_footprint_text(text: str, name: str) -> "tuple[str, list]":
    """Return (sanitised_text, notes) for one ``.kicad_mod`` body.

    ``name`` is authoritative (normally the target file's stem), so a renamed
    footprint always gets a matching, properly quoted internal name — including
    names with spaces and/or brackets such as ``CRYSTAL-SMD_4P (3.2x2.5)``.

    The file's *root* token is deliberately preserved. A legacy ``(module ...)``
    footprint can contain legacy-only constructs — e.g. two-point arcs with
    ``(angle ...)`` — that the modern ``(footprint ...)`` parser rejects, and
    rewriting that geometry could silently distort shapes. KiCad reads both
    roots, so we repair only the quoting (which is what actually breaks the
    parse) and leave format modernisation to KiCad itself.
    """
    notes: list = []
    out = text

    # 1) header -> (<root> "NAME"
    m = re.match(r"(\s*\(\s*)(module|footprint)\s+", out)
    if not m:
        notes.append("could not find the footprint header")
        return out, notes
    root_tok = m.group(2)
    after = m.end()

    if out[after:after + 1] == '"':
        # Already a quoted name: replace it wholesale.
        j = after + 1
        while j < len(out) and out[j] != '"':
            j += 2 if out[j] == "\\" else 1
        raw_name, rest_at = out[after:j + 1], j + 1
    else:
        # Unquoted: the name runs until the first *known* child element, so a
        # bracketed name like "CRYSTAL-SMD_4P (3.2x2.5)" is not cut in half and
        # its tail cannot leak out as a stray "(3.2x2.5)" element.
        child = _CHILD_RE.search(out, after)
        stop = child.start() if child else out.find("(", after)
        if stop < 0:
            stop = len(out)
        raw_name, rest_at = out[after:stop].strip(), stop

    if raw_name != _q(name):
        notes.append("set footprint name to %r (was %r)" % (name, raw_name.strip()))
    out = m.group(1) + root_tok + " " + _q(name) + "\n\t" + out[rest_at:].lstrip()

    # 1b) remove debris left by a previous partial repair (or by EasyEDA), e.g.
    #     a stray "(3.2x2.5)" that used to be part of the name.
    out = _drop_stray_children(out, notes)

    # 2) modern root only: ensure version/generator exist. A legacy (module ...)
    #    must NOT get them - they are not part of that format.
    if root_tok == "footprint":
        head = out[:400]
        inject = ""
        if not re.search(r"\(version\s", head):
            inject += "(version " + FP_VERSION + ")\n\t"
        if not re.search(r"\(generator\s", head):
            inject += '(generator "' + GENERATOR + '")\n\t'
        if inject:
            cut = out.index("\n\t") + 2
            out = out[:cut] + inject + out[cut:]
            notes.append("added version/generator")

    # 3) quote fp_text values: (fp_text user DATE CODE (at ...) -> "DATE CODE".
    #    A value may contain brackets too, e.g. 4P (3.2x2.5).
    def fix_text(mt: "re.Match") -> str:
        kind, raw = mt.group(1), mt.group(2).strip()
        if raw.startswith('"'):
            return mt.group(0)
        notes.append("quoted %s text %r" % (kind, raw))
        return "(fp_text " + kind + " " + _q(raw) + " (at"

    out = re.sub(
        r"\(fp_text\s+(" + "|".join(_QUOTABLE_TEXT) + r")\s+(.*?)\s*\(at\b",
        fix_text, out, flags=re.S,
    )

    # 4) quote descr / tags (free text, often with spaces and brackets)
    out = _quote_free_text(out, notes)

    # 5) quote layer names: (layer F.SilkS) / (layers F.Cu F.Paste F.Mask)
    before = out
    out = re.sub(r"\((layer|layers)\s+([^()\"]*?)\)", _quote_layer_list, out)
    if out != before:
        notes.append("quoted layer names")

    # 6) quote pad numbers: (pad 1 smd rect -> (pad "1" smd rect
    before = out
    out = re.sub(
        r"\(pad\s+(?!\")([^\s()]+)\s+(smd|thru_hole|np_thru_hole|connect)\b",
        lambda mt: "(pad " + _q(mt.group(1)) + " " + mt.group(2), out,
    )
    if out != before:
        notes.append("quoted pad numbers")

    return out, notes


# --------------------------------------------------------------------------- #
# validator
# --------------------------------------------------------------------------- #


BLOCKING = "block"      # KiCad cannot load the file (and so nor can its library)
COSMETIC = "cosmetic"   # loads fine; only tidier if fixed


def _validate(text: str, name: str) -> list:
    """Return [(severity, message), ...]."""
    problems: list = []
    try:
        root = parse_sexpr(text)
    except ValueError as exc:
        return [(BLOCKING, "malformed s-expression: %s" % exc)]

    if not root or str(root[0]) not in ("footprint", "module"):
        return [(BLOCKING, "root element is not (footprint ...) or (module ...)")]
    if len(root) < 2:
        return [(BLOCKING, "footprint has no name")]

    fp_name = root[1]
    if isinstance(fp_name, Sym):
        # A bare single-token name still loads; it is just not how KiCad writes it.
        problems.append((COSMETIC, "footprint name %r is not quoted" % str(fp_name)))
    elif name and fp_name != name:
        # KiCad identifies footprints by file name, so a mismatch is harmless.
        problems.append(
            (COSMETIC, "internal name %r != file name %r" % (fp_name, name))
        )

    # A direct child whose tag does not look like a keyword is almost always a
    # fragment of an unquoted name/value, e.g. a stray "(3.2x2.5)".
    for item in root[2:]:
        if isinstance(item, list) and item:
            tag = str(item[0])
            if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", tag):
                problems.append((
                    BLOCKING,
                    "stray element %r - leftover from an unquoted name or value?" % tag,
                ))

    def walk(node):
        for item in node:
            if isinstance(item, list) and item:
                tag = str(item[0])
                if tag == "fp_text":
                    if len(item) < 4:
                        problems.append(
                            (BLOCKING, "fp_text is missing its value or position")
                        )
                    else:
                        if isinstance(item[2], Sym):
                            problems.append((
                                BLOCKING,
                                "fp_text %s value %r is not quoted"
                                % (str(item[1]), str(item[2])),
                            ))
                        if not (isinstance(item[3], list) and str(item[3][0]) == "at"):
                            problems.append((
                                BLOCKING,
                                "fp_text %s has stray tokens before (at ...) - an "
                                "unquoted value containing spaces?" % str(item[1]),
                            ))
                elif tag in ("descr", "tags"):
                    if len(item) > 2:
                        problems.append((
                            BLOCKING,
                            "(%s ...) has stray tokens - unquoted free text?" % tag,
                        ))
                walk(item)

    walk(root)
    # NB: a footprint with no pads is perfectly valid (graphical/antenna outlines,
    # fiducials, logos), so pad count is deliberately not checked.
    return problems


def validate_footprint_text(text: str, name: str) -> list:
    """All problems found, most serious first (messages only)."""
    found = _validate(text, name)
    return [msg for sev, msg in found if sev == BLOCKING] + \
           [msg for sev, msg in found if sev != BLOCKING]


def blocking_problems_text(text: str, name: str) -> list:
    """Only the problems that stop KiCad loading the footprint/library."""
    return [msg for sev, msg in _validate(text, name) if sev == BLOCKING]


# --------------------------------------------------------------------------- #
# file-level helpers
# --------------------------------------------------------------------------- #


def sanitize_footprint_file(path, name=None) -> "tuple[bool, list]":
    """Sanitise ``path`` in place. Returns (changed, notes)."""
    path = Path(path)
    name = name or path.stem
    original = path.read_text(encoding="utf-8")
    fixed, notes = sanitize_footprint_text(original, name)
    if fixed != original:
        path.write_text(fixed, encoding="utf-8")
        return True, notes
    return False, notes


def validate_footprint_file(path, name=None) -> list:
    path = Path(path)
    name = name or path.stem
    try:
        return validate_footprint_text(path.read_text(encoding="utf-8"), name)
    except OSError as exc:
        return ["could not read the footprint: %s" % exc]


def blocking_problems_file(path, name=None) -> list:
    path = Path(path)
    name = name or path.stem
    try:
        return blocking_problems_text(path.read_text(encoding="utf-8"), name)
    except OSError as exc:
        return ["could not read the footprint: %s" % exc]


def scan_footprint_dir(directory) -> "tuple[int, list]":
    """Read-only check of a ``.pretty`` directory.

    Returns (checked, [(file_name, first_blocking_problem), ...]). Nothing is
    written — PartHive only ever rewrites the footprint it is importing, so other
    footprints are reported and left for the user to decide about.
    """
    directory = Path(directory)
    checked, broken = 0, []
    if not directory.is_dir():
        return 0, []
    for path in sorted(directory.glob("*.kicad_mod")):
        checked += 1
        try:
            problems = blocking_problems_file(path)
            if problems:
                broken.append((path.name, problems[0]))
        except Exception as exc:  # noqa: BLE001
            broken.append((path.name, "could not be checked: %s" % exc))
    return checked, broken


def repair_footprint_dir(directory, log=None) -> "tuple[int, int]":
    """Validate every ``.kicad_mod`` in a ``.pretty`` directory and repair the
    broken ones. Returns (checked, repaired).

    NOT called during an import: PartHive rewrites only the footprint it imports.
    This is for an explicit, user-initiated repair (see the "Check library" button),
    because KiCad refuses to load an entire library when a single footprint in it is
    malformed. Only quoting/naming is touched - never geometry.

    Files that KiCad can already load are left completely untouched, even if they
    are not written exactly the way KiCad would (e.g. an unquoted single-word name
    or an internal name that differs from the file name): needless rewrites would
    only create churn in the user's library.
    """
    directory = Path(directory)
    checked = repaired = 0
    if not directory.is_dir():
        return 0, 0
    for path in sorted(directory.glob("*.kicad_mod")):
        checked += 1
        try:
            problems = blocking_problems_file(path)
            if not problems:
                continue
            changed, _notes = sanitize_footprint_file(path)
            still = blocking_problems_file(path)
            if changed and not still:
                repaired += 1
                if log:
                    log("Repaired footprint for KiCad: %s" % path.name)
            elif still and log:
                log("Footprint still looks invalid: %s (%s)" % (path.name, still[0]))
        except Exception as exc:  # noqa: BLE001
            if log:
                log("Could not check %s (%s)" % (path.name, exc))
    return checked, repaired
