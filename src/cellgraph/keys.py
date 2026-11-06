"""The definition key and the execution key (design 4.2, 4.3, 7).

The definition key hashes the cell's language, its body without directive lines as
the language normalizes it, and its key fields. ``execution_key`` returns ``Keyed``,
``Unavailable`` or ``HostKeyed``, each with ``parts``: the key's components by stable
address, versioned by ``PARTS_SCHEMA``. A key is ``parts_digest(parts)``, so what
``parts`` shows is exactly what the key covers.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from cellgraph.effects import Input
from cellgraph.languages import directive_keys

if TYPE_CHECKING:
    from cellgraph.analysis import Analysis, CellAnalysis

PARTS_SCHEMA = 2
# The definition key's own format, so an execution-only change to ``parts`` keeps it.
_DEFINITION_FORMAT = 1


def derive_subkey(parent_hash: str, *labels: str) -> str:
    """``sha256("parent:label1:label2:...")``, byte-identical to vestigo's.

    Labels are joined by ``:`` with no escaping, so a label must not contain one.
    """
    for label in labels:
        if ":" in label:
            raise ValueError(f"label {label!r} contains ':', the label separator")
    return hashlib.sha256(":".join([parent_hash, *labels]).encode()).hexdigest()


def is_canonical(value: Any) -> bool:
    """None, bool, int, finite float, str, and lists and str-keyed dicts of these.

    Exactly what JSON round-trips: ``json`` would write ``{1: "a"}`` as ``{"1": "a"}``
    and a tuple as a list, so two values would share a key.
    """
    if value is None or isinstance(value, bool | int | str):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, list):
        return all(is_canonical(item) for item in value)
    if isinstance(value, dict):
        return all(isinstance(k, str) and is_canonical(v) for k, v in value.items())
    return False


def canonical_json(value: Any) -> str:
    """Sorted keys, no whitespace, UTF-8 as is; a value ``is_canonical`` refuses is a TypeError."""
    if not is_canonical(value):
        raise TypeError(f"not a canonical JSON value: {value!r}")
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def parts_digest(parts: Mapping[str, Any]) -> str:
    """The key of *parts*: ``sha256(canonical_json(parts))``.

    A host keying a ``HostKeyed`` cell adds its own components to ``parts`` and calls
    this, so its keys are built the way cellgraph builds its own.
    """
    return _sha256(canonical_json(dict(parts)))


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --- what the host supplies -------------------------------------------------------


@dataclass(frozen=True)
class Provenance:
    """An output filed under a provenance hash."""

    hash: str


@dataclass(frozen=True)
class Run:
    """An output of a run that had no reusable key (4.3), named by the run."""

    id: str


@dataclass(frozen=True)
class OutputRef:
    """One published output: its identity, and its content digest when known."""

    identity: Provenance | Run
    content_digest: str | None = None


@dataclass(frozen=True)
class Published:
    """What an upstream cell published (7.3).

    - not published: the variable is absent from ``outputs``;
    - published but unreadable: present as ``Unavailable``;
    - the older cell-level representation: ``outputs`` empty and ``cell_ref`` set;
    - a fan-out cell: each variable in ``outputs`` (any instance's ref, as Strata keeps
      the last one stored) and each ``(variable, variant)`` in ``instances``. A
      variable missing from ``outputs`` is not read, whatever ``instances`` holds.
    """

    outputs: Mapping[str, OutputRef | Unavailable] = field(default_factory=dict)
    instances: Mapping[tuple[str, str], OutputRef | Unavailable] = field(default_factory=dict)
    cell_ref: OutputRef | Unavailable | None = None


@dataclass(frozen=True)
class Item:
    """One item of an ``@each`` cell: what the host calls it, and its current content.

    The key is the instance's name, so it must be stable across runs; the digest is
    what makes the instance run again.
    """

    key: str
    digest: str


@dataclass(frozen=True)
class Resolved:
    """A resolver's answer for one input.

    ``identity`` names what is read and ``fingerprint`` its current content; both must
    be canonical JSON values.
    """

    identity: Any
    fingerprint: Any


# --- what execution_key returns ---------------------------------------------------


@dataclass(frozen=True)
class Keyed:
    digest: str
    parts: dict[str, Any]


@dataclass(frozen=True)
class Unavailable:
    """No reusable key: an input or an upstream output could not be resolved.

    Also what a resolver or a ``Published`` entry returns for one it cannot read.
    As a result, ``parts`` holds what could be built and ``unresolved`` the addresses
    that could not.
    """

    reason: str = ""
    parts: dict[str, Any] | None = None
    unresolved: tuple[str, ...] = ()


@dataclass(frozen=True)
class HostKeyed:
    """The host computes this cell's key (``owns_key=False``).

    ``parts`` holds the upstream components the cell reads, as a key would, and no
    inputs: those are the host's to key. ``unresolved`` is the addresses that could not
    be read; with any, the host has no sound key either.
    """

    parts: dict[str, Any] | None = None
    unresolved: tuple[str, ...] = ()


# --- the definition key -----------------------------------------------------------


def definition_key(analysis: Analysis, cell: CellAnalysis) -> tuple[str, str]:
    """``(normalized text, definition key)`` for *cell*."""
    analyzer = analysis.analyzers[cell.language]
    grammar = analysis.vocabulary.grammar_for(analyzer)
    body = grammar.strip_directives(cell.source, keys=directive_keys(analyzer))
    normalized = analyzer.normalize(body)
    # The same text in two languages is two programs.
    text = canonical_json(
        ["definition", _DEFINITION_FORMAT, cell.language, normalized, cell.key_fields]
    )
    return normalized, _sha256(text)


# --- the execution key ------------------------------------------------------------


@dataclass(frozen=True)
class VariantRead:
    """What a cell reads of one variable a sweep makes: the variants, and how.

    ``scalar``: a fan-out instance's own group, read as that variant's value; otherwise
    the ``{variant: value}`` dict. ``fanout``: the variants are one fan-out cell's
    instances rather than the members of a group. No variants: another variant's
    member, which this cell does not read.
    """

    variants: tuple[str, ...]
    scalar: bool
    fanout: bool


def variant_read(
    analysis: Analysis, cell_id: str, upstream_id: str, var: str, instance: str | None = None
) -> VariantRead | None:
    """What *cell_id* (or its *instance*) reads of *var* from *upstream_id*, if a sweep makes it."""
    from cellgraph.analysis import SweepProducer

    graph = analysis.graph
    producer = graph.read_producer(cell_id, var)
    if not isinstance(producer, SweepProducer):
        return None
    own_fanout = graph.fanout_cells.get(cell_id)
    own = instance is not None and own_fanout is not None and producer.group == own_fanout.group
    if producer.fanout_cell is not None:
        if producer.fanout_cell != upstream_id:
            return None
        names = (
            (instance,) if own and instance is not None else tuple(n for n, _ in producer.variants)
        )
        return VariantRead(variants=names, scalar=own, fanout=True)
    member = next((name for name, cid in producer.variants if cid == upstream_id), None)
    if member is None:
        return None
    if own:
        return VariantRead(
            variants=(member,) if member == instance else (), scalar=True, fanout=False
        )
    return VariantRead(variants=(member,), scalar=False, fanout=False)


def shadowed_reads(analysis: Analysis, cell_id: str) -> set[str]:
    """Names *cell_id* reads that an upstream defines but is not wired to give it.

    Such a name is read from its wired producer only.
    """
    graph = analysis.graph
    cell = analysis.cells[cell_id]
    reads = set(cell.references) | set(cell.builtin_references)
    shadowed: set[str] = set()
    for upstream_id in graph.cell_upstream.get(cell_id, []):
        upstream = analysis.cells.get(upstream_id)
        if upstream is not None:
            shadowed |= (reads & set(upstream.defines)) - graph.wired_variables(
                cell_id, upstream_id
            )
    return shadowed


class _Assembly:
    """Upstream components, by address."""

    def __init__(self) -> None:
        self.components: list[tuple[str, str]] = []
        self.unresolved: list[str] = []

    def component(self, ref: Any, by_content: bool, address: str) -> str | None:
        """One output's component, or None when it cannot be read (then *address* is unresolved)."""
        if isinstance(ref, Unavailable) or ref is None:
            self.unresolved.append(address)
            return None
        if isinstance(ref.identity, Run):
            if ref.content_digest is not None:
                return f"content:{ref.content_digest}"
            return f"run:{ref.identity.id}"
        provenance = ref.identity.hash
        if not by_content:
            return provenance
        if ref.content_digest is None:
            # A no-cache output is keyed by its bytes; its provenance says nothing of them.
            self.unresolved.append(address)
            return None
        return derive_subkey(provenance, f"content={ref.content_digest}")

    def add(self, address: str, value: str) -> None:
        self.components.append((address, value))


