"""Annotations and the vocabulary a host composes from them (design 4).

An annotation declares its key (and any extra keys it reads), a parser from its
directive lines to a value, its effects from the closed set in ``effects``, whether
it is in the definition key, an encoder when its value is not already canonical,
and an optional validator. The package applies the effects and never interprets
an annotation beyond them: one with no effects that is not in the key is metadata,
available to the host as ``cell.annotations[key]``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from cellgraph.diagnostics import Diagnostic
from cellgraph.effects import Effect
from cellgraph.grammar import GRAMMAR, Directive, Grammar

if TYPE_CHECKING:
    from cellgraph.analysis import Cell


class AnnotationError(ValueError):
    """A directive that cannot be read as written.

    Raised by a parser, an encoder or a validator. It becomes an error diagnostic,
    and the cell is not plannable. ``code`` defaults to ``<key>_unreadable``.
    """

    def __init__(self, message: str, *, code: str | None = None, line: int | None = None):
        super().__init__(message)
        self.code = code
        self.line = line


@dataclass(frozen=True)
class ValidationContext:
    """What a validator sees: the cell, its names after effects, and the host's context."""

    cell: Cell
    defines: tuple[str, ...]
    references: tuple[str, ...]
    host: Any = None


Parser = Callable[[list[Directive]], Any]
Validator = Callable[[Any, ValidationContext], Iterable[Diagnostic]]
CellEffects = Callable[[Mapping[str, Any], Mapping[str, Any]], Iterable[Effect]]


_OFF = frozenset({"off", "false", "no", "0"})
_FLAG_VALUES = frozenset({"", "on", "off", "true", "false", "yes", "no", "1", "0"})


def scalar(convert: Callable[[str], Any] = str) -> Parser:
    """One value; the last directive wins. An empty value or a failed ``convert`` is an error."""

    def parse(directives: list[Directive]) -> Any:
        last = directives[-1]
        if not last.value:
            raise AnnotationError(f"`@{last.key}` needs a value", line=last.line)
        try:
            return convert(last.value)
        except (TypeError, ValueError) as exc:
            raise AnnotationError(
                f"`@{last.key} {last.value}` could not be read: {exc}", line=last.line
            ) from exc

    return parse


def flag() -> Parser:
    """``# @key`` or ``# @key on|off|true|false|yes|no|1|0``; the last directive wins."""

    def parse(directives: list[Directive]) -> bool:
        last = directives[-1]
        if last.value.lower() not in _FLAG_VALUES:
            raise AnnotationError(
                f"`@{last.key} {last.value}`: expected nothing, or one of on, off, true, "
                "false, yes, no, 1, 0",
                line=last.line,
            )
        return last.value.strip().lower() not in _OFF

    return parse


def stacked(item: Callable[[Directive], Any]) -> Parser:
    """One value per directive line, in source order; the lines may repeat."""

    def parse(directives: list[Directive]) -> list[Any]:
        return [item(directive) for directive in directives]

    return mark_stacked(parse)


def mark_stacked(parse: Parser) -> Parser:
    """Mark *parse* as reading repeated lines, so ``set_directive`` refuses its key."""
    setattr(parse, "stacked", True)  # noqa: B010
    return parse


def is_stacked(parse: Parser) -> bool:
    """Whether *parse* reads repeated lines (``stacked``), which a single-line edit would drop."""
    return bool(getattr(parse, "stacked", False))


def _default_parse(directives: list[Directive]) -> Any:
    return scalar(str)(directives)


@dataclass(frozen=True)
class Annotation:
    """One annotation of a vocabulary.

    - ``parse`` gets every directive of ``key`` and ``also_reads``, in source order.
    - ``effects`` maps the parsed value to effects; it runs only when a directive is
      present.
    - ``in_key``: a present annotation adds one field, named ``key``, to the definition
      key. The value is ``encode(value)``, or the value itself when it is canonical.
    - ``validate`` gets the value and a ``ValidationContext`` and returns diagnostics.
    """

    key: str
    parse: Parser = _default_parse
    effects: Callable[[Any], Iterable[Effect]] | None = None
    in_key: bool = True
    encode: Callable[[Any], Any] | None = None
    validate: Validator | None = None
    also_reads: tuple[str, ...] = ()

    @property
    def keys(self) -> tuple[str, ...]:
        """Every directive key this annotation reads."""
        return (self.key, *self.also_reads)


