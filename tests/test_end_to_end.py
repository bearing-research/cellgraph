"""The brief's scenarios over the core vocabulary (plus the test host's effects)."""

import pytest
from conftest import codes, edges
from test_keys import ENV, out, published, strata_provenance
from vestigo.provenance import derive_subkey as vestigo_subkey

import cellgraph as cg

SWEEP = cg.GroupConfig(modes={"g": "sweep"})
MEMBERS = ("# @variant g small\nm = 1", "# @variant g big\nm = 2")


# --- variants -----------------------------------------------------------------


def test_a_switch_wires_the_active_member_only(run):
    default = run(*MEMBERS, "r = m")
    assert edges(default) == [("c0", "c2", "m")]
    assert default.graph.inactive_cells == {"c1"}
    chosen = run(*MEMBERS, "r = m", groups=cg.GroupConfig(active={"g": "big"}))
    assert edges(chosen) == [("c1", "c2", "m")]
    assert chosen.graph.inactive_cells == {"c0"}
    assert codes(run(*MEMBERS, "r = m", groups=cg.GroupConfig(active={"g": "huge"}))) == [
        "variant_active_unknown",
        "variant_active_unknown",
    ]


def test_a_sweep_reader_reads_every_member(run):
    analysis = run(*MEMBERS, "r = m", groups=SWEEP)
    assert edges(analysis) == [("c0", "c2", "m"), ("c1", "c2", "m")]
    key = analysis.execution_key(
        "c2", published=published(c0=out(m="h0"), c1=out(m="h1")), env_hash=ENV
    )
    assert key.parts["upstream"] == [["sweep:m", "sweep:m:big=h1;small=h0"]]
    assert key.digest == strata_provenance(
        ["sweep:m:big=h1;small=h0"], analysis.cells["c2"].definition_key, ENV
    )


def test_a_variant_collision_is_structural_on_both_cells(run):
    analysis = run("# @variant g a\nm = 1", "# @variant g a\nm = 2", "r = m")
    assert [(d.cell, d.code) for d in analysis.structural_errors] == [
        ("c0", "variant_collision"),
        ("c1", "variant_collision"),
    ]
    assert not analysis.cells["c2"].plannable


# --- fan-out ------------------------------------------------------------------


def fanout(run):
    return run(
        *MEMBERS,
        "# @per_variant\nr = m * 2",
        "# @per_variant\ns = r + 1",
        "t = s",
        groups=SWEEP,
    )


def fanned(var, **by_variant):
    """A fan-out cell's snapshot: the variable, and one ref per instance."""
    refs = {(var, name): cg.OutputRef(cg.Provenance(h)) for name, h in by_variant.items()}
    return cg.Published(outputs={var: next(iter(refs.values()))}, instances=refs)


def test_fan_out_and_chained_fan_out(run):
    analysis = fanout(run)
    assert set(analysis.graph.fanout_cells) == {"c2", "c3"}
    assert analysis.graph.fanout_cells["c3"].group == "g"
    assert ("c2", "c3", "r") in edges(analysis)
    assert analysis.plan("c4", ready=lambda _: False) == ["c0", "c1", "c2", "c3", "c4"]


def test_instance_keys_read_their_own_variant(run):
    analysis = fanout(run)
    upstream = published(c0=out(m="h0"), c1=out(m="h1"))
    small = analysis.execution_key("c2", instance="small", published=upstream, env_hash=ENV)
    assert small.parts["upstream"] == [["variant:c0.m@small", "variant:m:small=h0"]]
    base = strata_provenance(["variant:m:small=h0"], analysis.cells["c2"].definition_key, ENV)
    assert small.digest == vestigo_subkey(base, "variant=small")
    # The big member's bytes do not reach the small instance.
    moved = published(c0=out(m="h0"), c1=out(m="other"))
    again = analysis.execution_key("c2", instance="small", published=moved, env_hash=ENV)
    assert again.digest == small.digest
    big = analysis.execution_key("c2", instance="big", published=moved, env_hash=ENV)
    assert (
        big.digest
        != analysis.execution_key("c2", instance="big", published=upstream, env_hash=ENV).digest
    )


