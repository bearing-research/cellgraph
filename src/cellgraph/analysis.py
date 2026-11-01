"""Cell analysis and the DAG (design 4.4 and 4.5).

``analyze`` runs the phases in a fixed order: parse, names, groups, wiring,
outputs, keys. It always returns an analysis an editor can show. A cell carries
``plannable``, which is false for its own errors, for a structural error it is
part of (a cycle, a variant collision) and for any cell it depends on that is not
plannable. ``Analysis.graph`` holds every candidate edge; ``Analysis.dag`` keeps
only plannable cells.

The wiring is Strata's ``NotebookDag.from_cells``, ported as is.
"""

from __future__ import annotations

import ast
import difflib
import math
from collections import deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

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
    reads_directives,
    registered_analyzers,
)
from cellgraph.vocabulary import AnnotationError, Partial, ValidationContext, Vocabulary

NORMALIZE_LEGACY_1 = "legacy-1"
NORMALIZE_CELLGRAPH_1 = "cellgraph-1"
DIGEST_LEGACY_1 = "legacy-1"
DIGEST_CELLGRAPH_1 = "cellgraph-1"

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


def _cycles(dag: Dag, cell_ids: list[str]) -> list[list[str]]:
    """Every cycle the depth-first walk finds, each as the ids along it (Strata's order)."""
    color = {cid: 0 for cid in cell_ids}
    cycles: list[list[str]] = []

    def dfs(node: str, path: list[str]) -> None:
        color[node] = 1
        path.append(node)
        for downstream in dag.cell_downstream[node]:
            if color.get(downstream) == 1:
                start = path.index(downstream)
                cycles.append(path[start:] + [downstream])
            elif color.get(downstream) == 0:
                dfs(downstream, path)
        path.pop()
        color[node] = 2

    for cid in cell_ids:
        if color[cid] == 0:
            dfs(cid, [])
    return cycles


def _on_cycles(dag: Dag, cell_ids: list[str]) -> set[str]:
    """The cells in a strongly connected component of more than one cell."""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    found: set[str] = set()
    counter = [0]
    members = set(cell_ids)

    def strong(v: str) -> None:
        index[v] = low[v] = counter[0]
        counter[0] += 1
        stack.append(v)
        on_stack.add(v)
        for w in dag.cell_downstream[v]:
            if w not in members:
                continue
            if w not in index:
                strong(w)
                low[v] = min(low[v], low[w])
            elif w in on_stack:
                low[v] = min(low[v], index[w])
        if low[v] == index[v]:
            component = []
            while True:
                w = stack.pop()
                on_stack.discard(w)
                component.append(w)
                if w == v:
                    break
            if len(component) > 1:
                found.update(component)

    for cid in cell_ids:
        if cid not in index:
            strong(cid)
    return found


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
    defines: list[str] = field(default_factory=list)
    references: list[str] = field(default_factory=list)
    builtin_references: list[str] = field(default_factory=list)
    mutation_defines: list[str] = field(default_factory=list)
    bound: list[str] = field(default_factory=list)
    after: list[str] = field(default_factory=list)
    seeds: dict[str, str] = field(default_factory=dict)
    member: Member | None = None
    fanout: FanOut | None = None
    kept: list[str] = field(default_factory=list)
    inputs: list[Input] = field(default_factory=list)
    nocache: bool = False
    key_fields: dict[str, Any] = field(default_factory=dict)
    diagnostics: list[Diagnostic] = field(default_factory=list)
    plannable: bool = True
    # The cells whose own errors keep this one from planning (itself included).
    blocked_by: list[str] = field(default_factory=list)
    definition_key: str | None = None
    normalized: str | None = None

    def error(self, code: str, message: str, *, annotation: str | None = None, line=None):
        self.diagnostics.append(
            Diagnostic(code, Severity.ERROR, message, self.id, annotation, line)
        )

    def note(
        self,
        code: str,
        severity: Severity,
        message: str,
        *,
        annotation: str | None = None,
        line=None,
    ):
        self.diagnostics.append(Diagnostic(code, severity, message, self.id, annotation, line))

    @property
    def has_error(self) -> bool:
        return any(d.severity == Severity.ERROR for d in self.diagnostics)


def _is_canonical(value: Any) -> bool:
    """None, bool, int, finite float, str, and lists and str-keyed dicts of these."""
    if value is None or isinstance(value, bool | int | str):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, list):
        return all(_is_canonical(item) for item in value)
    if isinstance(value, dict):
        return all(isinstance(k, str) and _is_canonical(v) for k, v in value.items())
    return False


