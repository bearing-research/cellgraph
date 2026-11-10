"""How cells wire: edges, roots and leaves, order, what a cell needs, and shadowed names.

Ported from the host's DAG tests, which were the only ones pinning these shapes.
"""

from conftest import edges

import cellgraph as cg


def test_a_diamond_wires_four_edges_with_one_root_and_one_leaf(run):
    analysis = run("x = 1", "y = x", "z = x", "w = y + z")
    graph = analysis.graph

    assert edges(analysis) == [
        ("c0", "c1", "x"),
        ("c0", "c2", "x"),
        ("c1", "c3", "y"),
        ("c2", "c3", "z"),
    ]
    assert graph.cell_upstream == {"c0": [], "c1": ["c0"], "c2": ["c0"], "c3": ["c1", "c2"]}
    assert graph.cell_downstream == {"c0": ["c1", "c2"], "c1": ["c3"], "c2": ["c3"], "c3": []}
    assert (graph.roots, graph.leaves) == ({"c0"}, {"c3"})
    order = graph.topological_order
    assert (order[0], set(order[1:3]), order[3]) == ("c0", {"c1", "c2"}, "c3")


def test_cells_reading_nothing_are_each_a_root(run):
    graph = run("x = 1", "y = 2", "z = x + y").graph

    assert (graph.roots, graph.leaves) == ({"c0", "c1"}, {"c2"})


def test_two_names_from_one_cell_are_two_edges_and_one_link(run):
    analysis = run("x = 1\ny = 2", "z = x + y")

    assert edges(analysis) == [("c0", "c1", "x"), ("c0", "c1", "y")]
    assert analysis.graph.cell_upstream == {"c0": [], "c1": ["c0"]}
    assert analysis.graph.cell_downstream == {"c0": ["c1"], "c1": []}


class TestForwardReferences:
    """A name resolves against the cells before the reader, so reading ahead is no cycle."""

    def test_a_name_defined_later_wires_nothing(self, run):
        graph = run("x = y", "y = x").graph

        assert graph.cell_upstream == {"c0": [], "c1": ["c0"]}

    def test_a_cell_rebinding_its_own_name_has_no_upstream(self, run):
        assert run("x = x + 1").graph.cell_upstream == {"c0": []}

    def test_a_chain_read_ahead_links_only_what_came_before(self, run):
        graph = run("x = z", "y = x", "z = y").graph

        assert graph.cell_upstream == {"c0": [], "c1": ["c0"], "c2": ["c1"]}


class TestUpstreamReachable:
    """What running a cell needs: itself and every cell upstream of it."""

    def test_a_chain_needs_everything_before_it(self, run):
        graph = run("x = 1", "y = x", "z = y").graph

        reach = graph.upstream_reachable("c2")
        assert [c for c in graph.topological_order if c in reach] == ["c0", "c1", "c2"]

    def test_a_diamond_needs_both_sides(self, run):
        graph = run("x = 1", "y = x", "z = x", "w = y + z").graph

        assert graph.upstream_reachable("c3") == {"c0", "c1", "c2", "c3"}

    def test_a_root_needs_only_itself(self, run):
        assert run("x = 1", "y = x").graph.upstream_reachable("c0") == {"c0"}

    def test_an_unrelated_cell_is_not_needed(self, run):
        assert run("x = 1", "y = 2").graph.upstream_reachable("c1") == {"c1"}


class TestInplaceMutation:
    """An ``inplace=True`` call makes its cell the producer a later reader gets."""

    def test_a_reader_after_an_inplace_call_reads_the_mutating_cell(self, run):
        graph = run(
            "df = load_data()", "df.drop(columns=['x'], inplace=True)", "total = df.sum()"
        ).graph

        assert graph.cell_upstream == {"c0": [], "c1": ["c0"], "c2": ["c1"]}
        # The mutating cell stores df again; the loader's df is read by the mutating cell.
        assert graph.consumed_variables["c1"] == {"df"}
        assert graph.consumed_variables["c0"] == {"df"}

    def test_a_call_without_inplace_is_a_plain_read(self, run):
        graph = run("df = load_data()", "head = df.head()", "total = df.sum()").graph

        assert graph.cell_upstream["c2"] == ["c0"]
        assert graph.consumed_variables["c1"] == set()


class TestShadowedBuiltins:
    """A name a cell defines over a builtin wires like any other; one it does not, wires nothing."""

    def test_a_defined_builtin_name_wires_its_reader(self, run):
        analysis = run("input = load_data()", "model = fit(input)")

        assert edges(analysis) == [("c0", "c1", "input")]
        assert analysis.graph.consumed_variables["c0"] == {"input"}

    def test_an_undefined_builtin_wires_nothing(self, run):
        analysis = run("x = 1", "print(x)")

        assert edges(analysis) == [("c0", "c1", "x")]
        assert analysis.graph.consumed_variables["c0"] == {"x"}

    def test_names_users_shadow_often_wire_together(self, run):
        analysis = run("id = next_run_id()\ntype = pick_model()", "run = launch(id, type)")

        assert analysis.graph.cell_upstream["c1"] == ["c0"]
        assert analysis.graph.consumed_variables["c0"] == {"id", "type"}


class TestShadowWarnings:
    def test_a_second_definer_of_a_name_is_warned(self, run):
        warnings = run("result = 1", "result = 2").graph.shadow_warnings

        assert list(warnings) == ["c1"]
        assert any("result" in w for w in warnings["c1"])

    def test_distinct_names_warn_nothing(self, run):
        assert run("x = 1", "y = x").graph.shadow_warnings == {}


class TestVariantWiring:
    def test_an_unknown_selection_falls_back_to_the_first_member(self, run):
        analysis = run(
            "# @variant g a\nm = 1",
            "# @variant g b\nm = 2",
            "r = m",
            groups=cg.GroupConfig(active={"g": "ghost"}),
        )

        assert analysis.graph.inactive_cells == {"c1"}
        assert edges(analysis) == [("c0", "c2", "m")]

    def test_after_naming_an_inactive_member_adds_no_edge(self, run):
        analysis = run(
            "# @variant g a\nm = 1",
            "# @variant g b\nm = 2",
            "# @after c1\nq = 1",
            groups=cg.GroupConfig(active={"g": "a"}),
        )

        assert analysis.graph.cell_upstream["c2"] == []
        assert analysis.graph.cell_downstream["c1"] == []
