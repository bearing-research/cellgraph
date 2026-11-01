"""The language analyzer protocol and its registry (design 7.1).

An analyzer gives a cell's defines and references, and the normalized text its
definition key is built from. ``owns_key`` is False when the host computes the
cell's execution key itself (Strata's SQL and prompt cells); such a cell still
gets names, edges, plannability and plan steps.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from cellgraph.analysis import Cell


@dataclass(frozen=True)
class Analyzed:
    """One cell's names. ``error`` is a body that could not be read (a syntax error)."""

    defines: list[str] = field(default_factory=list)
    references: list[str] = field(default_factory=list)
    # Free names that shadow a builtin (``input``, ``type``): kept apart from
    # ``references`` but resolved against upstream producers the same way.
    builtin_references: list[str] = field(default_factory=list)
    # Names the body mutates in place (``df["c"] = ...``): read and defined.
    mutation_defines: list[str] = field(default_factory=list)
    error: str | None = None


class LanguageAnalyzer(Protocol):
    """What ``analyze`` needs of a language.

    An analyzer may also set ``directives = False`` for a language whose ``# @``
    lines are not directives (markdown headings): its cells get no annotations.
    """

    owns_key: bool

    def analyze(self, cell: Cell) -> Analyzed:
        """The cell's names; the body only, its directives are the vocabulary's."""
        ...

    def normalize(self, body: str) -> str:
        """The canonical text of a body whose directive lines are already removed."""
        ...


def reads_directives(analyzer: LanguageAnalyzer) -> bool:
    """Whether *analyzer*'s language has a directive block (``directives``, default True)."""
    return bool(getattr(analyzer, "directives", True))


def strip_lines(source: str) -> str:
    """The default normalizer: trailing whitespace per line, and outer blank lines, dropped."""
    lines = [line.rstrip() for line in source.splitlines()]
    return "\n".join(lines).strip()


class UnknownLanguageError(LookupError):
    """A cell's language has no analyzer, which would drop every reference silently."""


_REGISTRY: dict[str, LanguageAnalyzer] = {}


def register_language_analyzer(language: str, analyzer: LanguageAnalyzer) -> None:
    """Bind *analyzer* to *language*; a later registration replaces an earlier one."""
    _REGISTRY[language] = analyzer


def get_language_analyzer(language: str) -> LanguageAnalyzer:
    try:
        return _REGISTRY[language]
    except KeyError as exc:
        raise UnknownLanguageError(f"no language analyzer registered for {language!r}") from exc


def registered_analyzers() -> dict[str, LanguageAnalyzer]:
    """A copy of the registry."""
    return dict(_REGISTRY)
