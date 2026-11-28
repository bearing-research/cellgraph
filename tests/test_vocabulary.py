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


# --- found by the audit -------------------------------------------------------------


class Prose(Dashes):
    """A language whose ``# @`` lines are prose: markdown headings."""

    directives = False


class OnlyApp(Dashes):
    directives = frozenset({"app"})


def test_an_edit_leaves_alone_a_key_the_language_does_not_read():
    core = cg.Vocabulary.core()
    heading = "-- @name Overview\nSome prose\n"
    assert core.remove_directive(heading, "name", language=Prose()) == heading
    with pytest.raises(ValueError, match="does not read"):
        core.set_directive("Text\n", "name", "x", language=Prose())
    with pytest.raises(ValueError, match="does not read"):
        core.set_directive("Text\n", "name", "x", language=OnlyApp())
    app = cg.Vocabulary([cg.Annotation("app")])
    assert app.set_directive("x\n", "app", "a", language=OnlyApp()) == "-- @app a\nx\n"


@pytest.mark.parametrize("key", ["Tag", "", "a b", "a\tb"])
def test_an_annotation_key_is_lower_case_without_whitespace(key):
    with pytest.raises(ValueError, match="lower-case"):
        cg.Annotation(key)
    with pytest.raises(ValueError, match="lower-case"):
        cg.Annotation("loop", also_reads=(key,))


def test_also_reads_may_be_an_iterator():
    assert cg.Annotation("loop", also_reads=iter(["until"])).also_reads == ("until",)  # ty: ignore[invalid-argument-type]
    assert cg.Annotation("loop", also_reads=map(str.lower, ["UNTIL"])).also_reads == ("until",)  # ty: ignore[invalid-argument-type]


def test_a_vocabulary_with_a_grammar_of_its_own_state_hashes():
    class Stateful(cg.Grammar):
        def __init__(self, extra):
            super().__init__()
            self.extra = extra

        def __eq__(self, other):
            return super().__eq__(other) and self.extra == other.extra

    one, two = cg.Vocabulary(grammar=Stateful(1)), cg.Vocabulary(grammar=Stateful(2))
    assert hash(one) == hash(two) and one != two
    assert one == cg.Vocabulary(grammar=Stateful(1))


def test_a_stacked_item_raising_one_error_is_reported_on_each_line():
    shared = cg.AnnotationError("bad")

    def item(directive):
        if directive.value == "x":
            raise shared
        return directive.value

    vocab = cg.Vocabulary([cg.Annotation("n", parse=cg.stacked(item), in_key=False)])
    analysis = cg.analyze([cg.Cell("a", "# @n 1\n# @n x\n# @n x\nz = 1")], vocab)
    assert [d.line for d in analysis.diagnostics] == [2, 3]
    assert shared.line is None


def test_a_stacked_line_it_cannot_read_leaves_the_cell_unkeyed():
    vocab = cg.Vocabulary([cg.Annotation("n", parse=cg.stacked(lambda d: int(d.value)))])
    analysis = cg.analyze(
        [cg.Cell("a", "# @n 1\n# @n x\nz = 1"), cg.Cell("b", "# @n 1\nz = 1")], vocab
    )
    assert analysis.cells["a"].unkeyed == ["n"]
    assert isinstance(
        analysis.execution_key("a", published=lambda _: cg.Published(), env_hash="e" * 64),
        cg.Unavailable,
    )


def test_a_non_str_key_is_a_type_error():
    for edit in (
        lambda: cg.Vocabulary.core().set_directive("x", 1, "v"),  # ty: ignore[invalid-argument-type]
        lambda: cg.Vocabulary.core().remove_directive("x", 1),  # ty: ignore[invalid-argument-type]
    ):
        with pytest.raises(TypeError):
            edit()


def test_also_reads_is_a_sequence_of_keys():
    with pytest.raises(TypeError, match="also_reads"):
        cg.Annotation("loop", also_reads="until")  # ty: ignore[invalid-argument-type]
    assert cg.Annotation("loop", also_reads=["until"]).also_reads == ("until",)  # ty: ignore[invalid-argument-type]


