import pytest

import cellgraph as cg


def test_the_plan_is_the_unready_upstream_in_order_then_the_target(run):
    analysis = run("a = 1", "b = a", "c = 1", "# @after c2\nd = b")
    # Kahn's order over the roots first, as Strata's cascade planner has it.
    assert analysis.plan("c3", ready=lambda _: False) == ["c0", "c2", "c1", "c3"]
    assert analysis.plan("c3", ready=lambda cid: cid == "c1") == ["c0", "c2", "c3"]
    assert analysis.plan("c2", ready=lambda _: True) == ["c2"]


def test_a_fan_out_cell_is_one_step(run):
    analysis = run(
        "# @variant g a\nm = 1",
        "# @variant g b\nm = 2",
        "# @per_variant\nr = m",
        groups=cg.GroupConfig(modes={"g": "sweep"}),
    )
    assert analysis.plan("c2", ready=lambda _: False) == ["c0", "c1", "c2"]


def test_an_unplannable_target_raises_with_its_blockers(run):
    analysis = run("# @nocache maybe\na = 1", "b = a")
    with pytest.raises(cg.NotPlannableError) as raised:
        analysis.plan("c1", ready=lambda _: False)
    assert raised.value.cell_id == "c1"
    assert [d.code for d in raised.value.diagnostics] == ["nocache_unreadable"]