def _upstream(
    analysis: Analysis,
    cell_id: str,
    instance: str | None,
    published: Callable[[str], Published],
    without: str | None = None,
) -> _Assembly:
    graph = analysis.graph
    cell = analysis.cells[cell_id]
    assembly = _Assembly()
    upstream_ids = graph.cell_upstream.get(cell_id, [])
    if not upstream_ids:
        return assembly

    reads = set(cell.references) | set(cell.builtin_references)
    shadowed = shadowed_reads(analysis, cell_id)

    sweep_buckets: dict[str, list[tuple[str, str]]] = {}
    for upstream_id in upstream_ids:
        upstream = analysis.cells.get(upstream_id)
        if upstream is None:
            continue
        by_content = upstream.nocache
        wired = graph.wired_variables(cell_id, upstream_id) if shadowed else set()
        snapshot = published(upstream_id)

        names = list(snapshot.outputs)
        if not names and snapshot.cell_ref is not None:
            value = assembly.component(snapshot.cell_ref, by_content, f"cell:{upstream_id}")
            if value is not None:
                assembly.add(f"cell:{upstream_id}", value)

        # An output is looked up only once it counts, so a host's mapping can be lazy.
        for var in names:
            if by_content and var not in reads:
                continue
            if var == without:
                continue
            read = variant_read(analysis, cell_id, upstream_id, var, instance)
            if read is not None and read.fanout:
                parts: list[list[str]] = []
                for name in read.variants:
                    instance_ref = snapshot.instances.get((var, name))
                    if instance_ref is None:
                        continue  # not run yet
                    value = assembly.component(
                        instance_ref, by_content, f"output:{upstream_id}.{var}@{name}"
                    )
                    if value is not None:
                        parts.append([name, value])
                # A list, not a joined string: a host's run id may hold any separator.
                prefix = "variant" if read.scalar else "fanout"
                assembly.add(
                    f"instances:{upstream_id}.{var}", canonical_json([prefix, var, sorted(parts)])
                )
                continue
            if read is not None and not read.variants:
                continue  # another variant's member
            value = assembly.component(
                snapshot.outputs[var], by_content, f"output:{upstream_id}.{var}"
            )
            if value is None:
                continue
            if read is not None:
                for name in read.variants:
                    if read.scalar:
                        assembly.add(
                            f"variant:{upstream_id}.{var}@{name}", f"variant:{var}:{name}={value}"
                        )
                    else:
                        sweep_buckets.setdefault(var, []).append((name, value))
                continue
            assembly.add(f"output:{upstream_id}.{var}", value)
            if var in shadowed and var in wired:
                assembly.add(f"wired:{upstream_id}.{var}", f"wired:{var}={value}")

    for var, pairs in sweep_buckets.items():
        members = sorted([name, value] for name, value in pairs)
        assembly.add(f"sweep:{var}", canonical_json(["sweep", var, members]))
    return assembly