@dataclass(frozen=True)
class Vocabulary:
    """A set of annotations with distinct keys, and an optional per-cell host hook.

    ``cell_effects(meta, parsed)`` is called for every cell, with the host's ``meta``
    and the parsed value of each present annotation. It returns effects that come
    from host configuration rather than directives; they go through the same phases
    and conflict rules.
    """

    annotations: tuple[Annotation, ...] = ()
    cell_effects: CellEffects | None = None
    grammar: Grammar = field(default=GRAMMAR, compare=False)

    def __init__(
        self,
        annotations: Sequence[Annotation] = (),
        cell_effects: CellEffects | None = None,
        grammar: Grammar = GRAMMAR,
    ) -> None:
        seen: dict[str, str] = {}
        for annotation in annotations:
            for key in annotation.keys:
                if key in seen:
                    raise ValueError(
                        f"directive key {key!r} is claimed by both @{seen[key]} and "
                        f"@{annotation.key}"
                    )
                seen[key] = annotation.key
        object.__setattr__(self, "annotations", tuple(annotations))
        object.__setattr__(self, "cell_effects", cell_effects)
        object.__setattr__(self, "grammar", grammar)

    @classmethod
    def core(cls) -> Vocabulary:
        """``@name``, ``@after``, ``@nocache``, ``@variant`` and ``@per_variant`` (design 6)."""
        from cellgraph.core import core_annotations

        return cls(core_annotations())

    def __or__(self, other: Vocabulary) -> Vocabulary:
        """Both vocabularies; a key in both is refused, so order never decides a result."""
        hooks = [hook for hook in (self.cell_effects, other.cell_effects) if hook is not None]
        combined: CellEffects | None
        if len(hooks) == 2:
            first, second = hooks

            def combined(meta: Mapping[str, Any], parsed: Mapping[str, Any]) -> list[Effect]:
                return [*first(meta, parsed), *second(meta, parsed)]

        else:
            combined = hooks[0] if hooks else None
        return Vocabulary([*self.annotations, *other.annotations], cell_effects=combined)

    def __iter__(self) -> Iterator[Annotation]:
        return iter(self.annotations)

    def __contains__(self, key: object) -> bool:
        return any(annotation.key == key for annotation in self.annotations)

    def __getitem__(self, key: str) -> Annotation:
        for annotation in self.annotations:
            if annotation.key == key:
                return annotation
        raise KeyError(key)

    @property
    def keys(self) -> frozenset[str]:
        """Every directive key the vocabulary reads, ``also_reads`` keys included."""
        return frozenset(key for annotation in self.annotations for key in annotation.keys)

    def owner(self, directive_key: str) -> Annotation | None:
        """The annotation that reads directives of *directive_key*, if any."""
        for annotation in self.annotations:
            if directive_key in annotation.keys:
                return annotation
        return None

    def without(self, *keys: str) -> Vocabulary:
        """This vocabulary without the annotations named *keys*."""
        missing = set(keys) - {a.key for a in self.annotations}
        if missing:
            raise KeyError(sorted(missing))
        return Vocabulary(
            [a for a in self.annotations if a.key not in keys],
            cell_effects=self.cell_effects,
            grammar=self.grammar,
        )

    def replace(self, key: str, **changes: Any) -> Vocabulary:
        """This vocabulary with annotation *key* changed, e.g. given a validator."""
        return Vocabulary(
            [replace(a, **changes) if a.key == key else a for a in self.annotations],
            cell_effects=self.cell_effects,
            grammar=self.grammar,
        )

    def set_directive(self, source: str, key: str, value: str) -> str:
        """*source* with one ``# @key value`` set; refused for a stacked annotation's key."""
        owner = self.owner(key.lower())
        if owner is not None and is_stacked(owner.parse):
            raise ValueError(f"@{key} is repeatable; edit the cell source directly")
        return self.grammar.set_directive(source, key, value)

    def remove_directive(self, source: str, key: str) -> str:
        """*source* with every ``# @key`` directive removed from its leading block."""
        return self.grammar.remove_directive(source, key)
