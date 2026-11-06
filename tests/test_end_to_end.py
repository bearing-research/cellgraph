"""The brief's scenarios over the core vocabulary (plus the test host's effects)."""

import pytest
from conftest import codes, edges, prov
from test_keys import ENV, out, published

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
    assert key.parts["upstream"] == [
        [
            "sweep:m",
            cg.canonical_json(
                ["sweep", "m", [["big", ["provenance", "h1"]], ["small", ["provenance", "h0"]]]]
            ),
        ]
    ]
    assert key.digest == cg.parts_digest(key.parts)


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
    assert small.parts["upstream"] == [
        ["variant:c0.m@small", cg.canonical_json(["variant", "m", "small", ["provenance", "h0"]])]
    ]
    assert (small.parts["scope"], small.parts["instance"]) == ("instance", "small")
    cell = analysis.execution_key("c2", published=upstream, env_hash=ENV)
    assert cell.digest != small.digest
    # The big member's bytes do not reach the small instance.
    moved = published(c0=out(m="h0"), c1=out(m="other"))
    again = analysis.execution_key("c2", instance="small", published=moved, env_hash=ENV)
    assert again.digest == small.digest
    big = analysis.execution_key("c2", instance="big", published=moved, env_hash=ENV)
    assert (
        big.digest
        != analysis.execution_key("c2", instance="big", published=upstream, env_hash=ENV).digest
    )


# Members that also make y, which no reader reads.
WITH_Y = ("# @variant g small\nm = 1\ny = 1", "# @variant g big\nm = 2\ny = 2")


def both(**extra):
    return published(c0=out(m="h0", y="y0", **extra), c1=out(m="h1", y="y1", **extra))


def test_a_cell_after_a_reader_leaves_its_key_alone(run):
    # The members' y is keyed as theirs, wherever the notebook defines y last.
    alone = run(*WITH_Y, "r = m", groups=SWEEP)
    later = run(*WITH_Y, "r = m", "y = 99", groups=SWEEP)
    key = alone.execution_key("c2", published=both(), env_hash=ENV)
    assert later.execution_key("c2", published=both(), env_hash=ENV) == key
    assert [address for address, _ in key.parts["upstream"]] == ["sweep:m", "sweep:y"]


@pytest.mark.parametrize("tail", [(), ("y = 0",)])
def test_an_instance_reads_nothing_of_another_variant(run, tail):
    analysis = run(*WITH_Y, "# @per_variant\nr = m", *tail, groups=SWEEP)
    key = analysis.execution_key("c2", instance="small", published=both(), env_hash=ENV)
    assert [address for address, _ in key.parts["upstream"]] == [
        "variant:c0.m@small",
        "variant:c0.y@small",
    ]
    # Nor of what that variant publishes beyond its defines, or as one cell ref.
    extra = analysis.execution_key("c2", instance="small", published=both(_x="hx"), env_hash=ENV)
    assert not any(
        address.startswith(("output:c1", "variant:c1")) for address, _ in extra.parts["upstream"]
    )
    ref = published(c0=out(m="h0"), c1=cg.Published(cell_ref=cg.OutputRef(cg.Provenance("hc"))))
    by_ref = analysis.execution_key("c2", instance="small", published=ref, env_hash=ENV)
    assert not any(address == "cell:c1" for address, _ in by_ref.parts["upstream"])


def test_a_chained_instance_reads_its_upstream_instance(run):
    analysis = fanout(run)
    snapshot = fanned("r", small="rs", big="rb")
    key = analysis.execution_key(
        "c3", instance="big", published=published(c2=snapshot), env_hash=ENV
    )
    assert key.parts["upstream"] == [
        ["instances:c2.r", cg.canonical_json(["variant", "r", [["big", ["provenance", "rb"]]]])]
    ]


def test_a_plain_reader_of_a_fan_out_reads_every_instance(run):
    analysis = fanout(run)
    snapshot = fanned("s", small="ss", big="sb")
    key = analysis.execution_key("c4", published=published(c3=snapshot), env_hash=ENV)
    assert key.parts["upstream"] == [
        [
            "instances:c3.s",
            cg.canonical_json(
                ["fanout", "s", [["big", ["provenance", "sb"]], ["small", ["provenance", "ss"]]]]
            ),
        ]
    ]
    # An instance not run yet is left out, as Strata's loader drops it.
    partial = cg.Published(
        outputs=snapshot.outputs, instances={("s", "big"): snapshot.instances[("s", "big")]}
    )
    key = analysis.execution_key("c4", published=published(c3=partial), env_hash=ENV)
    assert key.parts["upstream"] == [
        ["instances:c3.s", cg.canonical_json(["fanout", "s", [["big", ["provenance", "sb"]]]])]
    ]


