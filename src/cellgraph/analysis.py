"""Cell analysis and the DAG.

``analyze`` runs the phases in a fixed order: parse, names, validate, wire, variant
notes; then it marks what is plannable and computes every definition key. It always returns
an analysis an editor can show. A cell carries ``plannable``, which is false for
its own errors, for a structural error it is part of (a cycle, a variant
collision) and for any cell it depends on that is not plannable.
``Analysis.graph`` holds every candidate edge; ``Analysis.dag`` keeps only
plannable cells.

The wiring is Strata's ``NotebookDag.from_cells``, ported as is.
"""

from __future__ import annotations

import ast
import difflib
from collections import Counter, deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

# keys and plan import this module inside their functions (and to type-check), not at
# module level, which is what keeps importing them here from being a cycle.
from cellgraph import keys as _keys
from cellgraph import plan as _plan
from cellgraph.diagnostics import Diagnostic, Severity
from cellgraph.effects import (
    EFFECT_TYPES,
    After,
    Binds,
    Defines,
    Effect,
    FanOut,
    Input,
    Keeps,
    Member,
    NoCache,
    Reads,
    Seeds,
)
from cellgraph.grammar import Directive
from cellgraph.languages import (
    Analyzed,
    LanguageAnalyzer,
    UnknownLanguageError,
    directive_keys,
    registered_analyzers,
)
from cellgraph.vocabulary import (
    CONVERSION_ERRORS,
    Annotation,
    AnnotationError,
    Partial,
    ValidationContext,
    Vocabulary,
)

# Diagnostics that make a structural error: the cell is on a cycle or in a collision.
STRUCTURAL_CODES = frozenset({"cycle", "variant_collision"})


@dataclass(frozen=True)
class Cell:
    """A cell as the host gives it. ``meta`` is the host's, passed to ``cell_effects``."""

    id: str
    source: str
    language: str = "python"
    meta: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class GroupConfig:
    """Per variant group: its mode (``switch`` or ``sweep``) and its active member's name."""

    modes: Mapping[str, str] = field(default_factory=dict)
    active: Mapping[str, str] = field(default_factory=dict)


class NotPlannableError(ValueError):
    """A key or a plan was asked of a cell that is not plannable."""

    def __init__(self, cell_id: str, diagnostics: Sequence[Diagnostic]):
        reasons = "; ".join(f"{d.cell}: {d.code}" for d in diagnostics) or "no diagnostic"
        super().__init__(f"cell {cell_id} is not plannable ({reasons})")
        self.cell_id = cell_id
        self.diagnostics = list(diagnostics)


class InactiveVariantError(NotPlannableError):
    """A key or a plan was asked of an inactive switch member (or a repeated variant).

    What it reads depends on the selection: the cells between it and its upstream are
    wired for the active variant. A host selects it and analyzes again.
    """

    def __init__(self, cell_id: str):
        ValueError.__init__(
            self, f"cell {cell_id} is an inactive variant; select it and analyze again"
        )
        self.cell_id = cell_id
        self.diagnostics = []


# --- the DAG ----------------------------------------------------------------------


@dataclass
class DagEdge:
    """``variable`` flows from ``from_cell_id`` to ``to_cell_id``; empty for an ordering edge."""

    from_cell_id: str
    to_cell_id: str
    variable: str


@dataclass
class VariantGroupResolution:
    """One variant group: its members in source order, inactive ones included."""

    group: str
    active_name: str
    active_cell_id: str
    members: list[tuple[str, str]] = field(default_factory=list)
    mode: str = "switch"


@dataclass(frozen=True)
class SweepProducer:
    """A variable produced across a set of variants.

    - **Sweep group** (``fanout_cell is None``): ``variants`` maps
      ``variant_name -> member_cell_id``; a reader takes one edge per member.
    - **Fan-out cell** (``fanout_cell`` set): every entry points at ``fanout_cell``,
      which runs once per variant.
    """

    group: str
    variants: tuple[tuple[str, str], ...]
    fanout_cell: str | None = None


@dataclass
class Dag:
    """The wired graph over active cells."""

    edges: list[DagEdge] = field(default_factory=list)
    cell_upstream: dict[str, list[str]] = field(default_factory=dict)
    cell_downstream: dict[str, list[str]] = field(default_factory=dict)
    leaves: set[str] = field(default_factory=set)
    roots: set[str] = field(default_factory=set)
    topological_order: list[str] = field(default_factory=list)
    variable_producer: dict[str, str | SweepProducer] = field(default_factory=dict)
    consumed_variables: dict[str, set[str]] = field(default_factory=dict)
    shadow_warnings: dict[str, list[str]] = field(default_factory=dict)
    variant_groups: list[VariantGroupResolution] = field(default_factory=list)
    inactive_cells: set[str] = field(default_factory=set)
    wired_inputs: dict[str, dict[str, set[str]]] = field(default_factory=dict)
    fanout_cells: dict[str, SweepProducer] = field(default_factory=dict)
    # An ``@each`` cell: the name its items come from. Not a sweep: a reader sees one output.
    each_cells: dict[str, str] = field(default_factory=dict)
    read_producers: dict[str, dict[str, str | SweepProducer]] = field(default_factory=dict)

    def wired_variables(self, consumer_id: str, producer_id: str) -> set[str]:
        """The variables ``consumer_id`` reads from ``producer_id``."""
        return self.wired_inputs.get(consumer_id, {}).get(producer_id, set())

    def read_producer(self, consumer_id: str, var: str) -> str | SweepProducer | None:
        """What ``consumer_id`` reads ``var`` from; its last producer for a name it never reads."""
        reads = self.read_producers.get(consumer_id, {})
        return reads[var] if var in reads else self.variable_producer.get(var)

    def sweep_groups_read(self, consumer_id: str) -> set[str]:
        """The groups whose sweep (or fan-out over it) ``consumer_id`` reads."""
        return {
            producer.group
            for producer in self.read_producers.get(consumer_id, {}).values()
            if isinstance(producer, SweepProducer)
        }

    def upstream_reachable(self, start: str) -> set[str]:
        """The cells reachable upstream from ``start``, ``start`` included."""
        visited: set[str] = set()
        queue: deque[str] = deque([start])
        while queue:
            current = queue.popleft()
            if current in visited:
                continue
            visited.add(current)
            for neighbor in self.cell_upstream.get(current, []):
                if neighbor not in visited:
                    queue.append(neighbor)
        return visited


