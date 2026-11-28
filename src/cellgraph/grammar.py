"""The ``# @key value`` directive block at the top of a cell.

One object holds the whole syntax: finding the leading block, reading a directive,
stripping the block, and setting or removing a directive, so editing round-trips
with parsing. The comment marker is the cell language's: ``#`` by default, ``--``
for SQL (``-- @key value``). A second syntax (Quarto's ``#| key: value``) would be
another ``Grammar``.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Container, Iterator
from dataclasses import dataclass

# ``str.splitlines`` also breaks at these, which Python keeps inside a line, a comment
# or a string. Another language may break at one (JavaScript at \u2028), so a block
# line holding one is body: no language reads a directive across it.
_OTHER_BREAKS = re.compile("[\x0b\x0c\x1c-\x1e\x85\u2028\u2029]")


# A line and its own ending, so an edit keeps every line ending as it was.
_LINE = re.compile(r"[^\r\n]*(?:\r\n|\r|\n)|[^\r\n]+\Z")
_BOM = "\ufeff"


def _lines_with_ends(source: str) -> list[tuple[str, str]]:
    """*source*'s lines with their endings: ``("x = 1", "\r\n")``; the last may have none."""
    pairs = []
    for match in _LINE.finditer(source):
        text = match.group()
        line = text.rstrip("\r\n")
        pairs.append((line, text[len(line) :]))
    return pairs


def _without_bom(source: str) -> str:
    """*source* without a leading byte-order mark, which hides the first line's ``#``."""
    return source[1:] if source.startswith(_BOM) else source


def source_lines(source: str) -> list[str]:
    """*source*'s lines, split at ``\n``, ``\r\n`` and ``\r`` as Python splits them."""
    lines = source.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if lines[-1] == "":
        lines.pop()
    return lines


@dataclass(frozen=True)
class Directive:
    """One ``<marker> @key value`` line: its lower-cased key, its value, and its 1-based line."""

    key: str
    value: str
    line: int


def check_marker(marker: object) -> str:
    """*marker* if it can start a comment line; a ``TypeError`` otherwise."""
    if (
        not isinstance(marker, str)
        or not marker
        or "@" in marker
        or any(c.isspace() for c in marker)
    ):
        raise TypeError(
            f"a comment marker must be a non-empty str without `@` or whitespace, not {marker!r}"
        )
    return marker


def _edit_key(key: object) -> str:
    """*key* lower-cased, as an edit writes and finds it."""
    if not isinstance(key, str):
        raise TypeError(f"a directive key must be a str, not {key!r}")
    return key.lower()


