import pytest
from conftest import codes, edges

import cellgraph as cg


def test_an_unknown_key_warns_with_a_hint_or_errors_when_strict(run):
    lax = run("# @nocach\nx = 1")
    (diag,) = lax.diagnostics
    assert (diag.code, diag.severity) == ("annotation_unknown", cg.Severity.WARN)
    assert "`@nocache`" in diag.message
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


def test_keeps_marks_a_defined_name_consumed(run):
    analysis = run("# @keep x\nx = 1\ny = 2", "# @keep nope\nz = 1")
    assert analysis.graph.consumed_variables["c0"] == {"x"}
    assert analysis.graph.consumed_variables["c1"] == set()


def test_after_edges_and_their_diagnostics(run):
    analysis = run("x = 1", "# @after c0 c1 c7\ny = 1")
    assert ("c0", "c1", "") in edges(analysis)
    assert codes(analysis, "c1") == ["after_self", "after_unknown_cell"]


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
    assert not cg.reads_directives(Prose()) and cg.reads_directives(cg.PythonAnalyzer())


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