def _kahn(dag: Dag, cell_ids: list[str]) -> list[str]:
    """Topological order of *cell_ids*; cells on or after a cycle are left out."""
    members = set(cell_ids)
    degree = {cid: sum(1 for u in dag.cell_upstream[cid] if u in members) for cid in cell_ids}
    queue: deque[str] = deque(cid for cid in cell_ids if degree[cid] == 0)
    order: list[str] = []
    while queue:
        current = queue.popleft()
        order.append(current)
        for downstream in dag.cell_downstream[current]:
            if downstream not in members:
                continue
            degree[downstream] -= 1
            if degree[downstream] == 0:
                queue.append(downstream)
    return order


def _strong_components(dag: Dag, cell_ids: list[str]) -> list[list[str]]:
    """The strongly connected components, downstream ones first (Tarjan, without recursion).

    A notebook can be thousands of cells long, past any recursion limit.
    """
    members = set(cell_ids)
    successors = {v: [w for w in dag.cell_downstream[v] if w in members] for v in cell_ids}
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    found: list[list[str]] = []

    def enter(v: str) -> None:
        index[v] = low[v] = len(index)
        stack.append(v)
        on_stack.add(v)

    for root in cell_ids:
        if root in index:
            continue
        enter(root)
        work = [(root, 0)]
        while work:
            v, i = work[-1]
            if i < len(successors[v]):
                work[-1] = (v, i + 1)
                w = successors[v][i]
                if w not in index:
                    enter(w)
                    work.append((w, 0))
                elif w in on_stack:
                    low[v] = min(low[v], index[w])
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[v])
            if low[v] == index[v]:
                component = []
                while True:
                    w = stack.pop()
                    on_stack.discard(w)
                    component.append(w)
                    if w == v:
                        break
                found.append(component)
    return found


def _cycle_through(dag: Dag, component: list[str], start: str) -> list[str]:
    """The shortest cycle from *start* back to it inside *component*, as ids along it."""
    inside = set(component)
    parent: dict[str, str | None] = {start: None}
    queue: deque[str] = deque([start])
    while queue:
        v = queue.popleft()
        for w in dag.cell_downstream[v]:
            if w == start:
                path = [v]
                while (up := parent[path[-1]]) is not None:
                    path.append(up)
                return [*reversed(path), start]
            if w in inside and w not in parent:
                parent[w] = v
                queue.append(w)
    raise AssertionError("a strongly connected component has a cycle through each cell")


def _wire(dag: Dag, from_id: str, to_id: str, var: str) -> None:
    dag.edges.append(DagEdge(from_cell_id=from_id, to_cell_id=to_id, variable=var))
    if from_id not in dag.cell_upstream[to_id]:
        dag.cell_upstream[to_id].append(from_id)
    if to_id not in dag.cell_downstream[from_id]:
        dag.cell_downstream[from_id].append(to_id)
    dag.consumed_variables[from_id].add(var)
    dag.wired_inputs.setdefault(to_id, {}).setdefault(from_id, set()).add(var)


# --- per-cell analysis --------------------------------------------------------------


@dataclass
class CellAnalysis:
    """One cell after every phase."""

    id: str
    language: str
    source: str
    # Parsed value of each annotation present on the cell, by key.
    annotations: dict[str, Any] = field(default_factory=dict)
    directives: list[Directive] = field(default_factory=list)
    effects: list[Effect] = field(default_factory=list)
    # Per effect: the annotation key and line it came from; None for the host's hook.
    origins: list[tuple[str | None, int | None]] = field(default_factory=list)
    defines: list[str] = field(default_factory=list)
    references: list[str] = field(default_factory=list)
    builtin_references: list[str] = field(default_factory=list)
    mutation_defines: list[str] = field(default_factory=list)
    bound: list[str] = field(default_factory=list)
    after: list[str] = field(default_factory=list)
    seeds: dict[str, str] = field(default_factory=dict)
    seed_origins: dict[str, tuple[str | None, int | None]] = field(default_factory=dict)
    # The annotation that ordered each ``after`` id, and the one that made ``fanout``.
    after_origins: dict[str, str | None] = field(default_factory=dict)
    member: Member | None = None
    fanout: FanOut | None = None
    fanout_origin: tuple[str | None, int | None] = (None, None)
    kept: list[str] = field(default_factory=list)
    inputs: list[Input] = field(default_factory=list)
    nocache: bool = False
    key_fields: dict[str, Any] = field(default_factory=dict)
    # In-key annotations present but missing from ``key_fields`` (unreadable or
    # unencodable): the key cannot tell this cell from one without them.
    unkeyed: list[str] = field(default_factory=list)
    diagnostics: list[Diagnostic] = field(default_factory=list)
    plannable: bool = True
    # The cells whose own errors keep this one from planning (itself included).
    blocked_by: list[str] = field(default_factory=list)
    # Set by ``analyze`` for every cell, plannable or not.
    definition_key: str | None = None
    normalized: str | None = None

    def error(
        self,
        code: str,
        message: str,
        *,
        annotation: str | None = None,
        line=None,
        args: Mapping[str, Any] | None = None,
    ):
        self.diagnostics.append(
            Diagnostic(code, Severity.ERROR, message, self.id, annotation, line, dict(args or {}))
        )

    def note(
        self,
        code: str,
        severity: Severity,
        message: str,
        *,
        annotation: str | None = None,
        line=None,
        args: Mapping[str, Any] | None = None,
    ):
        self.diagnostics.append(
            Diagnostic(code, severity, message, self.id, annotation, line, dict(args or {}))
        )

    @property
    def has_error(self) -> bool:
        return any(d.severity == Severity.ERROR for d in self.diagnostics)


