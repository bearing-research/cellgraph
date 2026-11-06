"""The language analyzer protocol and its registry.

An analyzer gives a cell's defines and references, and the normalized text its
definition key is built from. ``owns_key`` is False when the host computes the
cell's execution key itself (Strata's SQL and prompt cells); such a cell still
gets names, edges, plannability and plan steps.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from cellgraph.diagnostics import Diagnostic
from cellgraph.grammar import check_marker

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
    # A body that cannot be read becomes the error ``syntax_error``.
    error: str | None = None
    # Anything else the analyzer finds; an ERROR blocks the cell like a syntax error.
    diagnostics: list[Diagnostic] = field(default_factory=list)


class LanguageAnalyzer(Protocol):
    """What ``analyze`` needs of a language.

    An analyzer may also set ``directives``: ``True`` (the default) reads every
    directive, ``False`` none (markdown headings are ``#`` lines), and a frozenset
    of keys only those. A ``# @`` line whose key is not read is plain text: no
    annotation and no diagnostic.

    It may also set ``comment``, the line comment marker its directive block uses:
    ``#`` by default, ``--`` for SQL. The marker replaces ``#`` for that language,
    and is independent of ``directives``.
    """

    owns_key: bool

    def analyze(self, cell: Cell) -> Analyzed:
        """The cell's names; the body only, its directives are the vocabulary's."""
        ...

    def normalize(self, body: str) -> str:
        """The canonical text of a body whose directive lines are already removed."""
        ...


def directive_keys(analyzer: LanguageAnalyzer) -> frozenset[str] | None:
    """The directive keys *analyzer*'s language reads; None for every key."""
    directives = getattr(analyzer, "directives", True)
    if directives is True:
        return None
    if directives is False:
        return frozenset()
    if isinstance(directives, frozenset) and all(isinstance(k, str) for k in directives):
        return frozenset(key.lower() for key in directives)
    raise TypeError(f"`directives` must be a bool or a frozenset of keys, not {directives!r}")


def comment_marker(analyzer: LanguageAnalyzer) -> str:
    """The comment marker of *analyzer*'s directive block; ``#`` when it sets none."""
    return check_marker(getattr(analyzer, "comment", "#"))


def strip_lines(source: str) -> str:
    """A ready-made normalizer: trailing whitespace per line, and outer blank lines, dropped."""
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