def test_a_run_id_cannot_forge_another_instance(run):
    # A joined string would read ``a=run:x;b=run:y`` for both of these.
    analysis = fanout(run)

    def key(**refs):
        instances = {("s", name): cg.OutputRef(cg.Run(rid)) for name, rid in refs.items()}
        snapshot = cg.Published(outputs={"s": next(iter(instances.values()))}, instances=instances)
        return analysis.execution_key("c4", published=published(c3=snapshot), env_hash=ENV)

    assert key(big="x;small=run:y").digest != key(big="x", small="y").digest


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
    # Every cell has a definition key; only the cells the cycle reaches have no
    # execution key.
    assert all(ca.definition_key is not None for ca in analysis.cells.values())
    assert analysis.execution_key("c2", published=published(), env_hash=ENV).digest
    for cell_id in ("c0", "c1", "c3"):
        with pytest.raises(cg.NotPlannableError):
            analysis.execution_key(cell_id, published=published(), env_hash=ENV)


def test_the_binding_rule_is_what_the_keys_read(run):
    analysis = fanout(run)
    # An instance reads its own variant of its own group, as a scalar.
    assert analysis.variant_read("c2", "c0", "m", "small") == cg.VariantRead(
        ("small",), scalar=True, fanout=False
    )
    # The other member gives that instance nothing.
    assert analysis.variant_read("c2", "c1", "m", "small") == cg.VariantRead(
        (), scalar=True, fanout=False
    )
    # A plain reader of a fan-out reads every instance, as a dict.
    read = analysis.variant_read("c4", "c3", "s")
    assert read.fanout and not read.scalar and set(read.variants) == {"small", "big"}


# --- each ---------------------------------------------------------------------

EACH = ("items = [1, 2]", "other = 1", "# @each item in items\nr = item + other", "t = r")


def each_key(analysis, item, items="hc", other="ho", **kw):
    return analysis.execution_key(
        "c2",
        instance=item,
        published=published(c0=out(items=items), c1=out(other=other)),
        env_hash=ENV,
        **kw,
    )


def test_an_each_cell_is_wired_to_its_source_and_binds_its_item(run):
    analysis = run(*EACH)
    assert analysis.graph.each_cells == {"c2": "items"}
    assert analysis.graph.fanout_cells == {}
    assert ("c0", "c2", "items") in edges(analysis)
    cell = analysis.cells["c2"]
    assert "item" not in cell.references
    assert cell.bound == ["item"]
    assert cell.key_fields["each"] == {"name": "item", "source": "items"}
    assert analysis.dag.each_cells == {"c2": "items"}
    assert codes(analysis) == []


def test_an_items_key_and_digest_stay_apart(run):
    # Joined as "key=digest", these two items were one component.
    analysis = run(*EACH)
    one = each_key(analysis, cg.Item("a=b", "c"))
    other = each_key(analysis, cg.Item("a", "b=c"))
    assert one.parts["upstream"][0] != other.parts["upstream"][0]


def test_an_item_key_leaves_its_source_out(run):
    analysis = run(*EACH)
    item = cg.Item("a", "d1")
    key = each_key(analysis, item)
    assert key.parts["scope"] == "instance"
    assert key.parts["instance"] == "a"
    assert key.parts["upstream"] == [
        ["each:items", cg.canonical_json(["each", "a", "d1"])],
        ["output:c1.other", prov("ho")],
    ]
    # The rest of the collection changing does not reach this item.
    assert each_key(analysis, item, items="moved").digest == key.digest
    # Its own content, its name and any other input do.
    assert each_key(analysis, cg.Item("a", "d2")).digest != key.digest
    assert each_key(analysis, cg.Item("b", "d1")).digest != key.digest
    assert each_key(analysis, item, other="moved").digest != key.digest


def test_an_item_key_may_name_a_path_or_a_uri(run):
    analysis = run(*EACH)
    key = each_key(analysis, cg.Item("s3://bucket/a.csv", "d1"))
    assert key.parts["instance"] == "s3://bucket/a.csv"
    assert key.digest == cg.parts_digest(key.parts)


def test_reading_the_source_through_an_effect_is_each_reads_source():
    # The item key leaves the source out, so a declared read of it is refused too.
    host = cg.Vocabulary.core() | cg.Vocabulary(
        [cg.Annotation("ext", effects=lambda v: [cg.Reads(v.split())], in_key=False)]
    )
    analysis = cg.analyze(
        [
            cg.Cell("c0", "items = [1]"),
            cg.Cell("c1", "# @ext items\n# @each item in items\nr = item"),
        ],
        host,
    )
    assert codes(analysis, "c1") == ["each_reads_source"]


