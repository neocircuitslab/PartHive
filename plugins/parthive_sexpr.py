"""PartHive – small helpers for KiCad's S-expression files.

Just enough to find and edit blocks such as ``(symbol "Name" ...)`` in
``.kicad_sym`` / ``.kicad_sch`` files without disturbing anything else: the
text outside the edited blocks is kept byte-for-byte.

PartHive is licensed under GPL-3.0-or-later (see LICENSE).
"""

from __future__ import annotations

import re


def blocks(text: str, token: str, depth: int, start: int = 0, end: int | None = None) -> list:
    """``[(start, end)]`` of every ``(token ...)`` whose ``(`` sits at nesting
    ``depth`` (the file's root expression opens at depth 0, its children at 1)."""
    end = len(text) if end is None else end
    spans, stack = [], []
    level, i, in_quote = 0, start, False
    pat = re.compile(r"\(\s*" + re.escape(token) + r"(?=[\s()])")
    while i < end:
        ch = text[i]
        if in_quote:
            if ch == "\\":
                i += 2
                continue
            if ch == '"':
                in_quote = False
        elif ch == '"':
            in_quote = True
        elif ch == "(":
            if level == depth and pat.match(text, i):
                stack.append((i, level))
            level += 1
        elif ch == ")":
            level -= 1
            if stack and stack[-1][1] == level:
                s, _lvl = stack.pop()
                spans.append((s, i + 1))
        i += 1
    return spans


_STR = r'(?:"((?:[^"\\]|\\.)*)"|([^\s()"]+))'


def head_string(block: str) -> str:
    """The first string after the token: ``(symbol "R_10k" ...)`` -> ``R_10k``."""
    m = re.match(r"\(\s*[^\s()]+\s+" + _STR, block)
    if not m:
        return ""
    return m.group(1) if m.group(1) is not None else (m.group(2) or "")


def field(block: str, key: str) -> str | None:
    """Value of the first ``(key "value")`` inside ``block``."""
    m = re.search(r"\(\s*" + re.escape(key) + r"\s+" + _STR, block)
    if not m:
        return None
    return m.group(1) if m.group(1) is not None else m.group(2)


def prop(block: str, name: str) -> str | None:
    """Value of ``(property "name" "value" ...)`` inside ``block``."""
    m = re.search(r'\(\s*property\s+"' + re.escape(name) + r'"\s+"((?:[^"\\]|\\.)*)"', block)
    return m.group(1) if m else None


def set_prop(block: str, name: str, value: str) -> str:
    """``block`` with ``(property "name" ...)``'s value replaced (if present)."""
    pat = re.compile(r'(\(\s*property\s+"' + re.escape(name) + r'"\s+")((?:[^"\\]|\\.)*)(")')
    return pat.sub(lambda m: m.group(1) + quote(value) + m.group(3), block, count=1)


def set_head_string(block: str, value: str) -> str:
    """``(symbol "Old" ...`` -> ``(symbol "value" ...``."""
    return re.sub(r'^(\(\s*[^\s()]+\s+)' + _STR,
                  lambda m: m.group(1) + '"' + quote(value) + '"', block, count=1)


def quote(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')
