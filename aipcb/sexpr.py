"""Minimal S-expression parser / writer for KiCad files.

Parsed form: lists for "(...)", `Sym` for bare atoms, `str` for quoted strings,
and int/float for numbers.  This keeps quoted-vs-bare distinctions so a file can
be round-tripped faithfully.
"""
from __future__ import annotations

import re
from typing import Any, Iterable


class Sym(str):
    """A bare (unquoted) atom such as `yes`, `smd`, `F.Cu`."""

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Sym({str.__repr__(self)})"


_TOKEN = re.compile(r'\s*(?:(\()|(\))|"((?:[^"\\]|\\.)*)"|([^\s()"]+))', re.S)
_NUM = re.compile(r"^[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?$")


def _unescape(s: str) -> str:
    return s.replace('\\"', '"').replace("\\n", "\n").replace("\\\\", "\\")


def parse(text: str) -> list:
    stack: list[list] = [[]]
    pos = 0
    n = len(text)
    while pos < n:
        m = _TOKEN.match(text, pos)
        if not m:
            if text[pos:].strip() == "":
                break
            raise ValueError(f"sexpr parse error at {pos}: {text[pos:pos+40]!r}")
        pos = m.end()
        if m.group(1):
            stack.append([])
        elif m.group(2):
            done = stack.pop()
            stack[-1].append(done)
        elif m.group(3) is not None:
            stack[-1].append(_unescape(m.group(3)))
        else:
            tok = m.group(4)
            if _NUM.match(tok):
                stack[-1].append(float(tok) if any(c in tok for c in ".eE") else int(tok))
            else:
                stack[-1].append(Sym(tok))
    if len(stack) != 1 or len(stack[0]) != 1:
        raise ValueError("unbalanced s-expression")
    return stack[0][0]


def parse_file(path) -> list:
    with open(path, encoding="utf-8") as f:
        return parse(f.read())


def _fmt_atom(a: Any) -> str:
    if isinstance(a, Sym):
        return str(a)
    if isinstance(a, bool):
        return "yes" if a else "no"
    if isinstance(a, int):
        return str(a)
    if isinstance(a, float):
        s = f"{a:.6f}".rstrip("0").rstrip(".")
        return "0" if s in ("-0", "") else s
    if isinstance(a, str):
        return '"' + a.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'
    raise TypeError(f"cannot format {a!r}")


def dumps(node: Any, indent: int = 0) -> str:
    if not isinstance(node, list):
        return _fmt_atom(node)
    if not node:
        return "()"
    simple = all(not isinstance(x, list) for x in node)
    if simple:
        return "(" + " ".join(_fmt_atom(x) for x in node) + ")"
    tab = "\t" * indent
    out = ["(" + _fmt_atom(node[0])]
    i = 1
    while i < len(node) and not isinstance(node[i], list):
        out[0] += " " + _fmt_atom(node[i])
        i += 1
    for child in node[i:]:
        out.append("\n" + tab + "\t" + dumps(child, indent + 1))
    out.append("\n" + tab + ")")
    return "".join(out)


# ----------------------------------------------------------------- helpers
def head(node) -> str | None:
    return str(node[0]) if isinstance(node, list) and node and isinstance(node[0], str) else None


def find(node: list, name: str) -> list | None:
    for c in node:
        if head(c) == name:
            return c
    return None


def find_all(node: list, name: str) -> Iterable[list]:
    return (c for c in node if head(c) == name)


def walk(node, name: str):
    """Recursively yield every sub-list whose head is `name`."""
    if isinstance(node, list):
        if head(node) == name:
            yield node
        for c in node:
            if isinstance(c, list):
                yield from walk(c, name)


def value(node: list, name: str, default=None):
    c = find(node, name)
    return c[1] if c is not None and len(c) > 1 else default


def prop(node: list, key: str, default=None):
    for c in find_all(node, "property"):
        if len(c) > 2 and c[1] == key:
            return c[2]
    return default
