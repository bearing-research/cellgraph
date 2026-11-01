import pytest

import cellgraph as cg
from cellgraph.grammar import Directive


def _d(value, key="k", line=1):
    return Directive(key, value, line)


def test_composition_refuses_a_duplicate_key():
    with pytest.raises(ValueError, match="'name'"):
        cg.Vocabulary.core() | cg.Vocabulary([cg.Annotation("name")])


def test_composition_refuses_an_also_read_key_claimed_twice():
    with pytest.raises(ValueError, match="'until'"):
        cg.Vocabulary([cg.Annotation("loop", also_reads=("until",)), cg.Annotation("until")])


def test_keys_include_also_read_keys():
    vocab = cg.Vocabulary([cg.Annotation("loop", also_reads=("loop_until",))])
    assert vocab.keys == {"loop", "loop_until"}
    assert vocab.owner("loop_until").key == "loop"


def test_hooks_compose():
    one = cg.Vocabulary([cg.Annotation("a")], cell_effects=lambda m, p: [cg.Binds("x")])
    two = cg.Vocabulary([cg.Annotation("b")], cell_effects=lambda m, p: [cg.Binds("y")])
    assert list((one | two).cell_effects({}, {})) == [cg.Binds("x"), cg.Binds("y")]


def test_scalar_takes_the_last_line_and_refuses_an_empty_one():
    assert cg.scalar(int)([_d("1"), _d("2")]) == 2
    with pytest.raises(cg.AnnotationError):
        cg.scalar(str)([_d("")])
    with pytest.raises(cg.AnnotationError):
        cg.scalar(float)([_d("soon")])


def test_flag_reads_on_and_off():
    assert cg.flag()([_d("")]) is True
    assert cg.flag()([_d("off")]) is False
    with pytest.raises(cg.AnnotationError):
        cg.flag()([_d("maybe")])


def test_a_stacked_key_cannot_be_set_as_one_line():
    vocab = cg.Vocabulary([cg.Annotation("mount", parse=cg.stacked(lambda d: d.value))])
    assert cg.stacked(lambda d: d.value)([_d("a"), _d("b")]) == ["a", "b"]
    with pytest.raises(ValueError, match="repeatable"):
        vocab.set_directive("x = 1\n", "mount", "a b")
    assert cg.Vocabulary.core().set_directive("x = 1\n", "name", "n") == "# @name n\nx = 1\n"


def test_replace_and_without():
    core = cg.Vocabulary.core()
    assert "name" not in core.without("name")
    assert core.replace("name", in_key=True)["name"].in_key
