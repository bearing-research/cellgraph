import pytest
from conftest import codes, edges

import cellgraph as cg


def test_an_unknown_key_warns_with_a_hint_or_errors_when_strict(run):
    lax = run("# @nocach\nx = 1")
    (diag,) = lax.diagnostics
    assert (diag.code, diag.severity) == ("annotation_unknown", cg.Severity.WARN)
    assert "`@nocache`" in diag.message
    assert diag.args == {"key": "nocach", "suggestion": "nocache"}
    assert lax.cells["c0"].plannable
    strict = run("# @nocach\nx = 1", strict=True)
    assert strict.diagnostics[0].severity == cg.Severity.ERROR
    assert not strict.cells["c0"].plannable


def test_a_parse_error_is_key_unreadable_or_its_own_code(run):
    assert codes(run("# @nocache maybe\nx = 1")) == ["nocache_unreadable"]
    assert codes(run("# @variant only_group\nx = 1")) == ["variant_malformed"]
    assert codes(run("# @per_variant a b\nx = 1")) == ["per_variant_unreadable"]


def test_key_fields_are_encoded_values_and_must_be_canonical(run):
    analysis = run("# @mode fast\n# @per_variant\nx = 1", "# @opaque o\ny = 1")
    assert analysis.cells["c0"].key_fields == {"mode": {"m": "fast"}, "per_variant": None}
    assert codes(analysis, "c1") == ["opaque_unencodable"]


def test_an_encoder_lowers_a_value_to_its_key_field():
    vocab = cg.Vocabulary(
        [cg.Annotation("size", parse=cg.scalar(lambda s: object()), encode=lambda v: "obj")]
    )
    analysis = cg.analyze([cg.Cell("a", "# @size 1\nx = 1")], vocab)
    assert analysis.cells["a"].key_fields == {"size": "obj"}


def test_a_validator_sees_names_and_the_host_context():
    seen = []

    def validate(value, ctx):
        seen.append((value, ctx.defines, ctx.host))
        if value == "bad":
            raise cg.AnnotationError("no")
        return [cg.Diagnostic("tag_note", cg.Severity.INFO, "noted", ctx.cell.id)]

    vocab = cg.Vocabulary([cg.Annotation("tag", in_key=False, validate=validate)])
    analysis = cg.analyze(
        [cg.Cell("a", "# @tag ok\nx = 1"), cg.Cell("b", "# @tag bad\ny = 1")],
        vocab,
        context="host",
    )
    assert seen[0] == ("ok", ("x",), "host")
    assert codes(analysis) == ["tag_note", "tag_invalid"]
    assert analysis.cells["a"].plannable and not analysis.cells["b"].plannable


def test_effects_change_the_names(run):
    analysis = run("# @out y\nx = 1", "# @ext a b\nz = y\na = 1", "# @source data\nw = data")
    assert analysis.cells["c0"].defines == ["x", "y"]
    # ``a`` is defined, so a plain Reads does not add it.
    assert analysis.cells["c1"].references == ["y", "b"]
    assert analysis.cells["c2"].references == []
    assert analysis.cells["c2"].bound == ["data"]
    assert analysis.cells["c2"].inputs == [cg.Input("file", "data")]


def test_a_carry_is_read_even_though_the_body_defines_it(run):
    analysis = run("state = 0", "# @carry state\nstate = 5", "# @ext state\nstate = 6")
    assert analysis.cells["c1"].references == ["state"]
    assert analysis.cells["c2"].references == []
    assert edges(analysis) == [("c0", "c1", "state")]


def test_a_name_bound_and_read_is_a_conflict(run):
    analysis = run("# @source x\n# @ext x\ny = 1")
    assert codes(analysis) == ["bind_read_conflict"]


def test_two_inputs_with_one_address_are_a_conflict():
    vocab = cg.Vocabulary.core() | cg.Vocabulary(
        [cg.Annotation("source", effects=lambda v: [cg.Input("file", v)], in_key=False)],
        cell_effects=lambda meta, parsed: [cg.Input("file", "x")],
    )
    analysis = cg.analyze([cg.Cell("a", "# @source x\ny = 1")], vocab)
    assert codes(analysis) == ["input_conflict"]


def test_a_cell_seeded_from_two_cells_or_a_missing_one(run):
    analysis = run("x = 1", "# @seed c9 x\ny = x")
    assert codes(analysis) == ["seed_unknown_cell"]


