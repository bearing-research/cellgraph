import pytest
from conftest import codes, edges
from test_languages import Dashes

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
    )
    cell = analysis.cells["c1"]
    assert cell.annotations == {"app": "hide"}
    # The other ``# @`` lines are prose: no annotation, no edge, no diagnostic.
    assert cell.diagnostics == [] and cell.after == []
    assert edges(analysis) == []
    assert cell.plannable
    # Only the read key leaves the normalized text.
    assert cell.normalized == "# @after c0\n# @nocache maybe\n# @worker\nSome notes."


def test_a_dash_language_reads_dash_directives(vocab):
    cells = [
        cg.Cell("c0", "x = 1"),
        cg.Cell("c1", "# @after c0\nz = 2"),
        cg.Cell("c2", "-- @after c1\n-- note\n-- @nocache\na", language="dashes"),
        cg.Cell("c3", "# @after c0\n# @nocach\nb", language="dashes"),
    ]
    analysis = cg.analyze(
        cells, vocab, analyzers={"python": cg.PythonAnalyzer(), "dashes": Dashes()}
    )
    # A Python cell in the same notebook still reads ``#``.
    assert analysis.cells["c1"].after == ["c0"]
    sql = analysis.cells["c2"]
    assert (sql.after, sql.nocache, sql.diagnostics) == (["c1"], True, [])
    # Under ``--`` a ``#`` line is body: no edge and no ``annotation_unknown``.
    body = analysis.cells["c3"]
    assert (body.directives, body.after, body.diagnostics) == ([], [], [])
    assert edges(analysis) == [("c0", "c1", ""), ("c1", "c2", "")]


class DashNotes(Notes):
    comment = "--"


def test_a_dash_language_with_a_key_set_reads_only_those_keys(vocab):
    vocab = vocab | cg.Vocabulary([cg.Annotation("app", in_key=False)])
    source = "-- @app hide\n-- @after c0\n# @app shown\nSome notes."
    analysis = cg.analyze(
        [cg.Cell("c0", "x = 1"), cg.Cell("c1", source, language="notes")],
        vocab,
        analyzers={"python": cg.PythonAnalyzer(), "notes": DashNotes()},
    )
    cell = analysis.cells["c1"]
    assert cell.annotations == {"app": "hide"}
    assert cell.diagnostics == [] and cell.after == [] and edges(analysis) == []
    assert cell.normalized == "-- @after c0\n# @app shown\nSome notes."


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


# --- robustness ---------------------------------------------------------------


def test_a_long_chain_is_analyzed():
    # Cycle detection walked the graph recursively, one frame per cell.
    n = 3000
    cells = [cg.Cell("c0", "x0 = 1")]
    cells += [cg.Cell(f"c{i}", f"x{i} = x{i - 1} + 1") for i in range(1, n)]
    analysis = cg.analyze(cells, cg.Vocabulary.core())
    assert analysis.graph.topological_order == [f"c{i}" for i in range(n)]
    assert analysis.diagnostics == []


def test_a_long_cycle_is_found():
    n = 1500
    cells = [cg.Cell("c0", f"# @after c{n - 1}\nx0 = 1")]
    cells += [cg.Cell(f"c{i}", f"x{i} = x{i - 1} + 1") for i in range(1, n)]
    analysis = cg.analyze(cells, cg.Vocabulary.core())
    assert {d.code for d in analysis.diagnostics} == {"cycle"}
    assert len(analysis.diagnostics) == n


def test_each_cycle_is_reported_on_its_own_cells(run):
    analysis = run("# @seed c1 b\na = b", "b = a", "# @seed c3 d\nc = d", "d = c")
    cycle = {d.cell: d.args["cycle"] for d in analysis.diagnostics if d.code == "cycle"}
    assert cycle == {
        "c0": ["c0", "c1", "c0"],
        "c1": ["c0", "c1", "c0"],
        "c2": ["c2", "c3", "c2"],
        "c3": ["c2", "c3", "c2"],
    }


def test_duplicate_cell_ids_are_refused(vocab):
    # One cell's analysis, its errors included, would silently replace the other's.
    cells = [cg.Cell("a", "x = ("), cg.Cell("b", "y = x"), cg.Cell("a", "x = 2")]
    with pytest.raises(ValueError, match="'a'"):
        cg.analyze(cells, vocab)


def test_an_inactive_member_is_not_wired_and_has_no_plan_or_key(run):
    analysis = run(
        "x = 1",
        "# @variant g a\ny = x",
        "# @variant g b\ny = x * 2",
        "z = y",
        groups=cg.GroupConfig(active={"g": "a"}),
    )
    assert analysis.graph.inactive_cells == {"c2"}
    # What it would read depends on the selection, so nothing is wired to or from it.
    assert analysis.graph.cell_upstream["c2"] == analysis.graph.cell_downstream["c2"] == []
    assert analysis.graph.cell_upstream["c3"] == ["c1"]
    with pytest.raises(cg.InactiveVariantError, match="select it"):
        analysis.plan("c2", ready=lambda _: False)
    with pytest.raises(cg.InactiveVariantError):
        analysis.execution_key("c2", published=lambda _: cg.Published(), env_hash="e" * 64)
    # Selected, it is wired and planned like any cell.
    selected = run(
        "x = 1",
        "# @variant g a\ny = x",
        "# @variant g b\ny = x * 2",
        groups=cg.GroupConfig(active={"g": "b"}),
    )
    assert selected.plan("c2", ready=lambda _: False) == ["c0", "c2"]
    # An InactiveVariantError is a NotPlannableError, for a host that catches only that.
    assert issubclass(cg.InactiveVariantError, cg.NotPlannableError)


