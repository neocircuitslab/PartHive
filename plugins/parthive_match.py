"""PartHive – suggest existing footprints that match an imported one.

EasyEDA names footprints like ``SOT-23-3_L2.9-W1.3-P1.90-LS2.4-BR``; your
library may already hold ``SOT-23``, ``SOT-23-3`` or ``SOT-23-5``. PartHive
suggests the nearest ones. It never links anything by itself — the user picks.

Matching is strict about the package prefix:

* the *package* is the name up to the first ``_`` / space / ``(`` / ``,``
  (``SOT-23-3_L2.9…`` -> ``SOT-23-3``), compared case-insensitively;
* it is split at ``-`` into tokens (``SOT`` ``23`` ``3``), and two packages only
  match when they share leading tokens **whole** — so ``SOT-23`` matches
  ``SOT-23-3`` and ``SOT-23-5``, but ``SOT-223`` never matches ``SOT-23``, and
  ``R0603`` never matches ``C0603``.

Levels (shown as a light-yellow / yellow / deep-yellow warning):

1. same package, same pad count              (``SOT-23-3``  for ``SOT-23-3_…``)
2. one package is a prefix of the other, same pad count (``SOT-23`` with 3 pads)
3. same package family, different pad count  (``SOT-23-5``) — check before use

PartHive is licensed under GPL-3.0-or-later (see LICENSE).
"""

from __future__ import annotations

import re
from pathlib import Path

LEVEL_SAME = 1        # light yellow
LEVEL_PREFIX = 2      # yellow
LEVEL_FAMILY = 3      # deep yellow
MAX_SUGGESTIONS = 3

REASON = {
    LEVEL_SAME: "Same package and pad count — only the size details in the name differ.",
    LEVEL_PREFIX: "The package name shares the prefix and has the same pad count.",
    LEVEL_FAMILY: "Same package family but a different pad count — check it before linking.",
}

_PAD_RE = re.compile(r'\(\s*pad\s+(?:"((?:[^"\\]|\\.)*)"|([^\s()]+))')

# Package words that name the same family (thickness / power / vendor variants).
# A match through an alias is never "the same package" — at best yellow.
_ALIASES = {
    "DFN": {"DFN", "PDFN", "VDFN", "WDFN", "UDFN", "XDFN", "TDFN", "HDFN",
            "SON", "VSON", "WSON", "USON", "XSON", "TSON"},
    "QFN": {"QFN", "VQFN", "WQFN", "UQFN", "TQFN", "HQFN", "HVQFN", "LQFN", "DQFN"},
    "SOIC": {"SOIC", "SOP"},
}
_CANON = {word: root for root, words in _ALIASES.items() for word in words}
ALIAS_REASON = ("A different name for the same package family ({a} / {b}) — the "
                "footprints may well be the same, but check before linking.")


def package_of(name: str) -> str:
    """``SOT-23-3_L2.9-W1.3…`` -> ``SOT-23-3`` (upper case)."""
    return re.split(r"[_\s(,]", (name or "").strip(), maxsplit=1)[0].upper()


def _tokens(package: str) -> list:
    return [t for t in package.split("-") if t]


def footprint_pad_count(path: Path) -> int | None:
    """Distinct numbered pads in a ``.kicad_mod`` (None if unreadable)."""
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    numbers = {m.group(1) if m.group(1) is not None else m.group(2) for m in _PAD_RE.finditer(text)}
    numbers.discard("")
    return len(numbers)


def ee_pad_count(ee_footprint) -> int | None:
    """Distinct numbered pads of an EasyEDA footprint."""
    pads = getattr(ee_footprint, "pads", None)
    if pads is None:
        return None
    numbers = {str(getattr(p, "number", "") or "").strip() for p in pads}
    numbers.discard("")
    return len(numbers)


def match_level(new_name: str, new_pads: int | None, other_name: str,
                other_pads: int | None) -> int | None:
    """How well ``other_name`` matches ``new_name`` (a LEVEL_*), or None."""
    a, b = _tokens(package_of(new_name)), _tokens(package_of(other_name))
    if not a or not b:
        return None
    alias = a[0] != b[0] and _CANON.get(a[0], a[0]) == _CANON.get(b[0], b[0])
    if alias:
        # compare the rest as if the family word were the same (PDFN-8 ~ DFN-8)
        b = [a[0]] + b[1:]
    level = _level(a, b, new_pads, other_pads)
    if level is not None and alias:
        level = max(level, LEVEL_PREFIX)  # an alias is never "the same package"
    return level


def is_alias_match(new_name: str, other_name: str) -> bool:
    a, b = _tokens(package_of(new_name)), _tokens(package_of(other_name))
    return bool(a and b) and a[0] != b[0] and _CANON.get(a[0], a[0]) == _CANON.get(b[0], b[0])


def reason(new_name: str, other_name: str, level: int) -> str:
    """Hover text for a suggestion."""
    if is_alias_match(new_name, other_name):
        return ALIAS_REASON.format(a=_tokens(package_of(new_name))[0],
                                   b=_tokens(package_of(other_name))[0])
    return REASON[level]


def _level(a: list, b: list, new_pads: int | None, other_pads: int | None) -> int | None:
    common = 0
    for x, y in zip(a, b):
        if x != y:
            break
        common += 1
    if common == 0:
        return None
    pads_differ = new_pads is not None and other_pads is not None and new_pads != other_pads
    if a == b:
        return LEVEL_FAMILY if pads_differ else LEVEL_SAME
    if common == min(len(a), len(b)):  # one package is a whole-token prefix of the other
        return LEVEL_FAMILY if pads_differ else LEVEL_PREFIX
    # same family, different variant (SOT-23-3 vs SOT-23-5): the family must be
    # more than a bare word ("SOT"), unless the package is just "<word>-<pins>".
    if common >= 2 or (len(a) == 2 and len(b) == 2 and a[1].isdigit() and b[1].isdigit()):
        return LEVEL_FAMILY
    return None


def suggest(new_name: str, new_pads: int | None, footprint_dir: Path,
            limit: int = MAX_SUGGESTIONS) -> list:
    """Nearest existing footprints in ``footprint_dir``:
    ``[(name, level), ...]`` best first; the identically named one is left out
    (that case is "exists" -> Skip / Overwrite already)."""
    folder = Path(footprint_dir)
    if not folder.is_dir():
        return []
    scored = []
    for mod in folder.glob("*.kicad_mod"):
        name = mod.stem
        if name.casefold() == (new_name or "").casefold():
            continue
        pads = footprint_pad_count(mod)
        level = match_level(new_name, new_pads, name, pads)
        if level is None:
            continue
        # Nearest first: colour level, a real (non-alias) name, the nearest pad
        # count, then the nearest package name length.
        pad_gap = abs(pads - new_pads) if (pads is not None and new_pads is not None) else 0
        closeness = abs(len(package_of(name)) - len(package_of(new_name)))
        alias = is_alias_match(new_name, name)
        scored.append((level, alias, pad_gap, closeness, name.casefold(), name))
    scored.sort()
    return [(row[-1], row[0]) for row in scored[:limit]]
