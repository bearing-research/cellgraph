"""The definition key and the execution key (design 4.2, 4.3, 7).

Two independent axes: the **normalization** decides the text a definition key is
built from, the **digest** decides the bytes hashed. ``legacy-1`` on both is
Strata's format, byte for byte; ``cellgraph-1`` folds the format name into
canonical JSON.

``execution_key`` returns ``Keyed``, ``Unavailable`` or ``HostKeyed``, each with
``parts``: the key's components by stable address, versioned by
``PARTS_SCHEMA`` apart from the digest format (13.1).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from cellgraph import python
from cellgraph.effects import Input

if TYPE_CHECKING:
    from cellgraph.analysis import Analysis, CellAnalysis

PARTS_SCHEMA = 1


def derive_subkey(parent_hash: str, *labels: str) -> str:
    """``sha256("parent:label1:label2:...")``, byte-identical to vestigo's.

    Labels are joined by ``:`` with no escaping, so a label must not contain one.
    """
    for label in labels:
        if ":" in label:
            raise ValueError(f"label {label!r} contains ':', the label separator")
    return hashlib.sha256(":".join([parent_hash, *labels]).encode()).hexdigest()


def compute_provenance_hash(inputs: list[str], source_hash: str, env_hash: str) -> str:
    """The legacy execution digest: sorted inputs, the source hash and the env hash."""
    hasher = hashlib.sha256()
    for component in sorted(inputs):
        hasher.update(component.encode("utf-8"))
        hasher.update(b"\x00")
    hasher.update(source_hash.encode("utf-8"))
    hasher.update(b"\x00")
    hasher.update(env_hash.encode("utf-8"))
    return hasher.hexdigest()


def canonical_json(value: Any) -> str:
    """Sorted keys, no whitespace, UTF-8 as is, no NaN."""
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


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
class Resolved:
    """A resolver's answer for one input.

    ``identity`` names what is read and ``fingerprint`` its current content; under
    the ``legacy-1`` digest the component is ``legacy``, the host's own string.
    """

    identity: Any
    fingerprint: Any
    legacy: str | None = None


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
    """The host computes this cell's key (``owns_key=False``)."""

    parts: dict[str, Any] | None = None


# --- the definition key -----------------------------------------------------------


def definition_key(analysis: Analysis, cell: CellAnalysis) -> tuple[str, str]:
    """``(normalized text, definition key)`` for *cell*."""
    if analysis.normalization == "legacy-1":
        # Strata's compute_source_hash body: the whole source, whatever the language.
        normalized = python.normalize(cell.source)
    else:
        body = analysis.vocabulary.grammar.strip_directives(cell.source)
        normalized = analysis.analyzers[cell.language].normalize(body)
    fields = cell.key_fields
    if analysis.digest == "legacy-1":
        text = normalized
        if fields:
            text += "\x00" + json.dumps(fields, sort_keys=True, separators=(",", ":"))
        return normalized, _sha256(text)
    return normalized, _sha256(canonical_json(["cellgraph-1", "definition", normalized, fields]))


# --- the execution key ------------------------------------------------------------


@dataclass(frozen=True)
class _VariantRead:
    variants: tuple[str, ...]
    scalar: bool
    fanout: bool


def _variant_read(
    analysis: Analysis, cell_id: str, upstream_id: str, var: str, instance: str | None
) -> _VariantRead | None:
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
        return _VariantRead(variants=names, scalar=own, fanout=True)
    member = next((name for name, cid in producer.variants if cid == upstream_id), None)
    if member is None:
        return None
    if own:
        return _VariantRead(
            variants=(member,) if member == instance else (), scalar=True, fanout=False
        )
    return _VariantRead(variants=(member,), scalar=False, fanout=False)