def test_a_vocabulary_checks_what_it_is_given():
    with pytest.raises(TypeError, match="Annotations"):
        cg.Vocabulary(["name"])  # ty: ignore[invalid-argument-type]
    with pytest.raises(TypeError, match="Grammar"):
        cg.Vocabulary(grammar="--")  # ty: ignore[invalid-argument-type]
    with pytest.raises(TypeError, match="callable"):
        cg.Vocabulary(cell_effects=1)  # ty: ignore[invalid-argument-type]
    with pytest.raises(TypeError):
        cg.Vocabulary.core() | 3  # ty: ignore[unsupported-operator]


def test_vocabularies_with_different_grammars_differ():
    assert cg.Vocabulary(grammar=cg.Grammar("--")) != cg.Vocabulary()
    assert cg.Vocabulary(grammar=cg.Grammar("#")) == cg.Vocabulary()


def test_replace_can_rename_an_annotation():
    vocab = cg.Vocabulary([cg.Annotation("old")]).replace("old", key="new")
    assert "new" in vocab and "old" not in vocab


def test_a_stacked_line_that_cannot_be_read_is_an_error_on_that_line(vocab):
    tags = cg.Vocabulary(
        [cg.Annotation("n", parse=cg.stacked(lambda d: int(d.value)), in_key=False)]
    )
    analysis = cg.analyze([cg.Cell("a", "# @n 1\n# @n x\n# @n y\n# @n 4\nz = 1")], tags)
    cell = analysis.cells["a"]
    assert cell.annotations["n"] == [1, 4]
    assert [(d.code, d.line) for d in cell.diagnostics] == [
        ("n_unreadable", 2),
        ("n_unreadable", 3),
    ]


def test_a_partial_holds_annotation_errors():
    with pytest.raises(TypeError, match="AnnotationError"):
        cg.Partial(1, [ValueError("x")])  # ty: ignore[invalid-argument-type]


def test_a_validator_may_return_none():
    checked = cg.Vocabulary([cg.Annotation("m", validate=lambda value, context: None)])  # ty: ignore[invalid-argument-type]
    analysis = cg.analyze([cg.Cell("a", "# @m x\nz = 1")], checked)
    assert analysis.cells["a"].diagnostics == []


@pytest.mark.parametrize("names", [b"xy", [1], ["a", None]])
def test_effect_names_are_str(names):
    with pytest.raises(TypeError, match="names must be str"):
        cg.Binds(names)


def test_a_key_the_grammar_does_not_read_is_refused():
    with pytest.raises(ValueError, match="grammar reads"):
        cg.Vocabulary([cg.Annotation("my-tag")])
    with pytest.raises(ValueError, match="grammar reads"):
        cg.Vocabulary([cg.Annotation("loop", also_reads=("un-til",))])


def test_a_stacked_item_keeps_its_errors_kind_and_reads_a_group():
    class Hinted(cg.AnnotationError):
        def __init__(self, message, hint, **kwargs):
            super().__init__(message, **kwargs)
            self.hint = hint

    def item(directive):
        if directive.value == "h":
            raise Hinted("bad", "try 1")
        if directive.value == "g":
            raise ExceptionGroup("g", [cg.AnnotationError("one"), cg.AnnotationError("two")])
        return directive.value

    parsed = cg.stacked(item)([cg.Directive("n", v, i) for i, v in enumerate("1hg3", 1)])
    assert parsed.value == ["1", "3"]
    assert [(type(e).__name__, str(e), e.line) for e in parsed.errors] == [
        ("Hinted", "bad", 2),
        ("AnnotationError", "one", 3),
        ("AnnotationError", "two", 3),
    ]
    assert parsed.errors[0].hint == "try 1"

    def other(directive):
        raise ExceptionGroup("g", [cg.AnnotationError("one"), KeyError("k")])

    with pytest.raises(ExceptionGroup):
        cg.stacked(other)([cg.Directive("n", "x", 1)])
