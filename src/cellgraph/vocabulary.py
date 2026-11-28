"""Annotations and the vocabulary a host composes from them.

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
from cellgraph.languages import (
    LanguageAnalyzer,
    comment_marker,
    directive_keys,
    get_language_analyzer,
)

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
class Partial:
    """What a parser returns when part of an annotation reads: the value, and the errors.

    The annotation is present with ``value`` (its effects apply) and each error becomes
    a diagnostic, so the cell is not plannable. With errors, an ``in_key`` annotation is
    unkeyed: its value cannot tell the cell from one without the unread lines. For lines that
    cannot be read beside lines that can, as Strata keeps the readable ones.
    """

    value: Any
    errors: tuple[AnnotationError, ...]

    def __init__(self, value: Any, errors: Iterable[AnnotationError]) -> None:
        errors = tuple(errors)
        for error in errors:
            if not isinstance(error, AnnotationError):
                raise TypeError(f"a Partial's errors must be AnnotationErrors, not {error!r}")
        object.__setattr__(self, "value", value)
        object.__setattr__(self, "errors", errors)


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


# What a converter or an encoder raises for a value it cannot take (``int("x")``, a
# dict lookup). Anything else is a bug in it, and propagates.
CONVERSION_ERRORS = (ValueError, TypeError, LookupError)


def scalar(convert: Callable[[str], Any] = str) -> Parser:
    """One value; the last directive wins. An empty value or a failed ``convert`` is an error."""

    def parse(directives: list[Directive]) -> Any:
        last = directives[-1]
        if not last.value:
            raise AnnotationError(f"`@{last.key}` needs a value", line=last.line)
        try:
            return convert(last.value)
        except AnnotationError:
            raise  # the converter's own code and line
        except CONVERSION_ERRORS as exc:
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


def _on_line(exc: AnnotationError, line: int) -> AnnotationError:
    """A copy of *exc* on *line* unless it names its own: *item* may raise one error
    for every bad line."""
    # Not ``copy.copy``: that calls ``__init__``, which a subclass may give more
    # parameters. ``__new__`` sets ``args`` alone; the attributes come over as they are.
    err = type(exc).__new__(type(exc), *exc.args)
    err.__dict__.update(exc.__dict__)
    err.line = exc.line if exc.line is not None else line
    err.__cause__ = exc.__cause__
    err.__traceback__ = exc.__traceback__
    return err


def _leaves(group: BaseException | None) -> Iterator[AnnotationError]:
    if isinstance(group, BaseExceptionGroup):
        for exc in group.exceptions:
            yield from _leaves(exc)
    elif isinstance(group, AnnotationError):
        yield group


def stacked(item: Callable[[Directive], Any]) -> Parser:
    """One value per directive line, in source order; the lines may repeat.

    A line *item* cannot read (it raises ``AnnotationError`` or a conversion error) is
    an error on that line; the others are kept, as a ``Partial``.
    """

    def parse(directives: list[Directive]) -> Any:
        values: list[Any] = []
        errors: list[AnnotationError] = []
        for directive in directives:
            try:
                values.append(item(directive))
            except AnnotationError as exc:
                errors.append(_on_line(exc, directive.line))
            except ExceptionGroup as group:
                found, rest = group.split(AnnotationError)
                if rest is not None:
                    raise
                errors.extend(_on_line(exc, directive.line) for exc in _leaves(found))
            except CONVERSION_ERRORS as exc:
                errors.append(
                    AnnotationError(
                        f"`@{directive.key} {directive.value}` could not be read: {exc}",
                        line=directive.line,
                    )
                )
        return Partial(values, errors) if errors else values

    return mark_stacked(parse)


def mark_stacked(parse: Parser) -> Parser:
    """Mark *parse* as reading repeated lines, so ``set_directive`` refuses its key."""
    setattr(parse, "stacked", True)
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

    def __post_init__(self) -> None:
        if isinstance(self.also_reads, (str, bytes)):
            raise TypeError(f"also_reads must be a sequence of keys, not {self.also_reads!r}")
        also_reads = tuple(self.also_reads)  # once: an iterator is read only once
        if not all(isinstance(key, str) for key in also_reads):
            raise TypeError(f"also_reads must be a sequence of keys, not {also_reads!r}")
        object.__setattr__(self, "also_reads", also_reads)
        for key in self.keys:
            if not isinstance(key, str):
                raise TypeError(f"an annotation key must be a str, not {key!r}")
            # A grammar reads directive keys lower-cased; what else a key may hold is
            # the grammar's to say (Quarto's ``fig-cap``).
            if not key or key != key.lower() or any(c.isspace() for c in key):
                raise ValueError(
                    f"an annotation key must be non-empty, lower-case and without "
                    f"whitespace, not {key!r}"
                )

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
    # Compared, so two vocabularies that read directives differently are not equal; not
    # hashed, since a grammar subclass with state of its own may not hash.
    grammar: Grammar = field(default=GRAMMAR, hash=False)

    def __init__(
        self,
        annotations: Sequence[Annotation] = (),
        cell_effects: CellEffects | None = None,
        grammar: Grammar = GRAMMAR,
    ) -> None:
        annotations = tuple(annotations)
        if not all(isinstance(a, Annotation) for a in annotations):
            raise TypeError(f"a vocabulary holds Annotations, not {annotations!r}")
        if cell_effects is not None and not callable(cell_effects):
            raise TypeError(f"cell_effects must be callable, not {cell_effects!r}")
        if not isinstance(grammar, Grammar):
            raise TypeError(f"grammar must be a Grammar, not {grammar!r}")
        seen: dict[str, str] = {}
        for annotation in annotations:
            for key in annotation.keys:
                if not grammar.reads_key(key):
                    raise ValueError(
                        f"directive key {key!r} of @{annotation.key} is not one this "
                        "vocabulary's grammar reads"
                    )
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
        """``@name``, ``@after``, ``@nocache``, ``@variant``, ``@per_variant`` and ``@each``."""
        from cellgraph.core import core_annotations

        return cls(core_annotations())

    def __or__(self, other: Vocabulary) -> Vocabulary:
        """Both vocabularies; a key in both is refused, so order never decides a result.

        A custom grammar on one side is kept; one on each side is refused unless the
        two are equal (``Grammar.__eq__``), and then the left one is kept.
        """
        if not isinstance(other, Vocabulary):
            return NotImplemented
        custom = [g for g in (self.grammar, other.grammar) if g != GRAMMAR]
        if len(custom) == 2 and custom[0] != custom[1]:
            raise ValueError("both vocabularies have their own grammar")
        hooks = [hook for hook in (self.cell_effects, other.cell_effects) if hook is not None]
        combined: CellEffects | None
        if len(hooks) == 2:
            first, second = hooks

            def combined(meta: Mapping[str, Any], parsed: Mapping[str, Any]) -> list[Effect]:
                return [*first(meta, parsed), *second(meta, parsed)]

        else:
            combined = hooks[0] if hooks else None
        return Vocabulary(
            [*self.annotations, *other.annotations],
            cell_effects=combined,
            grammar=custom[0] if custom else GRAMMAR,
        )

    def __iter__(self) -> Iterator[Annotation]:
        return iter(self.annotations)

    def __contains__(self, key: object) -> bool:
        """Whether an annotation is named *key*; an ``also_reads`` key is in ``keys``."""
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

    def replace(self, key: str, /, **changes: Any) -> Vocabulary:
        """This vocabulary with annotation *key* changed, e.g. given a validator."""
        if key not in {a.key for a in self.annotations}:
            raise KeyError(key)  # a misspelt key would change nothing
        return Vocabulary(
            [replace(a, **changes) if a.key == key else a for a in self.annotations],
            cell_effects=self.cell_effects,
            grammar=self.grammar,
        )

    def grammar_for(self, analyzer: LanguageAnalyzer) -> Grammar:
        """The grammar of a cell in *analyzer*'s language: this one, with its marker."""
        return self.grammar.with_marker(comment_marker(analyzer))

    def set_directive(
        self, source: str, key: str, value: str, *, language: str | LanguageAnalyzer = "python"
    ) -> str:
        """*source* with one ``@key value`` set in *language*'s comment marker.

        *language* is a registered language's name, or the analyzer itself for a host
        that passes its own ``analyzers=`` table. Refused for a stacked annotation's
        key, which a single-line edit would truncate.
        """
        if not isinstance(key, str):
            raise TypeError(f"a directive key must be a str, not {key!r}")
        owner = self.owner(key.lower())
        if owner is not None and is_stacked(owner.parse):
            raise ValueError(f"@{key} is repeatable; edit the cell source directly")
        analyzer = _analyzer(language)
        read = directive_keys(analyzer)
        if read is not None and key.lower() not in read:
            # The line would be body text the analysis never reads as @key.
            raise ValueError(f"the cell's language does not read @{key}")
        return self.grammar_for(analyzer).set_directive(source, key, value)

    def remove_directive(
        self, source: str, key: str, *, language: str | LanguageAnalyzer = "python"
    ) -> str:
        """*source* with every ``@key`` directive removed from its leading block.

        A key the language does not read is body text, which stays.
        """
        if not isinstance(key, str):
            raise TypeError(f"a directive key must be a str, not {key!r}")
        analyzer = _analyzer(language)
        read = directive_keys(analyzer)
        if read is not None and key.lower() not in read:
            return source
        return self.grammar_for(analyzer).remove_directive(source, key)


def _analyzer(language: str | LanguageAnalyzer) -> LanguageAnalyzer:
    return get_language_analyzer(language) if isinstance(language, str) else language
