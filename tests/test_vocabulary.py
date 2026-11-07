import re

import pytest
from test_languages import Dashes

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
    owner = vocab.owner("loop_until")
    assert owner is not None and owner.key == "loop"


def test_hooks_compose():
    one = cg.Vocabulary([cg.Annotation("a")], cell_effects=lambda m, p: [cg.Binds("x")])
    two = cg.Vocabulary([cg.Annotation("b")], cell_effects=lambda m, p: [cg.Binds("y")])
    both = (one | two).cell_effects
    assert both is not None
    assert list(both({}, {})) == [cg.Binds("x"), cg.Binds("y")]


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
    # A misspelt key is refused by both, not left as is.
    with pytest.raises(KeyError):
        core.replace("nmae", in_key=True)
    with pytest.raises(KeyError):
        core.without("nmae")


def test_set_and_remove_write_the_languages_marker():
    cg.register_language_analyzer("dashes", Dashes())
    core = cg.Vocabulary.core()
    assert core.set_directive("SELECT 1\n", "name", "n", language="dashes") == (
        "-- @name n\nSELECT 1\n"
    )
    assert core.set_directive("SELECT 1\n", "name", "n") == "# @name n\nSELECT 1\n"
    source = "-- @name n\n# @name m\nSELECT 1"
    assert core.remove_directive(source, "name", language="dashes") == "# @name m\nSELECT 1"
    assert core.remove_directive(source, "name") == source
    vocab = cg.Vocabulary([cg.Annotation("mount", parse=cg.stacked(lambda d: d.value))])
    with pytest.raises(ValueError, match="repeatable"):
        vocab.set_directive("SELECT 1", "mount", "a b", language="dashes")


def test_grammar_for_keeps_the_vocabularys_grammar_class():
    class Custom(cg.Grammar):
        pass

    vocab = cg.Vocabulary(grammar=Custom())
    assert vocab.grammar_for(cg.PythonAnalyzer()) is vocab.grammar
    dashes = vocab.grammar_for(Dashes())
    assert type(dashes) is Custom and dashes.marker == "--"


def test_composition_keeps_a_custom_grammar():
    custom = cg.Grammar("--")
    one = cg.Vocabulary([cg.Annotation("a")], grammar=custom)
    two = cg.Vocabulary([cg.Annotation("b")])
    assert (one | two).grammar is custom and (two | one).grammar is custom
    assert (one | cg.Vocabulary([cg.Annotation("c")], grammar=custom)).grammar is custom
    assert (two | cg.Vocabulary([cg.Annotation("c")])).grammar is cg.GRAMMAR
    # An equal grammar from another package composes; a different one is refused.
    assert (one | cg.Vocabulary([cg.Annotation("c")], grammar=cg.Grammar("--"))).grammar is custom
    assert (two | cg.Vocabulary([cg.Annotation("c")], grammar=cg.Grammar())).grammar is cg.GRAMMAR
    with pytest.raises(ValueError, match="grammar"):
        one | cg.Vocabulary([cg.Annotation("c")], grammar=cg.Grammar("//"))


def test_grammars_are_equal_by_class_marker_and_pattern():
    class Quarto(cg.Grammar):
        directive_re = re.compile(r"^#\|\s*(\w+):\s*(.*?)\s*$")

    assert cg.Grammar("--") == cg.Grammar("--") and hash(cg.Grammar()) == hash(cg.GRAMMAR)
    assert cg.Grammar("--") != cg.Grammar() and Quarto() != cg.Grammar()
    assert cg.GRAMMAR.with_marker("--") == cg.Grammar("--")


def test_an_edit_takes_the_analyzer_itself():
    # A host with its own ``analyzers=`` table edits with the analyzer it analyzes with.
    core = cg.Vocabulary.core()
    assert core.set_directive("SELECT 1", "name", "n", language=Dashes()) == "-- @name n\nSELECT 1"
    assert core.remove_directive("-- @name n\nSELECT 1", "name", language=Dashes()) == "SELECT 1"
    with pytest.raises(cg.UnknownLanguageError):
        core.set_directive("SELECT 1", "name", "n", language="unregistered")
