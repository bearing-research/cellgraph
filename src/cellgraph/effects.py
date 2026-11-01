"""The ten effects an annotation can have on the graph (design 4.1).

The set is closed: ``analyze`` applies these and never interprets a host's
annotation beyond them. Names are tuples, so an effect is hashable and compares
by value.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


def _names(names: Any) -> tuple[str, ...]:
    return (names,) if isinstance(names, str) else tuple(names)


@dataclass(frozen=True, init=False)
class Binds:
    """Names put in the namespace before the body runs; never resolved to an upstream."""

    names: tuple[str, ...]

    def __init__(self, names: Any) -> None:
        object.__setattr__(self, "names", _names(names))


@dataclass(frozen=True, init=False)
class Defines:
    """Outputs the body does not assign."""

    names: tuple[str, ...]

    def __init__(self, names: Any) -> None:
        object.__setattr__(self, "names", _names(names))


@dataclass(frozen=True, init=False)
class Reads:
    """References the body's analysis cannot see.

    With ``even_if_defined=False`` a name the cell defines is not added.
    """

    names: tuple[str, ...]
    even_if_defined: bool

    def __init__(self, names: Any, even_if_defined: bool = False) -> None:
        object.__setattr__(self, "names", _names(names))
        object.__setattr__(self, "even_if_defined", even_if_defined)


@dataclass(frozen=True, init=False)
class Keeps:
    """Outputs kept with no downstream reader."""

    names: tuple[str, ...]

    def __init__(self, names: Any) -> None:
        object.__setattr__(self, "names", _names(names))


@dataclass(frozen=True, init=False)
class After:
    """Ordering edges from these cells, with no variable."""

    cell_ids: tuple[str, ...]

    def __init__(self, cell_ids: Any) -> None:
        object.__setattr__(self, "cell_ids", _names(cell_ids))


@dataclass(frozen=True)
class Seeds:
    """Resolve ``name`` from ``cell_id``, in place of its latest earlier producer."""

    cell_id: str
    name: str


@dataclass(frozen=True)
class Member:
    """Member ``name`` of variant group ``group``."""

    group: str
    name: str


@dataclass(frozen=True)
class FanOut:
    """Run once per member of ``group``, or of the single sweep group the cell reads."""

    group: str | None = None


@dataclass(frozen=True)
class Input:
    """An external input the host resolves.

    ``name`` is the binding name (or another stable name); ``spec`` is whatever the
    host's resolver for ``kind`` needs. One input per ``(kind, name)`` in a cell.
    """

    kind: str
    name: str
    spec: Any = field(default=None, compare=False)


@dataclass(frozen=True)
class NoCache:
    """Never served from cache; a downstream key follows this cell's bytes."""


Effect = Binds | Defines | Reads | Keeps | After | Seeds | Member | FanOut | Input | NoCache

EFFECT_TYPES: tuple[type, ...] = (
    Binds,
    Defines,
    Reads,
    Keeps,
    After,
    Seeds,
    Member,
    FanOut,
    Input,
    NoCache,
)