def _annotation_errors(exc: AnnotationError | ExceptionGroup) -> list[AnnotationError]:
    """The errors a parser or validator raised: one, or an ``ExceptionGroup`` of them."""
    if isinstance(exc, AnnotationError):
        return [exc]
    matched, rest = exc.split(AnnotationError)
    if rest is not None:
        raise rest
    assert matched is not None
    return _leaves(matched)


def _leaves(group: BaseExceptionGroup) -> list[AnnotationError]:
    out: list[AnnotationError] = []
    for exc in group.exceptions:
        if isinstance(exc, BaseExceptionGroup):
            out.extend(_leaves(exc))
        elif isinstance(exc, AnnotationError):
            out.append(exc)
    return out


def _first_line(directives: list[Directive], key: str) -> int | None:
    return next((d.line for d in directives if d.key == key), None)


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
    if reads_directives(analyzer):
        ca.directives = vocabulary.grammar.directives(cell.source)

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
                    exc.code or f"{directives[0].key}_unreadable",
                    str(exc),
                    annotation=annotation.key,
                    line=exc.line if exc.line is not None else directives[0].line,
                )
            continue
        if isinstance(value, Partial):
            for exc in value.errors:
                ca.error(
                    exc.code or f"{directives[0].key}_unreadable",
                    str(exc),
                    annotation=annotation.key,
                    line=exc.line if exc.line is not None else directives[0].line,
                )
            value = value.value
        ca.annotations[annotation.key] = value
        if annotation.in_key:
            encoded = annotation.encode(value) if annotation.encode is not None else value
            if not _is_canonical(encoded):
                ca.error(
                    f"{annotation.key}_unencodable",
                    f"`@{annotation.key}` has a value the key cannot encode "
                    f"({type(encoded).__name__}); the annotation needs an encoder",
                    annotation=annotation.key,
                    line=directives[0].line,
                )
            else:
                ca.key_fields[annotation.key] = encoded
        if annotation.effects is not None:
            ca.effects.extend(annotation.effects(value))

    if vocabulary.cell_effects is not None:
        ca.effects.extend(vocabulary.cell_effects(cell.meta, dict(ca.annotations)))
    for effect in ca.effects:
        if not isinstance(effect, EFFECT_TYPES):
            raise TypeError(f"{effect!r} is not one of the ten effects")

    analyzed = analyzer.analyze(cell)
    if analyzed.error is not None:
        ca.error("syntax_error", analyzed.error)
    return ca, analyzed


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
    for effect in ca.effects:
        if isinstance(effect, Binds):
            for name in effect.names:
                if name in bound:
                    ca.error("bind_conflict", f"`{name}` is bound twice")
                else:
                    bound.append(name)
    for name in sorted(set(bound) & set(read)):
        ca.error("bind_read_conflict", f"`{name}` is both bound and read from upstream")

    ca.defines = defines
    ca.references = [name for name in references if name not in bound]
    ca.builtin_references = [name for name in analyzed.builtin_references if name not in bound]
    ca.mutation_defines = list(analyzed.mutation_defines)
    ca.bound = bound

    for effect in ca.effects:
        if isinstance(effect, After):
            for cell_id in effect.cell_ids:
                if cell_id not in ca.after:
                    ca.after.append(cell_id)
        elif isinstance(effect, Seeds):
            if effect.name in ca.seeds and ca.seeds[effect.name] != effect.cell_id:
                ca.error("seed_conflict", f"`{effect.name}` is seeded from two cells")
            else:
                ca.seeds[effect.name] = effect.cell_id
            if effect.name in bound:
                ca.error("bind_read_conflict", f"`{effect.name}` is both bound and seeded")
        elif isinstance(effect, Member):
            if ca.member is not None and ca.member != effect:
                ca.error("member_conflict", "the cell is a member of two variants")
            ca.member = effect
        elif isinstance(effect, FanOut):
            if ca.fanout is not None and ca.fanout != effect:
                ca.error("fanout_conflict", "the cell fans out over two groups")
            ca.fanout = effect
        elif isinstance(effect, Keeps):
            ca.kept.extend(name for name in effect.names if name not in ca.kept)
        elif isinstance(effect, Input):
            if any((i.kind, i.name) == (effect.kind, effect.name) for i in ca.inputs):
                ca.error(
                    "input_conflict", f"two {effect.kind} inputs have the address {effect.name!r}"
                )
            else:
                ca.inputs.append(effect)
        elif isinstance(effect, NoCache):
            ca.nocache = True