def _annotation_errors(
    exc: AnnotationError | ExceptionGroup, *, conversions: bool = False
) -> list[AnnotationError]:
    """The errors a parser or validator raised: one, or an ``ExceptionGroup`` of them.

    With *conversions*, an encoder's, a group's ``CONVERSION_ERRORS`` count as well.
    """
    if isinstance(exc, AnnotationError):
        return [exc]
    accepted = (AnnotationError, *CONVERSION_ERRORS) if conversions else AnnotationError
    matched, rest = exc.split(accepted)
    if rest is not None:
        raise rest
    assert matched is not None
    return [
        e if isinstance(e, AnnotationError) else AnnotationError(str(e)) for e in _leaves(matched)
    ]


def _leaves(group: BaseExceptionGroup) -> list[Exception]:
    out: list[Exception] = []
    for exc in group.exceptions:
        if isinstance(exc, BaseExceptionGroup):
            out.extend(_leaves(exc))
        elif isinstance(exc, Exception):
            out.append(exc)
    return out


def _member_origin(cell: CellAnalysis) -> tuple[str | None, int | None]:
    """The annotation and line of the cell's Member effect: ``@variant``, or a host's."""
    for effect, origin in zip(cell.effects, cell.origins, strict=True):
        if isinstance(effect, Member) and effect == cell.member:
            return origin
    return None, None


def _first_line(directives: list[Directive], key: str) -> int | None:
    return next((d.line for d in directives if d.key == key), None)


def _naming_line(directives: list[Directive], key: str, token: str) -> int | None:
    """The first *key* line with *token* among its values; else the first *key* line."""
    for d in directives:
        if d.key == key and token in (t.rstrip(",") for t in d.value.split()):
            return d.line
    return _first_line(directives, key)


def _top_level_imports(source: str) -> set[str]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                names.add(alias.asname or alias.name)
    return names


def _parse_phase(
    cell: Cell, vocabulary: Vocabulary, analyzer: LanguageAnalyzer, strict: bool
) -> tuple[CellAnalysis, Analyzed]:
    """Phase 1: the body's analysis, and every annotation's parser."""
    ca = CellAnalysis(id=cell.id, language=cell.language, source=cell.source)
    grammar = vocabulary.grammar_for(analyzer)
    ca.directives = grammar.directives(cell.source, keys=directive_keys(analyzer))

    by_owner: dict[str, list[Directive]] = {}
    for directive in ca.directives:
        owner = vocabulary.owner(directive.key)
        if owner is None:
            close = difflib.get_close_matches(directive.key, vocabulary.keys, n=1, cutoff=0.75)
            hint = f" Did you mean `@{close[0]}`?" if close else ""
            ca.note(
                "annotation_unknown",
                Severity.ERROR if strict else Severity.WARN,
                f"`@{directive.key}` is not an annotation this vocabulary knows.{hint}",
                annotation=directive.key,
                line=directive.line,
                args={"key": directive.key, "suggestion": close[0] if close else None},
            )
            continue
        by_owner.setdefault(owner.key, []).append(directive)

    for annotation in vocabulary:
        directives = by_owner.get(annotation.key)
        if not directives:
            continue
        try:
            value = annotation.parse(directives)
        except (AnnotationError, ExceptionGroup) as raised:
            for exc in _annotation_errors(raised):
                ca.error(
                    exc.code or f"{annotation.key}_unreadable",
                    str(exc),
                    annotation=annotation.key,
                    line=exc.line if exc.line is not None else directives[0].line,
                )
            if annotation.in_key:
                ca.unkeyed.append(annotation.key)
            continue
        if isinstance(value, Partial):
            for exc in value.errors:
                ca.error(
                    exc.code or f"{annotation.key}_unreadable",
                    str(exc),
                    annotation=annotation.key,
                    line=exc.line if exc.line is not None else directives[0].line,
                )
            value = value.value
        ca.annotations[annotation.key] = value
        if annotation.in_key:
            _encode(ca, annotation, value, directives[0].line)
        if annotation.effects is not None:
            produced = list(annotation.effects(value))
            ca.effects.extend(produced)
            ca.origins.extend([(annotation.key, directives[0].line)] * len(produced))

    if vocabulary.cell_effects is not None:
        hooked = list(vocabulary.cell_effects(cell.meta, dict(ca.annotations)))
        ca.effects.extend(hooked)
        ca.origins.extend([(None, None)] * len(hooked))
    for effect in ca.effects:
        if not isinstance(effect, EFFECT_TYPES):
            raise TypeError(f"{effect!r} is not one of the ten effects")

    analyzed = analyzer.analyze(cell)
    if analyzed.error is not None:
        ca.error("syntax_error", analyzed.error)
    ca.diagnostics.extend(replace(d, cell=ca.id) for d in analyzed.diagnostics)
    return ca, analyzed