def test_a_cell_read_only_by_an_inactive_member_is_a_leaf(run):
    analysis = run(
        "x = 1",
        "# @variant g a\ny = 2",
        "# @variant g b\ny = x",
        groups=cg.GroupConfig(active={"g": "a"}),
    )
    assert analysis.graph.leaves == analysis.dag.leaves == {"c0", "c1"}


def test_a_repeated_variant_name_feeds_no_one(run):
    analysis = run(
        "# @variant g a\ny = 1",
        "# @variant g b\ny = 2",
        "# @variant g a\ny = 3",
        "z = y",
        groups=cg.GroupConfig(active={"g": "b"}),
    )
    assert "c2" in analysis.graph.inactive_cells
    assert analysis.graph.cell_upstream["c3"] == ["c1"]
    assert analysis.cells["c3"].plannable


def test_a_repeated_name_in_a_sweep_reads_none_of_its_siblings(run):
    analysis = run(
        "x = 0",
        "# @variant s p\nx = x + 1",
        "# @variant s q\nx = x + 2",
        "# @variant s p\nx = x + 3",
        groups=cg.GroupConfig(modes={"s": "sweep"}),
    )
    assert analysis.graph.cell_upstream["c3"] == []


def test_a_collision_is_on_the_line_of_the_annotation_that_made_the_member():
    grp = cg.Vocabulary([cg.Annotation("grp", effects=lambda v: [cg.Member("g", v)])])
    cells = [
        cg.Cell("c0", "x = 0\n"),
        cg.Cell("c1", "# @grp a\nx = 1"),
        cg.Cell("c2", "# @grp a\nx = 2"),
    ]
    analysis = cg.analyze(cells, grp)
    assert [(d.code, d.annotation, d.line) for d in analysis.diagnostics] == [
        ("variant_collision", "grp", 1),
        ("variant_collision", "grp", 1),
    ]


def test_a_member_conflict_joins_no_group_whatever_the_order():
    grp = cg.Vocabulary([cg.Annotation("grp", effects=lambda v: [cg.Member("g", v)])])
    cells = [
        cg.Cell("c0", "# @variant g a\n# @grp b\nx = 1"),
        cg.Cell("c1", "# @variant g b\nx = 2"),
        cg.Cell("c2", "y = x"),
    ]
    results = []
    for vocab in (cg.Vocabulary.core() | grp, grp | cg.Vocabulary.core()):
        analysis = cg.analyze(cells, vocab)
        assert analysis.cells["c0"].member is None
        assert codes(analysis, "c0") == ["member_conflict"]
        results.append((edges(analysis), {c: a.plannable for c, a in analysis.cells.items()}))
    assert results[0] == results[1]
    assert results[0][1]["c1"]


def test_three_cells_sharing_a_variant_name_get_one_collision_each(run):
    analysis = run("# @variant g a\nm = 1", "# @variant g a\nm = 2", "# @variant g a\nm = 3")
    assert [(d.cell, d.code) for d in analysis.diagnostics] == [
        ("c0", "variant_collision"),
        ("c1", "variant_collision"),
        ("c2", "variant_collision"),
    ]
    assert all(d.args["cells"] == ["c0", "c1", "c2"] for d in analysis.diagnostics)


def test_a_switch_member_does_not_fan_out(run):
    sweep = cg.GroupConfig(modes={"s": "sweep"})
    analysis = run(
        "# @variant s p\nx = 1",
        "# @variant s q\nx = 2",
        "# @variant h one\n# @per_variant\ny = x",
        groups=sweep,
    )
    assert codes(analysis, "c2") == ["per_variant_on_variant_member"]
    assert "c2" not in analysis.graph.fanout_cells


def test_a_variant_member_is_not_an_each_cell(run):
    analysis = run("files = []", "# @variant g a\n# @each item in files\nr = item")
    assert codes(analysis, "c1") == ["each_on_variant_member"]
    assert analysis.graph.each_cells == {}


@pytest.mark.parametrize(
    "convert", [lambda s: {"a": 1}[s], int, lambda s: s + 1], ids=["key", "value", "type"]
)
def test_a_converter_that_cannot_take_a_value_is_unreadable(vocab, convert):
    vocab = vocab | cg.Vocabulary([cg.Annotation("tier", parse=cg.scalar(convert))])
    analysis = cg.analyze([cg.Cell("c0", "# @tier b\nx = 1")], vocab)
    assert codes(analysis) == ["tier_unreadable"]