def test_reading_a_source_named_like_a_builtin_is_each_reads_source(run):
    analysis = run("input = [1, 2]", "# @each item in input\nprint(item, len(input))")
    assert codes(analysis, "c1") == ["each_reads_source"]


def test_the_cell_key_covers_the_whole_source(run):
    analysis = run(*EACH)
    key = each_key(analysis, None)
    assert key.parts["scope"] == "cell"
    assert ["output:c0.items", prov("hc")] in key.parts["upstream"]
    assert each_key(analysis, None, items="moved").digest != key.digest


def test_a_reader_of_an_each_cell_sees_one_ordinary_output(run):
    analysis = run(*EACH)
    key = analysis.execution_key("c3", published=published(c2=out(r="manifest")), env_hash=ENV)
    assert key.parts["upstream"] == [["output:c2.r", prov("manifest")]]
    assert analysis.variant_read("c3", "c2", "r") is None


def test_a_bound_source_is_left_out_of_an_item_key_too():
    host = cg.Vocabulary.core() | cg.Vocabulary(
        [],
        cell_effects=lambda meta, parsed: [cg.Binds("raw"), cg.Input("mount", "raw")],
    )
    analysis = cg.analyze([cg.Cell("c0", "# @each path in raw\nr = str(path)")], host)
    assert analysis.cells["c0"].references == []
    assert codes(analysis) == []
    resolve = {"mount": lambda spec: cg.Resolved(spec.name, "fp")}

    whole = analysis.execution_key("c0", published=published(), resolve=resolve, env_hash=ENV)
    assert isinstance(whole, cg.Keyed)
    assert whole.parts["inputs"] == [
        ["mount", "raw", cg.input_component("mount", "raw", cg.Resolved("raw", "fp"))]
    ]

    moved = {"mount": lambda spec: cg.Resolved(spec.name, "moved")}
    item = cg.Item("a.csv", "d1")
    key = analysis.execution_key(
        "c0", instance=item, published=published(), resolve=resolve, env_hash=ENV
    )
    assert isinstance(key, cg.Keyed)
    assert key.parts["inputs"] == []
    assert key.parts["upstream"] == [["each:raw", cg.canonical_json(["each", "a.csv", "d1"])]]
    again = analysis.execution_key(
        "c0", instance=item, published=published(), resolve=moved, env_hash=ENV
    )
    assert isinstance(again, cg.Keyed) and again.digest == key.digest


def test_an_instance_of_an_each_cell_is_an_item(run):
    analysis = run(*EACH, *MEMBERS, "# @per_variant\ns = m", groups=SWEEP)
    with pytest.raises(ValueError, match="not an Item"):
        each_key(analysis, "a")
    with pytest.raises(ValueError, match="has no items"):
        analysis.execution_key(
            "c0", instance=cg.Item("a", "d"), published=published(), env_hash=ENV
        )
    with pytest.raises(ValueError, match="has no items"):
        analysis.execution_key(
            "c6", instance=cg.Item("a", "d"), published=published(), env_hash=ENV
        )


@pytest.mark.parametrize(
    "line",
    [
        "# @each",
        "# @each item",
        "# @each item of items",
        "# @each 1item in items",
        "# @each item in 1items",
        "# @each items in items",
        "# @each item in items colour=red",
        "# @each item in items key=",
        "# @each item in items key=a key=b",
    ],
)
def test_a_line_each_cannot_read_is_an_error(run, line):
    assert codes(run("items = [1]", f"{line}\nr = 1")) == ["each_malformed"]


def test_each_takes_its_options(run):
    analysis = run("items = [1]", "# @each row in items key=id glob=*.csv\nr = row")
    assert analysis.cells["c1"].annotations["each"] == {
        "name": "row",
        "source": "items",
        "key": "id",
        "glob": "*.csv",
    }


def test_a_body_reading_its_source_is_an_error(run):
    analysis = run("items = [1]", "# @each item in items\nr = len(items)")
    assert codes(analysis) == ["each_reads_source"]
    assert not analysis.cells["c1"].plannable


def test_each_over_a_source_nothing_makes_is_an_error(run):
    assert codes(run("# @each item in items\nr = item")) == ["each_unknown_source"]


def test_each_over_a_sweep_is_an_error(run):
    analysis = run(*MEMBERS, "# @each item in m\nr = item", groups=SWEEP)
    assert codes(analysis) == ["each_over_sweep"]


def test_each_on_a_variant_member_is_an_error(run):
    analysis = run("items = [1]", "# @variant g a\n# @each item in items\nr = item")
    assert "each_on_variant_member" in codes(analysis)


def test_each_with_per_variant_is_an_error(run):
    analysis = run(
        *MEMBERS, "items = [1]", "# @each item in items\n# @per_variant\nr = m", groups=SWEEP
    )
    assert codes(analysis) == ["fanout_conflict"]