def _encode(ca: CellAnalysis, annotation: Annotation, value: Any, line: int) -> None:
    """The annotation's key field, or ``<key>_unencodable`` and the key in ``unkeyed``."""
    key = annotation.key
    try:
        encoded = annotation.encode(value) if annotation.encode is not None else value
    except (AnnotationError, ExceptionGroup) as raised:
        errors = _annotation_errors(raised, conversions=True)
    except CONVERSION_ERRORS as raised:
        errors = [AnnotationError(f"`@{key}` could not be encoded: {raised}")]
    else:
        if _keys.is_canonical(encoded):
            ca.key_fields[key] = encoded
            return
        name = type(encoded).__name__
        ca.error(
            f"{key}_unencodable",
            f"`@{key}` has a value the key cannot encode ({name}); the annotation needs an encoder",
            annotation=key,
            line=line,
            args={"type": name},
        )
        errors = []
    for exc in errors:
        ca.error(
            exc.code or f"{key}_unencodable",
            str(exc),
            annotation=key,
            line=exc.line if exc.line is not None else line,
        )
    ca.unkeyed.append(key)


def _names_phase(ca: CellAnalysis, analyzed: Analyzed) -> None:
    """Phase 2: defines, references and bound names after the effects."""
    defines = list(analyzed.defines)
    for effect in ca.effects:
        if isinstance(effect, Defines):
            for name in effect.names:
                if name not in defines:
                    defines.append(name)

    references = list(analyzed.references)
    read: list[str] = []
    for effect in ca.effects:
        if isinstance(effect, Reads):
            for name in effect.names:
                read.append(name)
                if name in references:
                    continue
                if not effect.even_if_defined and name in defines:
                    continue
                references.append(name)

    bound: list[str] = []
    bound_by: dict[str, tuple[str | None, int | None]] = {}
    for effect, origin in zip(ca.effects, ca.origins, strict=True):
        if isinstance(effect, Binds):
            for name in effect.names:
                if name in bound:
                    annotation, line = origin
                    ca.error(
                        "bind_conflict",
                        f"`{name}` is bound twice",
                        annotation=annotation,
                        line=line,
                        args={"name": name},
                    )
                else:
                    bound.append(name)
                    bound_by[name] = origin
    for name in sorted(set(bound) & set(read)):
        annotation, line = bound_by[name]
        ca.error(
            "bind_read_conflict",
            f"`{name}` is both bound and read from upstream",
            annotation=annotation,
            line=line,
            args={"name": name},
        )

    for effect, (origin, origin_line) in zip(ca.effects, ca.origins, strict=True):
        if isinstance(effect, FanOut) and effect.over is not None:
            # An instance's key leaves the source out, so the cell must not read it whole:
            # not in its body (a builtin's name included), nor through a Reads effect.
            if effect.over in {*analyzed.references, *analyzed.builtin_references, *read}:
                ca.error(
                    "each_reads_source",
                    f"the cell reads `{effect.over}` itself, which every item's result would "
                    "then depend on; read each item, or compute it in a cell before this one",
                    annotation=origin,
                    line=origin_line,
                    args={"source": effect.over},
                )
            elif effect.over not in bound and effect.over not in references:
                references.append(effect.over)

    ca.defines = defines
    ca.references = [name for name in references if name not in bound]
    ca.builtin_references = [name for name in analyzed.builtin_references if name not in bound]
    ca.mutation_defines = list(analyzed.mutation_defines)
    ca.bound = bound

    member_conflict = fanout_conflict = False
    for effect, (origin, origin_line) in zip(ca.effects, ca.origins, strict=True):
        if isinstance(effect, After):
            for cell_id in effect.cell_ids:
                if cell_id not in ca.after:
                    ca.after.append(cell_id)
                    ca.after_origins[cell_id] = origin
        elif isinstance(effect, Seeds):
            if effect.name in ca.seeds and ca.seeds[effect.name] != effect.cell_id:
                ca.error(
                    "seed_conflict",
                    f"`{effect.name}` is seeded from two cells",
                    annotation=origin,
                    line=origin_line,
                    args={"name": effect.name},
                )
            else:
                ca.seeds[effect.name] = effect.cell_id
                ca.seed_origins[effect.name] = (origin, origin_line)
            if effect.name in bound:
                ca.error(
                    "bind_read_conflict",
                    f"`{effect.name}` is both bound and seeded",
                    annotation=origin,
                    line=origin_line,
                    args={"name": effect.name},
                )
        # A conflicting cell joins no group and fans out no way: keeping either side
        # would let composition order decide.
        elif isinstance(effect, Member):
            if member_conflict:
                continue
            if ca.member is not None and ca.member != effect:
                ca.error(
                    "member_conflict",
                    "the cell is a member of two variants",
                    annotation=origin,
                    line=origin_line,
                )
                ca.member, member_conflict = None, True
            else:
                ca.member = effect
        elif isinstance(effect, FanOut):
            if fanout_conflict:
                continue
            if ca.fanout is not None and ca.fanout != effect:
                ca.error(
                    "fanout_conflict",
                    "the cell fans out two ways",
                    annotation=origin,
                    line=origin_line,
                )
                ca.fanout, fanout_conflict = None, True
            else:
                ca.fanout, ca.fanout_origin = effect, (origin, origin_line)
        elif isinstance(effect, Keeps):
            ca.kept.extend(name for name in effect.names if name not in ca.kept)
        elif isinstance(effect, Input):
            if any((i.kind, i.name) == (effect.kind, effect.name) for i in ca.inputs):
                ca.error(
                    "input_conflict",
                    f"two {effect.kind} inputs have the address {effect.name!r}",
                    annotation=origin,
                    line=origin_line,
                    args={"kind": effect.kind, "name": effect.name},
                )
            else:
                ca.inputs.append(effect)
        elif isinstance(effect, NoCache):
            ca.nocache = True