class _Assembly:
    """Upstream components, by address, as Strata's ``_collect_input_hashes`` builds them."""

    def __init__(self, legacy: bool):
        self.legacy = legacy
        self.components: list[tuple[str, str]] = []
        self.unresolved: list[str] = []

    def component(self, ref: Any, by_content: bool, address: str) -> str | None:
        """One output's component, or None when the legacy format skips it."""
        if isinstance(ref, Unavailable) or ref is None:
            if self.legacy:
                return None
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
            if self.legacy:
                return provenance
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
) -> _Assembly:
    graph = analysis.graph
    cell = analysis.cells[cell_id]
    assembly = _Assembly(legacy=analysis.digest == "legacy-1")
    upstream_ids = graph.cell_upstream.get(cell_id, [])
    if not upstream_ids:
        return assembly

    reads = set(cell.references) | set(cell.builtin_references)
    # A name several upstreams define is read from its wired producer only.
    shadowed: set[str] = set()
    for upstream_id in upstream_ids:
        upstream = analysis.cells.get(upstream_id)
        if upstream is not None:
            shadowed |= (reads & set(upstream.defines)) - graph.wired_variables(
                cell_id, upstream_id
            )

    sweep_buckets: dict[str, list[tuple[str, str]]] = {}
    for upstream_id in upstream_ids:
        upstream = analysis.cells.get(upstream_id)
        if upstream is None:
            continue
        by_content = upstream.nocache
        wired = graph.wired_variables(cell_id, upstream_id) if shadowed else set()
        snapshot = published(upstream_id)

        items: list[tuple[str | None, Any]] = list(snapshot.outputs.items())
        if not items and snapshot.cell_ref is not None:
            items = [(None, snapshot.cell_ref)]

        for var, ref in items:
            if var is None:
                value = assembly.component(ref, by_content, f"cell:{upstream_id}")
                if value is not None:
                    assembly.add(f"cell:{upstream_id}", value)
                continue
            if by_content and var not in reads:
                continue
            read = _variant_read(analysis, cell_id, upstream_id, var, instance)
            if read is not None and read.fanout:
                parts = []
                for name in read.variants:
                    instance_ref = snapshot.instances.get((var, name))
                    if instance_ref is None:
                        continue  # not run yet
                    value = assembly.component(
                        instance_ref, by_content, f"output:{upstream_id}.{var}@{name}"
                    )
                    parts.append(f"{name}={value}")
                prefix = "variant" if read.scalar else "fanout"
                assembly.add(
                    f"instances:{upstream_id}.{var}", f"{prefix}:{var}:{';'.join(sorted(parts))}"
                )
                continue
            if read is not None and not read.variants:
                continue  # another variant's member
            value = assembly.component(ref, by_content, f"output:{upstream_id}.{var}")
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
        joined = ";".join(f"{name}={value}" for name, value in sorted(pairs))
        assembly.add(f"sweep:{var}", f"sweep:{var}:{joined}")
    return assembly


Resolver = Callable[[Input], "Resolved | Unavailable"]


def execution_key(
    analysis: Analysis,
    cell_id: str,
    *,
    instance: str | None = None,
    published: Callable[[str], Published],
    resolve: Mapping[str, Resolver] | None = None,
    env_hash: str,
) -> Keyed | Unavailable | HostKeyed:
    """The key that says whether *cell_id* (or one fan-out *instance*) must run.

    ``published(cell_id)`` is what an upstream published; ``resolve`` has one resolver
    per input kind. ``instance=None`` on a fan-out cell is its cell-level key.
    """
    from cellgraph.analysis import NotPlannableError

    cell = analysis.cells[cell_id]
    if not cell.plannable:
        raise NotPlannableError(cell_id, analysis.blocking(cell_id))
    if not analysis.analyzers[cell.language].owns_key:
        return HostKeyed()
    fanout = analysis.graph.fanout_cells.get(cell_id)
    if instance is not None:
        if fanout is None:
            raise ValueError(f"cell {cell_id} does not fan out, so it has no instance {instance!r}")
        if instance not in {name for name, _ in fanout.variants}:
            raise ValueError(f"cell {cell_id} has no instance {instance!r}")

    legacy = analysis.digest == "legacy-1"
    assembly = _upstream(analysis, cell_id, instance, published)
    inputs: list[tuple[str, str, str]] = []
    resolvers = resolve or {}
    for spec in cell.inputs:
        if spec.kind not in resolvers:
            raise ValueError(f"no resolver for {spec.kind!r} inputs (cell {cell_id})")
        result = resolvers[spec.kind](spec)
        address = f"input:{spec.kind}:{spec.name}"
        if isinstance(result, Unavailable):
            assembly.unresolved.append(address)
            continue
        if legacy:
            if result.legacy is None:
                raise ValueError(
                    f"the {spec.kind} resolver gave no legacy string for {spec.name!r}"
                )
            component = result.legacy
        else:
            component = canonical_json([spec.kind, spec.name, result.identity, result.fingerprint])
        inputs.append((spec.kind, spec.name, component))

    assert cell.definition_key is not None
    parts: dict[str, Any] = {
        "parts_schema": PARTS_SCHEMA,
        "digest": analysis.digest,
        "scope": "instance" if instance is not None else "cell",
        "instance": instance,
        "definition_key": cell.definition_key,
        "fields": dict(cell.key_fields),
        "env_hash": env_hash,
        "upstream": sorted([address, value] for address, value in assembly.components),
        "inputs": sorted([kind, name, component] for kind, name, component in inputs),
    }
    if assembly.unresolved:
        return Unavailable(
            reason="unresolved: " + ", ".join(assembly.unresolved),
            parts=parts,
            unresolved=tuple(assembly.unresolved),
        )

    components = [value for _, value in assembly.components] + [c for _, _, c in inputs]
    if legacy:
        digest = compute_provenance_hash(components, cell.definition_key, env_hash)
        if instance is not None:
            digest = derive_subkey(digest, f"variant={instance}")
    else:
        digest = _sha256(
            canonical_json(
                [
                    "cellgraph-1",
                    "execution",
                    cell.definition_key,
                    env_hash,
                    sorted(components),
                    parts["scope"],
                    instance,
                ]
            )
        )
    return Keyed(digest=digest, parts=parts)