@pytest.mark.parametrize(
    ("cells", "reason"),
    [
        (["x = 1", "# @name n\n# @seed c9 x\ny = x"], "unknown"),
        (["x = 1", "# @name n\n# @seed c1 x\ny = x"], "self"),
        (["y = 1", "# @name n\n# @seed c0 x\ny = x"], "undefined"),
    ],
)
def test_a_seed_error_is_on_its_annotation_line_and_says_why(run, cells, reason):
    (diag,) = run(*cells).cells["c1"].diagnostics
    assert (diag.annotation, diag.line) == ("seed", 2)
    assert diag.args == {"name": "x", "cell_id": diag.args["cell_id"], "reason": reason}


def test_keeps_marks_a_defined_name_consumed(run):
    analysis = run("# @keep x\nx = 1\ny = 2", "# @keep nope\nz = 1")
    assert analysis.graph.consumed_variables["c0"] == {"x"}
    assert analysis.graph.consumed_variables["c1"] == set()


def test_after_edges_and_their_diagnostics(run):
    analysis = run("x = 1", "# @after c0 c1 c7\ny = 1")
    assert ("c0", "c1", "") in edges(analysis)
    assert codes(analysis, "c1") == ["after_self", "after_unknown_cell"]


def test_an_after_problem_is_on_the_line_naming_the_id(run):
    analysis = run("x = 1", "# @after c0\n# @after c7, c1\ny = 1")
    found = [(d.code, d.line, dict(d.args)) for d in analysis.cells["c1"].diagnostics]
    assert sorted(found) == [
        ("after_self", 2, {"cell_id": "c1"}),
        ("after_unknown_cell", 2, {"cell_id": "c7"}),
    ]


def test_args_do_not_change_what_a_diagnostic_equals():
    a = cg.Diagnostic("x", cg.Severity.WARN, "m", args={"k": 1})
    assert a == cg.Diagnostic("x", cg.Severity.WARN, "m")
    assert {a} == {cg.Diagnostic("x", cg.Severity.WARN, "m", args={"k": 2})}


def test_unknown_normalization_or_digest_is_refused(run):
    with pytest.raises(ValueError):
        run("x = 1", normalization="nope")
    with pytest.raises(ValueError):
        run("x = 1", digest="nope")


class Prose:
    owns_key = True
    directives = False

    def analyze(self, cell):
        return cg.Analyzed()

    def normalize(self, body):
        return cg.strip_lines(body)


def test_a_language_without_directives_reads_no_annotations(vocab):
    source = "# @worker big\n# @nocache maybe\nSome prose."
    analysis = cg.analyze(
        [cg.Cell("a", source, language="prose")],
        vocab,
        analyzers={"prose": Prose()},
        normalization=cg.NORMALIZE_CELLGRAPH_1,
    )
    cell = analysis.cells["a"]
    assert (cell.directives, cell.annotations, cell.diagnostics) == ([], {}, [])
    assert cell.plannable
    # The heading lines are the body, so they stay in its normalized text.
    assert cell.normalized == "# @worker big\n# @nocache maybe\nSome prose."
    assert cg.directive_keys(Prose()) == frozenset()
    assert cg.directive_keys(cg.PythonAnalyzer()) is None


def _every_line(directives):
    errors = [cg.AnnotationError(f"bad {d.value}", line=d.line) for d in directives if d.value]
    if errors:
        raise ExceptionGroup("check", errors)
    return True


def test_a_parser_reports_each_error_in_a_group():
    vocab = cg.Vocabulary([cg.Annotation("check", parse=_every_line, in_key=False)])
    analysis = cg.analyze([cg.Cell("a", "# @check one\n# @check\n# @check two\nx = 1")], vocab)
    assert [(d.code, d.line) for d in analysis.diagnostics] == [
        ("check_unreadable", 1),
        ("check_unreadable", 3),
    ]


def test_a_group_with_another_exception_is_not_swallowed():
    def parse(directives):
        raise ExceptionGroup("mixed", [cg.AnnotationError("bad"), KeyError("bug")])

    vocab = cg.Vocabulary([cg.Annotation("check", parse=parse, in_key=False)])
    with pytest.raises(ExceptionGroup) as raised:
        cg.analyze([cg.Cell("a", "# @check x\ny = 1")], vocab)
    assert isinstance(raised.value.exceptions[0], KeyError)


def test_a_validator_error_defaults_to_its_annotation_line():
    def validate(value, ctx):
        raise ExceptionGroup(
            "tag", [cg.AnnotationError("first"), cg.AnnotationError("second", line=7)]
        )

    vocab = cg.Vocabulary([cg.Annotation("tag", in_key=False, validate=validate)])
    analysis = cg.analyze([cg.Cell("a", "# note\n# @tag ok\nx = 1")], vocab)
    assert [(d.code, d.line) for d in analysis.diagnostics] == [
        ("tag_invalid", 2),
        ("tag_invalid", 7),
    ]


def _readable_lines(directives):
    good = [d.value for d in directives if d.value.isidentifier()]
    bad = [cg.AnnotationError("bad", line=d.line) for d in directives if not d.value.isidentifier()]
    return cg.Partial(good, bad) if bad else good