def _resolve_groups(
    cells: list[CellAnalysis], groups: GroupConfig
) -> tuple[list[VariantGroupResolution], set[str]]:
    """Part of phase 4: each variant group's members and its active one; collisions are errors."""
    sweep = {g for g, m in groups.modes.items() if m == "sweep"}
    grouped: dict[str, list[tuple[str, str]]] = {}
    group_order: list[str] = []
    by_id = {c.id: c for c in cells}
    # Every cell sharing a name gets one diagnostic naming them all.
    named: dict[tuple[str, str], list[str]] = {}
    for cell in cells:
        if cell.member is not None:
            named.setdefault((cell.member.group, cell.member.name), []).append(cell.id)
    for (group, name), cids in named.items():
        if len(cids) < 2:
            continue
        message = (
            f"Variant name '{name}' is used by cells {', '.join(c[:8] for c in cids)} "
            f"in group '{group}'"
        )
        for cid in cids:
            origin, origin_line = _member_origin(by_id[cid])
            by_id[cid].error(
                "variant_collision",
                message,
                annotation=origin,
                line=origin_line,
                args={"group": group, "name": name, "cells": list(cids)},
            )
    repeats: set[str] = set()
    for cell in cells:
        if cell.member is None:
            continue
        group, name = cell.member.group, cell.member.name
        members = grouped.setdefault(group, [])
        if named[(group, name)][0] != cell.id:
            # The first cell keeps the name; a repeat, like an inactive member, feeds no one.
            repeats.add(cell.id)
            continue
        if not members:
            group_order.append(group)
        members.append((cell.id, name))

    resolutions: list[VariantGroupResolution] = []
    inactive: set[str] = set(repeats)
    for group in group_order:
        members = grouped[group]
        is_sweep = group in sweep
        wanted = groups.active.get(group)
        active_cell_id, active_name = members[0]
        if wanted is not None and not is_sweep:
            for cid, name in members:
                if name == wanted:
                    active_cell_id, active_name = cid, name
                    break
        resolutions.append(
            VariantGroupResolution(
                group=group,
                active_name=active_name,
                active_cell_id=active_cell_id,
                members=list(members),
                mode="sweep" if is_sweep else "switch",
            )
        )
        if not is_sweep:
            inactive.update(cid for cid, _ in members if cid != active_cell_id)
    return resolutions, inactive


def _variant_notes(cells: list[CellAnalysis], dag: Dag, groups: GroupConfig) -> None:
    """Strata's variant warnings: diverging defines, an unknown mode or active name."""
    members = {res.group: res for res in dag.variant_groups}
    by_id = {c.id: c for c in cells}
    for cell in cells:
        if cell.member is None or cell.member.group not in members:
            continue
        group = cell.member.group
        origin, line = _member_origin(cell)
        siblings = [by_id[cid] for cid, _ in members[group].members if cid != cell.id]
        own = set(cell.defines) - _top_level_imports(cell.source)
        for sibling in siblings:
            theirs = set(sibling.defines) - _top_level_imports(sibling.source)
            if own != theirs:
                cell.note(
                    "variant_contract_mismatch",
                    Severity.WARN,
                    f"variant `{cell.member.name}` of `{group}` defines {sorted(own)}, "
                    f"its sibling `{sibling.member.name if sibling.member else '?'}` "
                    f"defines {sorted(theirs)}",
                    annotation=origin,
                    line=line,
                    args={
                        "group": group,
                        "name": cell.member.name,
                        "defines": sorted(own),
                        "sibling": sibling.member.name if sibling.member else None,
                        "sibling_defines": sorted(theirs),
                    },
                )
                break
        mode = groups.modes.get(group, "switch")
        if mode not in ("switch", "sweep"):
            cell.note(
                "variant_mode_invalid",
                Severity.WARN,
                f"group `{group}` has mode {mode!r}; it runs as a switch",
                annotation=origin,
                line=line,
                args={"group": group, "mode": mode},
            )
        active = groups.active.get(group)
        if mode == "sweep" and active:
            cell.note(
                "variant_active_redundant",
                Severity.INFO,
                f"group `{group}` is a sweep, so its active selection is ignored",
                annotation=origin,
                line=line,
                args={"group": group, "active": active},
            )
        if mode != "sweep" and active:
            names = [name for _, name in members[group].members]
            if active not in names:
                cell.note(
                    "variant_active_unknown",
                    Severity.WARN,
                    f"group `{group}` selects `{active}`, which no cell provides",
                    annotation=origin,
                    line=line,
                    args={"group": group, "active": active},
                )