def _resolve_groups(
    cells: list[CellAnalysis], groups: GroupConfig
) -> tuple[list[VariantGroupResolution], set[str]]:
    """Phase 3: each variant group's members and its active one; collisions are errors."""
    sweep = {g for g, m in groups.modes.items() if m == "sweep"}
    grouped: dict[str, list[tuple[str, str]]] = {}
    group_order: list[str] = []
    by_id = {c.id: c for c in cells}
    for cell in cells:
        if cell.member is None:
            continue
        group, name = cell.member.group, cell.member.name
        members = grouped.setdefault(group, [])
        clash = next((cid for cid, n in members if n == name), None)
        if clash is not None:
            message = (
                f"Variant name '{name}' is used by both cell {clash[:8]} and cell "
                f"{cell.id[:8]} in group '{group}'"
            )
            for cid in (clash, cell.id):
                by_id[cid].error("variant_collision", message, annotation="variant")
            continue
        if not members:
            group_order.append(group)
        members.append((cell.id, name))

    resolutions: list[VariantGroupResolution] = []
    inactive: set[str] = set()
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
        line = _first_line(cell.directives, "variant")
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
                    annotation="variant",
                    line=line,
                )
                break
        mode = groups.modes.get(group, "switch")
        if mode not in ("switch", "sweep"):
            cell.note(
                "variant_mode_invalid",
                Severity.WARN,
                f"group `{group}` has mode {mode!r}; it runs as a switch",
                annotation="variant",
                line=line,
            )
        active = groups.active.get(group)
        if mode == "sweep" and active:
            cell.note(
                "variant_active_redundant",
                Severity.INFO,
                f"group `{group}` is a sweep, so its active selection is ignored",
                annotation="variant",
                line=line,
            )
        if mode != "sweep" and active:
            names = [name for _, name in members[group].members]
            if active not in names:
                cell.note(
                    "variant_active_unknown",
                    Severity.WARN,
                    f"group `{group}` selects `{active}`, which no cell provides",
                    annotation="variant",
                    line=line,
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
            if seed is None:
                cell.error("seed_unknown_cell", f"`{name}` is seeded from {seed_id!r}, no cell")
            elif seed_id == cell.id:
                cell.error("seed_unknown_cell", f"`{name}` is seeded from the cell itself")
            elif seed_id in inactive:
                cell.error(
                    "seed_unknown_cell", f"`{name}` is seeded from {seed_id}, an inactive variant"
                )
            elif name not in seed.defines:
                cell.error(
                    "seed_unknown_cell",
                    f"`{name}` is seeded from {seed_id}, which does not define it",
                )
            else:
                dag.read_producers.setdefault(cell.id, {})[name] = seed_id
                _wire(dag, seed_id, cell.id, name)

        dag.consumed_variables[cell.id].update(v for v in cell.kept if v in cell.defines)

        for upstream_id in cell.after:
            line = _first_line(cell.directives, "after")
            if upstream_id == cell.id:
                cell.note(
                    "after_self",
                    Severity.WARN,
                    "`@after` names this cell",
                    annotation="after",
                    line=line,
                )
                continue
            if upstream_id not in cell_by_id:
                cell.error(
                    "after_unknown_cell",
                    f"`@after {upstream_id}` names no cell",
                    annotation="after",
                    line=line,
                )
                continue
            if upstream_id not in active_ids:
                cell.error(
                    "after_inactive_variant",
                    f"`@after {upstream_id}` names a variant that is not active",
                    annotation="after",
                    line=line,
                )
                continue
            dag.edges.append(DagEdge(from_cell_id=upstream_id, to_cell_id=cell.id, variable=""))
            if upstream_id not in dag.cell_upstream[cell.id]:
                dag.cell_upstream[cell.id].append(upstream_id)
            if cell.id not in dag.cell_downstream[upstream_id]:
                dag.cell_downstream[upstream_id].append(cell.id)

        is_sweep_member = cell.id in sweep_member_group
        fanout_group: str | None = None
        if cell.fanout is not None:
            read_groups = dag.sweep_groups_read(cell.id)
            named = cell.fanout.group
            if named is not None:
                fanout_group = named if named in read_groups else None
            else:
                fanout_group = next(iter(read_groups)) if len(read_groups) == 1 else None
            _fanout_notes(cell, read_groups, sweep_group_variant_names)
        if fanout_group is not None and not is_sweep_member:
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

    on_cycle = _on_cycles(dag, active_cell_ids)
    if on_cycle:
        cycles = _cycles(dag, active_cell_ids)
        message = "Cycle detected in DAG: " + (" → ".join(cycles[0]) if cycles else "unknown")
        for cid in active_cell_ids:
            if cid in on_cycle:
                cell_by_id[cid].error("cycle", message)
    dag.topological_order = _kahn(dag, active_cell_ids)
    return dag


def _fanout_notes(
    cell: CellAnalysis, read_groups: set[str], sizes: Mapping[str, tuple[str, ...]]
) -> None:
    """Strata's ``@per_variant`` warnings, against the sweep groups the cell reads."""
    assert cell.fanout is not None
    line = _first_line(cell.directives, "per_variant")
    if cell.member is not None:
        cell.note(
            "per_variant_on_variant_member",
            Severity.WARN,
            "a variant member cannot also fan out over a group",
            annotation="per_variant",
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
                annotation="per_variant",
                line=line,
            )
            return
        effective = named
    else:
        if not read_groups:
            cell.note(
                "per_variant_no_sweep_source",
                Severity.WARN,
                "`@per_variant` reads no sweep-produced variable",
                annotation="per_variant",
                line=line,
            )
            return
        if len(read_groups) > 1:
            cell.note(
                "per_variant_ambiguous_group",
                Severity.WARN,
                f"`@per_variant` reads {', '.join(sorted(read_groups))}; name one",
                annotation="per_variant",
                line=line,
            )
            return
        effective = next(iter(read_groups))
    if len(sizes.get(effective, ())) == 1:
        cell.note(
            "per_variant_group_of_one",
            Severity.INFO,
            f"group `{effective}` has one variant",
            annotation="per_variant",
            line=line,
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
    normalization: str
    digest: str

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

    def execution_key(self, cell_id: str, **kwargs: Any) -> Any:
        """See ``cellgraph.keys.execution_key``."""
        from cellgraph.keys import execution_key

        return execution_key(self, cell_id, **kwargs)

    def plan(self, target: str, ready: Callable[[str], bool]) -> list[str]:
        """See ``cellgraph.plan.plan``."""
        from cellgraph.plan import plan

        return plan(self, target, ready)


def analyze(
    cells: Iterable[Cell],
    vocabulary: Vocabulary,
    groups: GroupConfig | None = None,
    normalization: str = NORMALIZE_LEGACY_1,
    digest: str = DIGEST_LEGACY_1,
    strict: bool = False,
    *,
    analyzers: Mapping[str, LanguageAnalyzer] | None = None,
    context: Any = None,
) -> Analysis:
    """Analyze *cells*, given in source order.

    ``analyzers`` maps a language to its analyzer; the registry when omitted.
    ``context`` is passed to every validator as ``ValidationContext.host``. With
    ``strict=True`` an unknown directive key is an error rather than a warning.
    """
    from cellgraph.keys import definition_key

    if normalization not in (NORMALIZE_LEGACY_1, NORMALIZE_CELLGRAPH_1):
        raise ValueError(f"unknown normalization {normalization!r}")
    if digest not in (DIGEST_LEGACY_1, DIGEST_CELLGRAPH_1):
        raise ValueError(f"unknown digest {digest!r}")
    table = dict(analyzers) if analyzers is not None else registered_analyzers()
    groups = groups or GroupConfig()
    cell_list = list(cells)

    analyses: list[CellAnalysis] = []
    for cell in cell_list:
        if cell.language not in table:
            from cellgraph.languages import UnknownLanguageError

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
                ca.diagnostics.extend(
                    annotation.validate(ca.annotations[annotation.key], context_view)
                )
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
    for ca in analyses:
        if ca.has_error:
            ca.plannable = False
            ca.blocked_by = [ca.id]
    for blocker in [ca.id for ca in analyses if not ca.plannable]:
        queue: deque[str] = deque(graph.cell_downstream.get(blocker, []))
        seen: set[str] = set()
        while queue:
            current = queue.popleft()
            if current in seen:
                continue
            seen.add(current)
            target = by_id[current]
            target.plannable = False
            if blocker not in target.blocked_by:
                target.blocked_by.append(blocker)
            queue.extend(graph.cell_downstream.get(current, []))

    plannable = {ca.id for ca in analyses if ca.plannable}
    analysis = Analysis(
        cells=by_id,
        graph=graph,
        dag=_restrict(graph, plannable),
        vocabulary=vocabulary,
        analyzers=table,
        groups=groups,
        normalization=normalization,
        digest=digest,
    )
    for ca in analyses:
        if ca.plannable:
            ca.normalized, ca.definition_key = definition_key(analysis, ca)
    return analysis