class Grammar:
    """The ``<marker> @key value`` syntax of a cell's leading comment block.

    *marker* starts every line of the block (``#`` by default). It replaces ``#``:
    under ``--``, a ``#`` line is body and ends the block.
    """

    # Class-level defaults, so a subclass's own ``directive_re`` and a subclass
    # ``__init__`` that skips ours both keep working as they did in 0.3.0.
    marker: str = "#"
    # Greedy, on a stripped line: a lazy value with a trailing ``\s*`` backtracks in
    # quadratic time over a run of whitespace.
    directive_re: re.Pattern[str] = re.compile(r"^#\s*@(\w+)\s*(.*)$")

    def __init__(self, marker: str = "#") -> None:
        if check_marker(marker) != self.marker:
            self._set_marker(marker)

    def __eq__(self, other: object) -> bool:
        """Equal grammars read and write alike: same class, marker and pattern.

        A subclass with state of its own overrides this.
        """
        if not isinstance(other, Grammar):
            return NotImplemented
        return (type(self), self.marker, self.directive_re.pattern) == (
            type(other),
            other.marker,
            other.directive_re.pattern,
        )

    def __hash__(self) -> int:
        return hash((type(self), self.marker, self.directive_re.pattern))

    def with_marker(self, marker: str) -> Grammar:
        """This grammar for *marker*: itself when it already uses it, else a copy.

        A subclass whose directive pattern is not built from the marker overrides this.
        """
        if marker == self.marker:
            return self
        other = copy.copy(self)
        other._set_marker(marker)
        return other

    def _set_marker(self, marker: str) -> None:
        self.marker = check_marker(marker)
        self.directive_re = re.compile(rf"^{re.escape(marker)}\s*@(\w+)\s*(.*)$")

    def block(self, source: str) -> Iterator[tuple[int, str]]:
        """Yield ``(1-based lineno, raw_line)`` for the leading comment block.

        The block is the first contiguous run of blank lines and lines that start
        with the marker; blank lines are not yielded.
        """
        for lineno, line in enumerate(source_lines(_without_bom(source)), start=1):
            stripped = line.strip()
            if not stripped:
                continue
            if not self._in_block(stripped):
                break
            yield lineno, line

    def reads_key(self, key: str) -> bool:
        """Whether a directive this grammar writes for *key* reads back as *key*: under
        the default pattern ``per-variant`` reads as ``per``, and ``""`` as none."""
        parsed = self.parse_directive(self._format(key, "v"))
        return parsed is not None and parsed[0] == key

    def parse_directive(self, line: str) -> tuple[str, str] | None:
        """Match one ``<marker> @key value`` line: ``(key.lower(), value)``, or ``None``."""
        match = self.directive_re.match(line.strip())
        if not match:
            return None
        return match.group(1).lower(), match.group(2).strip()

    def directives(self, source: str, keys: Container[str] | None = None) -> list[Directive]:
        """Every directive of the leading block, in source order; only *keys* when given."""
        found = []
        for lineno, line in self.block(source):
            parsed = self.parse_directive(line)
            if parsed is not None and (keys is None or parsed[0] in keys):
                found.append(Directive(key=parsed[0], value=parsed[1], line=lineno))
        return found

    def strip(self, source: str) -> str:
        """Return *source* with the leading comment block removed."""
        lines = source_lines(_without_bom(source))
        for i, line in enumerate(lines):
            stripped = line.strip()
            if not stripped or self._in_block(stripped):
                continue
            return "\n".join(lines[i:])
        return ""

    def strip_directives(self, source: str, keys: Container[str] | None = None) -> str:
        """Return *source* without the directive lines of its leading block.

        With *keys*, only those keys' lines go. Other comments stay: they are the
        body's, for its normalizer to judge.
        """
        lines = source_lines(_without_bom(source))
        end = self._block_end(lines)

        def is_directive(line: str) -> bool:
            parsed = self.parse_directive(line)
            return parsed is not None and (keys is None or parsed[0] in keys)

        kept = [line for i, line in enumerate(lines) if i >= end or not is_directive(line)]
        return "\n".join(kept)

    def set_directive(self, source: str, key: str, value: str) -> str:
        """Return *source* with a single ``<marker> @key value`` directive set.

        Replaces the first ``<marker> @key`` in the leading block and drops duplicates; when
        absent, inserts after the last directive (or at the top). Every other line, and
        its line ending, is kept. A *key* this grammar would not read back as itself,
        or a *value* with a line break inside it, is a ``ValueError``: either would
        write another line than one directive.
        """
        key = _edit_key(key)
        if not isinstance(value, str):
            raise TypeError(f"a directive value must be a str, not {value!r}")
        inner = value.strip()  # ``_format`` strips the value, so an outer break is fine
        if "\n" in inner or "\r" in inner or _OTHER_BREAKS.search(inner):
            raise ValueError(f"a directive value must be one line, not {value!r}")
        if not self.reads_key(key):
            raise ValueError(f"{key!r} is not a directive key this grammar reads back")
        new_line = self._format(key, inner)
        bom, body = (_BOM, source[1:]) if source.startswith(_BOM) else ("", source)
        pairs = _lines_with_ends(body)
        end = self._block_end([line for line, _ in pairs])

        matches = [
            i for i in range(end) if (d := self.parse_directive(pairs[i][0])) and d[0] == key
        ]
        if matches:
            first = matches[0]
            pairs[first] = (new_line, pairs[first][1])
            for i in reversed(matches[1:]):
                self._drop(pairs, i)
        else:
            directives = [i for i in range(end) if self.parse_directive(pairs[i][0])]
            at = directives[-1] + 1 if directives else 0
            sep = self._line_sep(body)
            if at == len(pairs):
                if pairs and not pairs[-1][1]:
                    pairs[-1] = (pairs[-1][0], sep)  # the old last line now has one after it
                pairs.append((new_line, sep if body.endswith(("\n", "\r")) else ""))
            else:
                neighbour = pairs[at - 1][1] if at else pairs[at][1]
                pairs.insert(at, (new_line, neighbour or sep))
        result = bom + "".join(line + ending for line, ending in pairs)
        return source if result == source else result

    def remove_directive(self, source: str, key: str) -> str:
        """Return *source* with every ``<marker> @key`` directive removed from the block.

        Every other line keeps its line ending; *source* is returned as it was when it
        has no such directive.
        """
        key = _edit_key(key)
        bom, body = (_BOM, source[1:]) if source.startswith(_BOM) else ("", source)
        pairs = _lines_with_ends(body)
        end = self._block_end([line for line, _ in pairs])
        matches = [
            i for i in range(end) if (d := self.parse_directive(pairs[i][0])) and d[0] == key
        ]
        if not matches:
            return source
        for i in reversed(matches):
            self._drop(pairs, i)
        return bom + "".join(line + ending for line, ending in pairs)

    @staticmethod
    def _drop(pairs: list[tuple[str, str]], index: int) -> None:
        """Remove line *index*; a last line without an ending leaves the new last one so."""
        _, ending = pairs.pop(index)
        if index == len(pairs) and pairs and not ending:
            pairs[-1] = (pairs[-1][0], "")

    def _block_end(self, lines: list[str]) -> int:
        """Index of the first body line: the end of the leading comment block."""
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped and not self._in_block(stripped):
                return i
        return len(lines)

    def _in_block(self, stripped: str) -> bool:
        """Whether a non-blank, stripped line belongs to the leading comment block."""
        return stripped.startswith(self.marker) and not _OTHER_BREAKS.search(stripped)

    def _format(self, key: str, value: str) -> str:
        value = value.strip()
        return f"{self.marker} @{key} {value}" if value else f"{self.marker} @{key}"

    @staticmethod
    def _line_sep(source: str) -> str:
        """The most common line ending, for a line that has no neighbour's to take."""
        endings = [ending for _, ending in _lines_with_ends(source) if ending]
        if not endings:
            return "\n"
        return max(("\n", "\r\n", "\r"), key=endings.count)


GRAMMAR = Grammar()