Resolver = Callable[[Input], "Resolved | Unavailable"]


def execution_key(
    analysis: Analysis,
    cell_id: str,
    *,
    instance: str | Item | None = None,
    published: Callable[[str], Published],
    resolve: Mapping[str, Resolver] | None = None,
    env_hash: str,
) -> Keyed | Unavailable | HostKeyed:
    """The key that says whether *cell_id* (or one fan-out *instance*) must run.

    ``published(cell_id)`` is what an upstream published; ``resolve`` has one resolver
    per input kind. ``instance=None`` on a fan-out cell is its cell-level key. An ``@each``
    cell's instance is an :class:`Item`, keyed on that item in place of its source, so
    changing one item leaves every other item's key alone.
    """
    from cellgraph.analysis import STRUCTURAL_CODES, InactiveVariantError, NotPlannableError

    if cell_id in analysis.graph.inactive_cells:
        raise InactiveVariantError(cell_id)  # its upstream depends on the selection
    cell = analysis.cells[cell_id]
    # A blocked cell is keyed too: a result cached under its key is still its result.
    # One a cycle or a variant collision reaches has no sound upstreams to key it on.
    structural = [
        d
        for blocker in cell.blocked_by
        for d in analysis.cells[blocker].diagnostics
        if d.code in STRUCTURAL_CODES
    ]
    if structural:
        raise NotPlannableError(cell_id, structural)
    owns_key = analysis.analyzers[cell.language].owns_key
    fanout = analysis.graph.fanout_cells.get(cell_id)
    source = analysis.graph.each_cells.get(cell_id)
    item = instance if isinstance(instance, Item) else None
    if item is not None:
        if source is None:
            raise ValueError(f"cell {cell_id} has no items, so it has no item {item.key!r}")
        instance = item.key
    elif instance is not None:
        if source is not None:
            raise ValueError(f"cell {cell_id} runs per item; {instance!r} is not an Item")
        if fanout is None:
            raise ValueError(f"cell {cell_id} does not fan out, so it has no instance {instance!r}")
        if instance not in {name for name, _ in fanout.variants}:
            raise ValueError(f"cell {cell_id} has no instance {instance!r}")

    if item is not None:
        assembly = _upstream(analysis, cell_id, None, published, without=source)
        assembly.add(f"each:{source}", f"each:{item.key}={item.digest}")
    else:
        assembly = _upstream(analysis, cell_id, instance, published)
    inputs: list[tuple[str, str, str]] = []
    resolvers = resolve or {}
    # A host-keyed cell's inputs are the host's to key. An item stands in for its source.
    for spec in cell.inputs if owns_key else []:
        if item is not None and spec.name == source:
            continue
        if spec.kind not in resolvers:
            raise ValueError(f"no resolver for {spec.kind!r} inputs (cell {cell_id})")
        result = resolvers[spec.kind](spec)
        address = f"input:{spec.kind}:{spec.name}"
        if isinstance(result, Unavailable):
            assembly.unresolved.append(address)
            continue
        component = canonical_json([spec.kind, spec.name, result.identity, result.fingerprint])
        inputs.append((spec.kind, spec.name, component))

    # A field missing from the definition key leaves no sound key.
    assembly.unresolved.extend(f"field:{key}" for key in cell.unkeyed)

    assert cell.definition_key is not None
    parts: dict[str, Any] = {
        "parts_schema": PARTS_SCHEMA,
        "scope": "instance" if instance is not None else "cell",
        "instance": instance,
        "definition_key": cell.definition_key,
        "fields": dict(cell.key_fields),
        "env_hash": env_hash,
        "upstream": sorted([address, value] for address, value in assembly.components),
        "inputs": sorted([kind, name, component] for kind, name, component in inputs),
    }
    if not owns_key:
        return HostKeyed(parts=parts, unresolved=tuple(assembly.unresolved))
    if assembly.unresolved:
        return Unavailable(
            reason="unresolved: " + ", ".join(assembly.unresolved),
            parts=parts,
            unresolved=tuple(assembly.unresolved),
        )
    return Keyed(digest=parts_digest(parts), parts=parts)