def _wiring_phase(cells: list[CellAnalysis], groups: GroupConfig) -> Dag:
    """Phase 4: every reference to its latest earlier producer (``NotebookDag.from_cells``)."""
    dag = Dag()
    cell_ids = [c.id for c in cells]
    dag.variant_groups, dag.inactive_cells = _resolve_groups(cells, groups)
    inactive = dag.inactive_cells

    cell_by_id = {c.id: c for c in cells}
    sweep_producer_for_var: dict[str, SweepProducer] = {}
    sweep_member_group: dict[str, str] = {}
    for res in dag.variant_groups:
        if res.mode != "sweep":
            continue
        for cid, _ in res.members:
            sweep_member_group[cid] = res.group
        var_members: dict[str, list[tuple[str, str]]] = {}
        for cid, name in res.members:
            for var in cell_by_id[cid].defines:
                var_members.setdefault(var, []).append((name, cid))
        for var, members in var_members.items():
            sweep_producer_for_var[var] = SweepProducer(
                group=res.group, variants=tuple(sorted(members))
            )

    sweep_group_variant_names: dict[str, tuple[str, ...]] = {
        res.group: tuple(sorted(name for _, name in res.members))
        for res in dag.variant_groups
        if res.mode == "sweep"
    }

    for cell_id in cell_ids:
        dag.cell_upstream[cell_id] = []
        dag.cell_downstream[cell_id] = []
        dag.consumed_variables[cell_id] = set()

    # Wire each reference to the producer before this cell, then register this cell's
    # defines: a mutating cell reads the prior value and produces the new one.
    active_ids = set(cell_ids) - inactive
    for cell in cells:
        # An inactive member is not wired: what it would read depends on the selection,
        # so a host selects it and analyzes again (``InactiveVariantError``).
        if cell.id in inactive:
            continue
        for var in (*cell.references, *cell.builtin_references):
            if var in cell.seeds:
                continue
            producer = dag.variable_producer.get(var)
            if producer is None or producer == cell.id:
                continue
            if isinstance(producer, SweepProducer):
                if cell.id in {cid for _, cid in producer.variants}:
                    # A member refining its own group's variable does not read its siblings.
                    continue
            dag.read_producers.setdefault(cell.id, {})[var] = producer
            if isinstance(producer, SweepProducer):
                for _name, member_id in producer.variants:
                    _wire(dag, member_id, cell.id, var)
            else:
                _wire(dag, producer, cell.id, var)

        for name, seed_id in cell.seeds.items():
            seed = cell_by_id.get(seed_id)
            origin, origin_line = cell.seed_origins.get(name, (None, None))
            if seed is None:
                reason, message = "unknown", f"`{name}` is seeded from {seed_id!r}, no cell"
            elif seed_id == cell.id:
                reason, message = "self", f"`{name}` is seeded from the cell itself"
            elif seed_id in inactive:
                reason, message = (
                    "inactive",
                    f"`{name}` is seeded from {seed_id}, an inactive variant",
                )
            elif name not in seed.defines:
                reason = "undefined"
                message = f"`{name}` is seeded from {seed_id}, which does not define it"
            else:
                dag.read_producers.setdefault(cell.id, {})[name] = seed_id
                _wire(dag, seed_id, cell.id, name)
                continue
            cell.error(
                "seed_unknown_cell",
                message,
                annotation=origin,
                line=origin_line,
                args={"name": name, "cell_id": seed_id, "reason": reason},
            )

        dag.consumed_variables[cell.id].update(v for v in cell.kept if v in cell.defines)

        for upstream_id in cell.after:
            origin = cell.after_origins.get(upstream_id)
            line = _naming_line(cell.directives, origin, upstream_id) if origin else None
            if upstream_id == cell.id:
                cell.note(
                    "after_self",
                    Severity.WARN,
                    "`@after` names this cell",
                    annotation=origin,
                    line=line,
                    args={"cell_id": upstream_id},
                )
                continue
            if upstream_id not in cell_by_id:
                cell.error(
                    "after_unknown_cell",
                    f"`@after {upstream_id}` names no cell",
                    annotation=origin,
                    line=line,
                    args={"cell_id": upstream_id},
                )
                continue
            if upstream_id not in active_ids:
                cell.error(
                    "after_inactive_variant",
                    f"`@after {upstream_id}` names a variant that is not active",
                    annotation=origin,
                    line=line,
                    args={"cell_id": upstream_id},
                )
                continue
            dag.edges.append(DagEdge(from_cell_id=upstream_id, to_cell_id=cell.id, variable=""))
            if upstream_id not in dag.cell_upstream[cell.id]:
                dag.cell_upstream[cell.id].append(upstream_id)
            if cell.id not in dag.cell_downstream[upstream_id]:
                dag.cell_downstream[upstream_id].append(cell.id)

        is_sweep_member = cell.id in sweep_member_group
        fanout_group: str | None = None
        if cell.fanout is not None and cell.fanout.over is not None:
            if cell.member is None:
                dag.each_cells[cell.id] = cell.fanout.over
            _each_notes(cell, dag)
        elif cell.fanout is not None:
            read_groups = dag.sweep_groups_read(cell.id)
            named = cell.fanout.group
            if named is not None:
                fanout_group = named if named in read_groups else None
            else:
                fanout_group = next(iter(read_groups)) if len(read_groups) == 1 else None
            _fanout_notes(cell, read_groups, sweep_group_variant_names)
        if fanout_group is not None and cell.member is None:
            dag.fanout_cells[cell.id] = SweepProducer(
                group=fanout_group,
                variants=tuple(
                    sorted((name, cell.id) for name in sweep_group_variant_names[fanout_group])
                ),
                fanout_cell=cell.id,
            )
        for var in cell.defines:
            if is_sweep_member and var in sweep_producer_for_var:
                new_producer: str | SweepProducer = sweep_producer_for_var[var]
            elif cell.id in dag.fanout_cells:
                new_producer = dag.fanout_cells[cell.id]
            else:
                new_producer = cell.id
            previous = dag.variable_producer.get(var)
            if previous is not None and previous != new_producer:
                short = (previous.group if isinstance(previous, SweepProducer) else previous)[:8]
                dag.shadow_warnings.setdefault(cell.id, []).append(
                    f"Variable '{var}' shadows definition from cell {short}"
                )
            dag.variable_producer[var] = new_producer

    active_cell_ids = [cid for cid in cell_ids if cid not in inactive]
    for cell_id in active_cell_ids:
        if not dag.cell_downstream[cell_id]:
            dag.leaves.add(cell_id)
        if not dag.cell_upstream[cell_id]:
            dag.roots.add(cell_id)

    position = {cid: i for i, cid in enumerate(active_cell_ids)}
    cycles = [c for c in _strong_components(dag, active_cell_ids) if len(c) > 1]
    for component in cycles:
        # Each cycle is reported on its own cells, through the first of them.
        ordered = sorted(component, key=position.__getitem__)
        cycle = _cycle_through(dag, component, ordered[0])
        message = "Cycle detected in DAG: " + " → ".join(cycle)
        for cid in ordered:
            cell_by_id[cid].error("cycle", message, args={"cycle": cycle})
    dag.topological_order = _kahn(dag, active_cell_ids)
    return dag


