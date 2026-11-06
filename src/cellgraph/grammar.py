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


class Grammar:
    """The ``<marker> @key value`` syntax of a cell's leading comment block.

    *marker* starts every line of the block (``#`` by default). It replaces ``#``:
    under ``--``, a ``#`` line is body and ends the block.
    """

    # Class-level defaults, so a subclass's own ``directive_re`` and a subclass
    # ``__init__`` that skips ours both keep working as they did in 0.3.0.
    marker: str = "#"
    directive_re: re.Pattern[str] = re.compile(r"^#\s*@(\w+)\s*(.*?)\s*$")

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
        self.directive_re = re.compile(rf"^{re.escape(marker)}\s*@(\w+)\s*(.*?)\s*$")

    def block(self, source: str) -> Iterator[tuple[int, str]]:
        """Yield ``(1-based lineno, raw_line)`` for the leading comment block.

        The block is the first contiguous run of blank lines and lines that start
        with the marker; blank lines are not yielded.
        """
        for lineno, line in enumerate(source_lines(source), start=1):
            stripped = line.strip()
            if not stripped:
                continue
            if not self._in_block(stripped):
                break
            yield lineno, line

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
        lines = source_lines(source)
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
        lines = source_lines(source)
        end = self._block_end(lines)

        def is_directive(line: str) -> bool:
            parsed = self.parse_directive(line)
            return parsed is not None and (keys is None or parsed[0] in keys)

        kept = [line for i, line in enumerate(lines) if i >= end or not is_directive(line)]
        return "\n".join(kept)

    def set_directive(self, source: str, key: str, value: str) -> str:
        """Return *source* with a single ``<marker> @key value`` directive set.

        Replaces the first ``<marker> @key`` in the leading block and drops duplicates; when
        absent, inserts after the last directive (or at the top).
        """
        key = key.lower()
        new_line = self._format(key, value)
        sep = self._line_sep(source)
        lines = source_lines(source)
        end = self._block_end(lines)

        matches = [i for i in range(end) if (d := self.parse_directive(lines[i])) and d[0] == key]
        if matches:
            lines[matches[0]] = new_line
            for i in reversed(matches[1:]):
                del lines[i]
        else:
            directives = [i for i in range(end) if self.parse_directive(lines[i])]
            lines.insert(directives[-1] + 1 if directives else 0, new_line)

        result = sep.join(lines)
        return result + sep if source.endswith(("\n", "\r")) else result

    def remove_directive(self, source: str, key: str) -> str:
        """Return *source* with every ``<marker> @key`` directive removed from the block."""
        key = key.lower()
        sep = self._line_sep(source)
        lines = source_lines(source)
        end = self._block_end(lines)
        kept = [
            line
            for i, line in enumerate(lines)
            if not (i < end and (d := self.parse_directive(line)) is not None and d[0] == key)
        ]
        result = sep.join(kept)
        return result + sep if source.endswith(("\n", "\r")) else result

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
        """The dominant line ending, so a splice preserves CRLF, CR or LF."""
        if "\r\n" in source:
            return "\r\n"
        return "\r" if "\r" in source else "\n"


GRAMMAR = Grammar()
