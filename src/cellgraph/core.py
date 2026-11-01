"""The core vocabulary: ``@name``, ``@after``, ``@nocache``, ``@variant``, ``@per_variant``.

The parsing rules and diagnostic codes are Strata's (``annotations.py`` and
``annotation_validation.py``), which these were ported from.
"""

from __future__ import annotations

from cellgraph.effects import After, FanOut, Member, NoCache
from cellgraph.grammar import Directive
from cellgraph.vocabulary import Annotation, AnnotationError, flag, mark_stacked, scalar


def _parse_after(directives: list[Directive]) -> list[str]:
    """One edge per whitespace-separated id; lines stack, and a repeated id counts once."""
    ids: list[str] = []
    for directive in directives:
        if not directive.value:
            raise AnnotationError("`@after` needs a cell id", line=directive.line)
        for token in directive.value.split():
            token = token.strip().rstrip(",")
            if token and token not in ids:
                ids.append(token)
    return ids


mark_stacked(_parse_after)


def _parse_variant(directives: list[Directive]) -> tuple[str, str]:
    """``# @variant <group> <name>``, both identifiers; the last line wins."""
    for directive in directives:
        parts = directive.value.split()
        if len(parts) != 2 or not all(part.isidentifier() for part in parts):
            raise AnnotationError(
                f"`@variant {directive.value}`: expected `@variant <group> <name>`, "
                "both Python identifiers",
                code="variant_malformed",
                line=directive.line,
            )
    group, name = directives[-1].value.split()
    return group, name


def _parse_per_variant(directives: list[Directive]) -> str | None:
    """``# @per_variant`` (the single sweep group read) or ``# @per_variant <group>``."""
    for directive in directives:
        tokens = directive.value.split()
        if len(tokens) > 1 or (tokens and not tokens[0].isidentifier()):
            raise AnnotationError(
                f"`@per_variant {directive.value}`: expected at most one group name, "
                "a Python identifier",
                line=directive.line,
            )
    tokens = directives[-1].value.split()
    return tokens[0] if tokens else None


def core_annotations() -> list[Annotation]:
    return [
        Annotation("name", parse=scalar(str), in_key=False),
        Annotation(
            "after",
            parse=_parse_after,
            in_key=False,
            effects=lambda ids: [After(ids)],
        ),
        Annotation(
            "nocache",
            parse=flag(),
            in_key=False,
            effects=lambda on: [NoCache()] if on else [],
        ),
        Annotation(
            "variant",
            parse=_parse_variant,
            in_key=False,
            effects=lambda value: [Member(group=value[0], name=value[1])],
        ),
        # The group decides which upstream binds a scalar, so it is in the key; a
        # bare ``@per_variant`` is present with the value None.
        Annotation(
            "per_variant",
            parse=_parse_per_variant,
            effects=lambda group: [FanOut(group)],
        ),
    ]