def _each_notes(cell: CellAnalysis, dag: Dag) -> None:
    """What an ``@each`` cell cannot iterate, or be."""
    assert cell.fanout is not None and cell.fanout.over is not None
    source = cell.fanout.over
    origin, line = cell.fanout_origin
    if cell.member is not None:
        cell.error(
            "each_on_variant_member",
            "a variant member cannot also run once per item",
            annotation=origin,
            line=line,
        )
    if source in cell.bound:
        return
    producer = dag.read_producer(cell.id, source)
    if isinstance(producer, SweepProducer):
        cell.error(
            "each_over_sweep",
            f"`{source}` comes from a sweep, which has no one collection to iterate",
            annotation=origin,
            line=line,
            args={"source": source},
        )
    elif producer is None:
        cell.error(
            "each_unknown_source",
            f"no cell before this one defines `{source}`, and the cell does not bind it",
            annotation=origin,
            line=line,
            args={"source": source},
        )


def _fanout_notes(
    cell: CellAnalysis, read_groups: set[str], sizes: Mapping[str, tuple[str, ...]]
) -> None:
    """Strata's ``@per_variant`` warnings, against the sweep groups the cell reads."""
    assert cell.fanout is not None
    origin, line = cell.fanout_origin
    if cell.member is not None:
        cell.note(
            "per_variant_on_variant_member",
            Severity.WARN,
            "a variant member cannot also fan out over a group",
            annotation=origin,
            line=line,
        )
        return
    named = cell.fanout.group
    if named is not None:
        if named not in read_groups:
            cell.note(
                "per_variant_unknown_group",
                Severity.WARN,
                f"`@per_variant {named}` names no sweep group the cell reads",
                annotation=origin,
                line=line,
                args={"group": named},
            )
            return
        effective = named
    else:
        if not read_groups:
            cell.note(
                "per_variant_no_sweep_source",
                Severity.WARN,
                "`@per_variant` reads no sweep-produced variable",
                annotation=origin,
                line=line,
            )
            return
        if len(read_groups) > 1:
            cell.note(
                "per_variant_ambiguous_group",
                Severity.WARN,
                f"`@per_variant` reads {', '.join(sorted(read_groups))}; name one",
                annotation=origin,
                line=line,
                args={"groups": sorted(read_groups)},
            )
            return
        effective = next(iter(read_groups))
    if len(sizes.get(effective, ())) == 1:
        cell.note(
            "per_variant_group_of_one",
            Severity.INFO,
            f"group `{effective}` has one variant",
            annotation=origin,
            line=line,
            args={"group": effective},
        )


def _restrict(graph: Dag, keep: set[str]) -> Dag:
    """*graph* over the cells in *keep* only."""
    ids = [cid for cid in graph.cell_upstream if cid in keep]
    dag = Dag(
        edges=[e for e in graph.edges if e.from_cell_id in keep and e.to_cell_id in keep],
        cell_upstream={cid: [u for u in graph.cell_upstream[cid] if u in keep] for cid in ids},
        cell_downstream={cid: [d for d in graph.cell_downstream[cid] if d in keep] for cid in ids},
        variable_producer=dict(graph.variable_producer),
        consumed_variables={cid: set(graph.consumed_variables[cid]) for cid in ids},
        shadow_warnings={c: w for c, w in graph.shadow_warnings.items() if c in keep},
        variant_groups=list(graph.variant_groups),
        inactive_cells=set(graph.inactive_cells),
        wired_inputs={
            c: {p: set(v) for p, v in ins.items() if p in keep}
            for c, ins in graph.wired_inputs.items()
            if c in keep
        },
        fanout_cells={c: p for c, p in graph.fanout_cells.items() if c in keep},
        each_cells={c: s for c, s in graph.each_cells.items() if c in keep},
        read_producers={c: dict(r) for c, r in graph.read_producers.items() if c in keep},
    )
    active = [cid for cid in ids if cid not in graph.inactive_cells]
    dag.leaves = {cid for cid in active if not dag.cell_downstream[cid]}
    dag.roots = {cid for cid in active if not dag.cell_upstream[cid]}
    dag.topological_order = _kahn(dag, active)
    return dag


# --- the result -------------------------------------------------------------------