def test_a_partial_value_keeps_its_effects_and_reports_its_errors():
    vocab = cg.Vocabulary(
        [
            cg.Annotation(
                "src",
                parse=_readable_lines,
                effects=lambda names: [cg.Binds(names)],
                encode=sorted,
            )
        ]
    )
    analysis = cg.analyze([cg.Cell("a", "# @src data\n# @src 1x\ny = data")], vocab)
    cell = analysis.cells["a"]
    assert cell.annotations["src"] == ["data"]
    assert cell.bound == ["data"] and cell.references == []
    assert cell.key_fields == {"src": ["data"]}
    assert [(d.code, d.line) for d in cell.diagnostics] == [("src_unreadable", 2)]
    assert not cell.plannable


class Notes(Prose):
    directives = frozenset({"app"})


def test_a_language_with_a_key_set_reads_only_those_keys(vocab):
    vocab = vocab | cg.Vocabulary([cg.Annotation("app", in_key=False)])
    source = "# @app hide\n# @after c0\n# @nocache maybe\n# @worker\nSome notes."
    analysis = cg.analyze(
        [cg.Cell("c0", "x = 1"), cg.Cell("c1", source, language="notes")],
        vocab,
        analyzers={"python": cg.PythonAnalyzer(), "notes": Notes()},
        normalization=cg.NORMALIZE_CELLGRAPH_1,
    )
    cell = analysis.cells["c1"]
    assert cell.annotations == {"app": "hide"}
    # The other ``# @`` lines are prose: no annotation, no edge, no diagnostic.
    assert cell.diagnostics == [] and cell.after == []
    assert edges(analysis) == []
    assert cell.plannable
    # Only the read key leaves the normalized text.
    assert cell.normalized == "# @after c0\n# @nocache maybe\n# @worker\nSome notes."


def test_directives_must_be_a_bool_or_a_frozenset():
    class Bad(Prose):
        directives = ["app"]

    with pytest.raises(TypeError, match="frozenset"):
        cg.directive_keys(Bad())


class Checked(Prose):
    """An analyzer that reports what the cell's first line asks for."""

    directives = True

    def analyze(self, cell):
        first = cg.GRAMMAR.strip(cell.source).splitlines()[0]
        names = first.split()
        severity = {"error": cg.Severity.ERROR, "warn": cg.Severity.WARN}.get(names[0])
        found = []
        if severity is not None:
            found = [cg.Diagnostic("checked", severity, "found", cell="wrong", line=2)]
            names = names[1:]
        return cg.Analyzed(defines=names[:1], references=names[1:], diagnostics=found)


@pytest.mark.parametrize(
    "dependents",
    [("y = x",), ("# @after c0\ny = 1",)],
    ids=["variable", "after"],
)
def test_an_analyzer_error_blocks_the_cell_and_its_dependents(vocab, dependents):
    cells = [cg.Cell("c0", "error x", language="checked")]
    cells += [cg.Cell(f"c{i + 1}", src) for i, src in enumerate(dependents)]
    analysis = cg.analyze(
        cells, vocab, analyzers={"python": cg.PythonAnalyzer(), "checked": Checked()}
    )
    (diag,) = analysis.cells["c0"].diagnostics
    # The diagnostic is the analyzed cell's, whatever the analyzer named.
    assert (diag.code, diag.severity, diag.cell, diag.line) == (
        "checked",
        cg.Severity.ERROR,
        "c0",
        2,
    )
    assert not analysis.cells["c0"].plannable
    assert not analysis.cells["c1"].plannable
    assert analysis.cells["c1"].blocked_by == ["c0"]
    assert [d.code for d in analysis.blocking("c1")] == ["checked"]


def test_an_analyzer_warning_does_not_block(vocab):
    analysis = cg.analyze(
        [cg.Cell("c0", "warn x", language="checked"), cg.Cell("c1", "y = x")],
        vocab,
        analyzers={"python": cg.PythonAnalyzer(), "checked": Checked()},
    )
    assert [d.code for d in analysis.diagnostics] == ["checked"]
    assert analysis.cells["c0"].plannable and analysis.cells["c1"].plannable
    assert analysis.cells["c0"].definition_key is not None


def test_an_analyzer_error_string_is_still_a_syntax_error():
    class Broken(Prose):
        def analyze(self, cell):
            return cg.Analyzed(error="cannot read", diagnostics=[])

    analysis = cg.analyze(
        [cg.Cell("a", "x", language="broken")],
        cg.Vocabulary.core(),
        analyzers={"broken": Broken()},
    )
    assert [d.code for d in analysis.diagnostics] == ["syntax_error"]