def test_a_converters_own_annotation_error_keeps_its_code_and_line(vocab):
    def convert(value):
        raise cg.AnnotationError("bad size", code="size_bad", line=2)

    vocab = vocab | cg.Vocabulary([cg.Annotation("size", parse=cg.scalar(convert))])
    analysis = cg.analyze([cg.Cell("c0", "# @size 1\n# @size 2\nx = 1")], vocab)
    assert [(d.code, d.line) for d in analysis.diagnostics] == [("size_bad", 2)]


def test_a_bug_in_a_converter_propagates(vocab):
    def convert(value):
        raise RuntimeError("a bug in the converter")

    vocab = vocab | cg.Vocabulary([cg.Annotation("tier", parse=cg.scalar(convert))])
    with pytest.raises(RuntimeError):
        cg.analyze([cg.Cell("c0", "# @tier b\nx = 1")], vocab)


def test_an_encoder_that_raises_is_unencodable(vocab):
    def encode(value):
        raise cg.AnnotationError("no encoding")

    vocab = vocab | cg.Vocabulary([cg.Annotation("tier", encode=encode)])
    analysis = cg.analyze([cg.Cell("c0", "# @tier b\nx = 1")], vocab)
    assert [(d.code, d.line) for d in analysis.diagnostics] == [("tier_unencodable", 1)]
    assert analysis.cells["c0"].unkeyed == ["tier"]


def test_an_encoders_group_of_conversion_errors_is_unencodable(vocab):
    def encode(value):
        raise ExceptionGroup("two", [ValueError("one"), cg.AnnotationError("other")])

    vocab = vocab | cg.Vocabulary([cg.Annotation("tier", encode=encode)])
    analysis = cg.analyze([cg.Cell("c0", "# @tier b\nx = 1")], vocab)
    assert codes(analysis) == ["tier_unencodable", "tier_unencodable"]


def test_an_encoder_that_cannot_take_a_value_is_unencodable(vocab):
    vocab = vocab | cg.Vocabulary([cg.Annotation("tier", encode=lambda v: {"a": 1}[v])])
    analysis = cg.analyze([cg.Cell("c0", "# @tier b\nx = 1")], vocab)
    assert [(d.code, d.line) for d in analysis.diagnostics] == [("tier_unencodable", 1)]
    assert analysis.cells["c0"].unkeyed == ["tier"]


def test_an_unreadable_error_is_named_after_its_annotation(vocab):
    def parse(directives):
        raise cg.AnnotationError("bad")

    vocab = vocab | cg.Vocabulary([cg.Annotation("sql", parse=parse, also_reads=("connection",))])
    analysis = cg.analyze([cg.Cell("c0", "# @connection w\n# @sql q\nx = 1")], vocab)
    assert codes(analysis) == ["sql_unreadable"]


def test_a_validators_diagnostic_is_the_cells():
    def validate(value, context):
        return [cg.Diagnostic("tier_odd", cg.Severity.ERROR, "odd", None)]

    vocab = cg.Vocabulary.core() | cg.Vocabulary([cg.Annotation("tier", validate=validate)])
    analysis = cg.analyze([cg.Cell("c0", "# @tier b\nx = 1")], vocab)
    assert [(d.cell, d.code) for d in analysis.diagnostics] == [("c0", "tier_odd")]
    assert analysis.blocking("c0")[0].cell == "c0"


def test_a_conflict_is_on_the_line_of_the_annotation_that_caused_it(run):
    bound_twice = run("files = []", "# @each item in files\n# @source item\nr = item")
    assert [(d.code, d.annotation, d.line) for d in bound_twice.diagnostics] == [
        ("bind_conflict", "source", 2)
    ]
    bound_and_read = run("# @source q\n# @ext q\nx = q")
    assert [(d.code, d.annotation, d.line) for d in bound_and_read.diagnostics] == [
        ("bind_read_conflict", "source", 1)
    ]
    reads_source = run("files = []", "# @name n\n# @each item in files\nr = len(files)")
    assert [(d.code, d.annotation, d.line) for d in reads_source.diagnostics] == [
        ("each_reads_source", "each", 2)
    ]


def test_a_hosts_after_and_fan_out_annotations_place_their_diagnostics():
    host = cg.Vocabulary(
        [
            cg.Annotation(
                "dep", parse=cg.stacked(lambda d: d.value), effects=lambda v: [cg.After(v)]
            ),
            cg.Annotation("foreach", effects=lambda v: [cg.FanOut(over=v)], in_key=False),
        ]
    )
    analysis = cg.analyze([cg.Cell("c0", "# @dep zz\n# @foreach nothing\nr = 1")], host)
    assert [(d.code, d.annotation, d.line) for d in analysis.diagnostics] == [
        ("after_unknown_cell", "dep", 1),
        ("each_unknown_source", "foreach", 2),
    ]


def test_a_cycle_blocks_each_of_its_cells_once_per_blocker(run):
    analysis = run("# @after c2\na = 1", "# @after c0\nb = 1", "# @after c1\nc = 1", "d = a")
    assert analysis.cells["c1"].blocked_by == ["c1", "c0", "c2"]
    assert analysis.cells["c3"].blocked_by == ["c0", "c1", "c2"]