@dataclass
class Analysis:
    """What ``analyze`` returns."""

    cells: dict[str, CellAnalysis]
    graph: Dag
    dag: Dag
    vocabulary: Vocabulary
    analyzers: dict[str, LanguageAnalyzer]
    groups: GroupConfig

    @property
    def diagnostics(self) -> list[Diagnostic]:
        return [d for cell in self.cells.values() for d in cell.diagnostics]

    @property
    def structural_errors(self) -> list[Diagnostic]:
        return [d for d in self.diagnostics if d.code in STRUCTURAL_CODES]

    def blocking(self, cell_id: str) -> list[Diagnostic]:
        """The error diagnostics that keep *cell_id* from planning."""
        return [
            d
            for blocker in self.cells[cell_id].blocked_by
            for d in self.cells[blocker].diagnostics
            if d.severity == Severity.ERROR
        ]

    def execution_key(
        self,
        cell_id: str,
        *,
        instance: str | _keys.Item | None = None,
        published: Callable[[str], _keys.Published],
        resolve: Mapping[str, _keys.Resolver] | None = None,
        env_hash: str,
    ) -> _keys.Keyed | _keys.Unavailable | _keys.HostKeyed:
        """See ``cellgraph.keys.execution_key``."""
        return _keys.execution_key(
            self,
            cell_id,
            instance=instance,
            published=published,
            resolve=resolve,
            env_hash=env_hash,
        )

    def variant_read(
        self, cell_id: str, upstream_id: str, var: str, instance: str | None = None
    ) -> _keys.VariantRead | None:
        """See ``cellgraph.keys.variant_read``."""
        return _keys.variant_read(self, cell_id, upstream_id, var, instance)

    def shadowed_reads(self, cell_id: str) -> set[str]:
        """See ``cellgraph.keys.shadowed_reads``."""
        return _keys.shadowed_reads(self, cell_id)

    def plan(self, target: str, ready: Callable[[str], bool]) -> list[str]:
        """See ``cellgraph.plan.plan``."""
        return _plan.plan(self, target, ready)


def analyze(
    cells: Iterable[Cell],
    vocabulary: Vocabulary,
    groups: GroupConfig | None = None,
    *,
    strict: bool = False,
    analyzers: Mapping[str, LanguageAnalyzer] | None = None,
    context: Any = None,
) -> Analysis:
    """Analyze *cells*, given in source order.

    ``analyzers`` maps a language to its analyzer; the registry when omitted.
    ``context`` is passed to every validator as ``ValidationContext.host``. With
    ``strict=True`` an unknown directive key is an error rather than a warning.
    """
    table = dict(analyzers) if analyzers is not None else registered_analyzers()
    groups = groups or GroupConfig()
    cell_list = list(cells)
    # Cells are found by id, so a repeated one would silently replace the other.
    repeated = sorted(cid for cid, n in Counter(c.id for c in cell_list).items() if n > 1)
    if repeated:
        raise ValueError(f"cell ids must be unique; repeated: {repeated}")

    analyses: list[CellAnalysis] = []
    for cell in cell_list:
        if cell.language not in table:
            raise UnknownLanguageError(f"no language analyzer for {cell.language!r}")
        ca, analyzed = _parse_phase(cell, vocabulary, table[cell.language], strict)
        _names_phase(ca, analyzed)
        analyses.append(ca)

    for cell, ca in zip(cell_list, analyses, strict=True):
        for annotation in vocabulary:
            if annotation.validate is None or annotation.key not in ca.annotations:
                continue
            context_view = ValidationContext(
                cell=cell, defines=tuple(ca.defines), references=tuple(ca.references), host=context
            )
            try:
                found = annotation.validate(ca.annotations[annotation.key], context_view)
                ca.diagnostics.extend(replace(d, cell=ca.id) for d in found)
            except (AnnotationError, ExceptionGroup) as raised:
                first = next((d.line for d in ca.directives if d.key in annotation.keys), None)
                for exc in _annotation_errors(raised):
                    ca.error(
                        exc.code or f"{annotation.key}_invalid",
                        str(exc),
                        annotation=annotation.key,
                        line=exc.line if exc.line is not None else first,
                    )

    graph = _wiring_phase(analyses, groups)
    _variant_notes(analyses, graph, groups)

    by_id = {ca.id: ca for ca in analyses}
    # One pass over the components, upstream ones first: a component's blockers are
    # its own erroring cells and its upstreams' blockers.
    ids = [ca.id for ca in analyses]
    position = {cid: i for i, cid in enumerate(ids)}
    blockers_of: dict[str, set[str]] = {}
    for component in reversed(_strong_components(graph, ids)):
        inside = set(component)
        blockers = {cid for cid in component if by_id[cid].has_error}
        for cid in component:
            for upstream in graph.cell_upstream[cid]:
                if upstream not in inside:
                    blockers |= blockers_of[upstream]
        ordered = sorted(blockers, key=position.__getitem__)
        for cid in component:
            blockers_of[cid] = blockers
            target = by_id[cid]
            target.plannable = not blockers
            # Its own errors first, then the cells upstream that block it.
            target.blocked_by = [cid] * (cid in blockers) + [b for b in ordered if b != cid]

    plannable = {ca.id for ca in analyses if ca.plannable}
    analysis = Analysis(
        cells=by_id,
        graph=graph,
        dag=_restrict(graph, plannable),
        vocabulary=vocabulary,
        analyzers=table,
        groups=groups,
    )
    for ca in analyses:
        ca.normalized, ca.definition_key = _keys.definition_key(analysis, ca)
    return analysis
