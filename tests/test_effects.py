import pytest

import cellgraph as cg


def test_names_coerce_to_a_tuple():
    assert cg.Binds("x").names == ("x",)
    assert cg.Defines(["a", "b"]).names == ("a", "b")
    assert cg.After("c1") == cg.After(["c1"])
    assert hash(cg.Reads("x")) == hash(cg.Reads(("x",)))


def test_an_input_spec_does_not_decide_its_identity():
    assert cg.Input("file", "x", spec={"a": 1}) == cg.Input("file", "x", spec={"b": 2})


def test_an_effect_outside_the_ten_is_refused():
    bad = cg.Annotation("bad", effects=lambda v: ["not an effect"])  # ty: ignore[invalid-argument-type]
    vocab = cg.Vocabulary([bad])
    with pytest.raises(TypeError, match="ten effects"):
        cg.analyze([cg.Cell("a", "# @bad 1\nx = 1")], vocab)