def test_a_chained_instance_reads_its_upstream_instance(run):
    analysis = fanout(run)
    snapshot = fanned("r", small="rs", big="rb")
    key = analysis.execution_key(
        "c3", instance="big", published=published(c2=snapshot), env_hash=ENV
    )
    assert key.parts["upstream"] == [["instances:c2.r", "variant:r:big=rb"]]


def test_a_plain_reader_of_a_fan_out_reads_every_instance(run):
    analysis = fanout(run)
    snapshot = fanned("s", small="ss", big="sb")
    key = analysis.execution_key("c4", published=published(c3=snapshot), env_hash=ENV)
    assert key.parts["upstream"] == [["instances:c3.s", "fanout:s:big=sb;small=ss"]]
    # An instance not run yet is left out, as Strata's loader drops it.
    partial = cg.Published(
        outputs=snapshot.outputs, instances={("s", "big"): snapshot.instances[("s", "big")]}
    )
    key = analysis.execution_key("c4", published=published(c3=partial), env_hash=ENV)
    assert key.parts["upstream"] == [["instances:c3.s", "fanout:s:big=sb"]]


def test_an_instance_must_exist(run):
    analysis = fanout(run)
    with pytest.raises(ValueError, match="no instance"):
        analysis.execution_key("c2", instance="huge", published=published(), env_hash=ENV)
    with pytest.raises(ValueError, match="does not fan out"):
        analysis.execution_key("c4", instance="small", published=published(), env_hash=ENV)


def test_per_variant_without_a_sweep_is_a_warning(run):
    analysis = run("m = 1", "# @per_variant\nr = m")
    assert codes(analysis) == ["per_variant_no_sweep_source"]
    assert analysis.graph.fanout_cells == {}


# --- after --------------------------------------------------------------------


def test_after_orders_without_a_variable(run):
    analysis = run("x = 1", "y = 1", "# @after c1\nz = x")
    assert edges(analysis) == [("c0", "c2", "x"), ("c1", "c2", "")]
    key = analysis.execution_key(
        "c2", published=published(c0=out(x="h0"), c1=out(y="h1")), env_hash=ENV
    )
    # An ordering edge orders; its upstream's outputs are still components.
    assert [address for address, _ in key.parts["upstream"]] == ["output:c0.x", "output:c1.y"]


# --- blocking -----------------------------------------------------------------


@pytest.mark.parametrize(
    "downstream",
    ["y = x", "# @after c0\ny = 1", "x = 0\n# @seed c0 x\ny = x"],
    ids=["variable", "after", "seed"],
)
def test_an_error_blocks_downstream_through_every_edge(run, downstream):
    if downstream.startswith("x = 0"):
        cells = ("# @nocache maybe\nx = 1", "x = 2", "# @seed c0 x\ny = x")
    else:
        cells = ("# @nocache maybe\nx = 1", downstream)
    analysis = run(*cells)
    last = f"c{len(cells) - 1}"
    assert not analysis.cells[last].plannable
    assert analysis.cells[last].blocked_by == ["c0"]
    assert [d.code for d in analysis.blocking(last)] == ["nocache_unreadable"]
    assert last not in analysis.dag.topological_order


def test_a_seed_replaces_the_latest_producer(run):
    analysis = run("x = 1", "x = 2", "# @seed c0 x\ny = x")
    assert edges(analysis) == [("c0", "c2", "x")]


# --- partial analysis ---------------------------------------------------------


def test_a_cycle_leaves_the_rest_plannable(run):
    analysis = run("# @after c1\nx = 1", "y = x", "z = 1", "w = y")
    assert [(d.cell, d.code) for d in analysis.structural_errors] == [
        ("c0", "cycle"),
        ("c1", "cycle"),
    ]
    assert analysis.structural_errors[0].message.startswith("Cycle detected in DAG: ")
    plannable = {cid for cid, ca in analysis.cells.items() if ca.plannable}
    assert plannable == {"c2"}
    assert analysis.dag.topological_order == ["c2"]
    assert analysis.plan("c2", ready=lambda _: False) == ["c2"]
    assert analysis.cells["c2"].definition_key is not None
    assert analysis.cells["c3"].definition_key is None
