"""The ``# @key value`` directive block at the top of a cell.

One object holds the whole syntax: finding the leading block, reading a directive,
stripping the block, and setting or removing a directive, so editing round-trips
with parsing. A second syntax (Quarto's ``#| key: value``) would be another
``Grammar``.
"""

from __future__ import annotations

import re
from collections.abc import Container, Iterator
from dataclasses import dataclass


@dataclass(frozen=True)
class Directive:
    """One ``# @key value`` line: its lower-cased key, its value, and its 1-based line."""

    key: str
    value: str
    line: int


class Grammar:
    """The ``# @key value`` syntax of a cell's leading comment block."""

    directive_re = re.compile(r"^#\s*@(\w+)\s*(.*?)\s*$")

    def block(self, source: str) -> Iterator[tuple[int, str]]:
        """Yield ``(1-based lineno, raw_line)`` for the leading comment block.

        The block is the first contiguous run of blank and ``#`` lines; blank lines
        are not yielded.
        """
        for lineno, line in enumerate(source.splitlines(), start=1):
            stripped = line.strip()
            if not stripped:
                continue
            if not stripped.startswith("#"):
                break
            yield lineno, line

    def parse_directive(self, line: str) -> tuple[str, str] | None:
        """Match one ``# @key value`` line and return ``(key.lower(), value)``, or ``None``."""
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
        lines = source.splitlines()
        for i, line in enumerate(lines):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            return "\n".join(lines[i:])
        return ""

    def strip_directives(self, source: str, keys: Container[str] | None = None) -> str:
        """Return *source* without the directive lines of its leading block.

        With *keys*, only those keys' lines go. Other comments stay: they are the
        body's, for its normalizer to judge.
        """
        lines = source.splitlines()
        end = self._block_end(lines)

        def is_directive(line: str) -> bool:
            parsed = self.parse_directive(line)
            return parsed is not None and (keys is None or parsed[0] in keys)

        kept = [line for i, line in enumerate(lines) if i >= end or not is_directive(line)]
        return "\n".join(kept)

    def set_directive(self, source: str, key: str, value: str) -> str:
        """Return *source* with a single ``# @key value`` directive set.

        Replaces the first ``# @key`` in the leading block and drops duplicates; when
        absent, inserts after the last directive (or at the top).
        """
        key = key.lower()
        new_line = self._format(key, value)
        sep = self._line_sep(source)
        lines = source.splitlines()
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
        return result + sep if source.endswith("\n") else result

    def remove_directive(self, source: str, key: str) -> str:
        """Return *source* with every ``# @key`` directive removed from the block."""
        key = key.lower()
        sep = self._line_sep(source)
        lines = source.splitlines()
        end = self._block_end(lines)
        kept = [
            line
            for i, line in enumerate(lines)
            if not (i < end and (d := self.parse_directive(line)) is not None and d[0] == key)
        ]
        result = sep.join(kept)
        return result + sep if source.endswith("\n") else result

    @staticmethod
    def _block_end(lines: list[str]) -> int:
        """Index of the first body line: the end of the leading comment block."""
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                return i
        return len(lines)

    @staticmethod
    def _format(key: str, value: str) -> str:
        value = value.strip()
        return f"# @{key} {value}" if value else f"# @{key}"

    @staticmethod
    def _line_sep(source: str) -> str:
        """The dominant line ending, so a splice preserves CRLF vs LF."""
        return "\r\n" if "\r\n" in source else "\n"


GRAMMAR = Grammar()
