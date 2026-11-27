"""Call readers: names a call reads that its code does not show, given by the host."""

import ast

import pytest

import cellgraph as cg
from cellgraph.python import analyze_cell

ENV = "e" * 64


def pandas(call):
    """A host's rule: ``.query`` / ``.eval`` on any receiver, a literal first argument or expr=."""
    func = call.func
    if not (isinstance(func, ast.Attribute) and func.attr in ("query", "eval")):
        return None
    expr = call.args[0] if call.args else None
    expr = expr or next((k.value for k in call.keywords if k.arg == "expr"), None)
    text = cg.string_literal(expr)
    return cg.at_names(text) if text is not None else None


def refs(source, readers=(pandas,)):
    return analyze_cell(source, call_readers=readers).references


# --- at_names and string_literal --------------------------------------------------


@pytest.mark.parametrize(
    ("text", "names"),
    [
        ("a > @mu", ["mu"]),
        ("(b > @mu - 5 * @sig) & (b < @mu + 5 * @sig)", ["mu", "sig"]),
        ("@ mu", ["mu"]),
        ("@obj.attr > 1", ["obj"]),
        ("email == 'a@b.com'", []),
        ('name == "x@y" and @z', ["z"]),
        ("`col@name` > @t", ["t"]),
        (r"s == 'it\'s @no' or @yes", ["yes"]),
        ("@1 + @", []),
        ("@if", []),
        ("@été", ["été"]),
        ("'unclosed @x", []),
    ],
)
def test_at_names(text, names):
    assert cg.at_names(text) == names


def test_string_literal():
    def arg(src):
        return ast.parse(src).body[0].value.args[0]

    assert cg.string_literal(arg('f("a" "b")')) == "ab"
    assert cg.string_literal(arg('f(f"{x}")')) is None
    assert cg.string_literal(arg("f(x)")) is None
    assert cg.string_literal(arg("f(b'x')")) is None
    assert cg.string_literal(None) is None


# --- where a reader's names are read ----------------------------------------------


def test_the_issue_repro_reads_mu():
    assert refs('print(df.query("a > @mu"))') == ["df", "mu"]


def test_no_readers_change_nothing():
    assert refs('print(df.query("a > @mu"))', readers=()) == ["df"]
    assert cg.PythonAnalyzer().call_readers == ()


def test_keyword_expr():
    assert refs('pd.eval(expr="@mu + 1")') == ["mu", "pd"]


def test_names_follow_scope_at_module_level():
    # A comprehension variable and a lambda parameter stay local.
    assert refs('[df.query("@k") for k in ks]') == ["df", "ks"]
    assert refs('f = lambda mu: df.query("@mu")') == ["df"]
    # Read after the call's arguments, before the assignment binds.
    assert refs('mu = df.query("@mu")') == ["df", "mu"]
    # A name the cell binds first is its own.
    assert refs('mu = 2\ndf.query("@mu")') == ["df"]
    assert refs('class C:\n    x = df.query("@mu")') == ["df", "mu"]


def test_names_in_function_bodies():
    assert refs('def f():\n    return df.query("@mu")') == ["df", "mu"]
    assert refs('def f(mu):\n    return df.query("@mu")') == ["df"]
    assert refs('def f():\n    mu = 1\n    return df.query("@mu")') == ["df"]
    assert refs('async def f():\n    return df.query("@mu")') == ["df", "mu"]
    assert refs('class C:\n    def m(self):\n        return self.df.eval("@mu")') == ["mu"]


def test_an_enclosing_functions_name_is_the_modules_unless_captured():
    # The library looks in the calling frame, which has ``mu`` only when captured.
    uncaptured = 'def outer():\n    mu = 1\n    def inner():\n        return df.query("@mu")'
    assert refs(uncaptured) == ["df", "mu"]
    captured = (
        "def outer():\n    mu = 1\n    def inner():\n"
        '        print(mu)\n        return df.query("@mu")'
    )
    assert refs(captured) == ["df"]


def test_what_a_reader_does_not_answer_reads_nothing():
    assert refs('df.query(f"a > @{x}")') == ["df", "x"]
    assert refs("df.query(expr)") == ["df", "expr"]
    assert refs('model.eval("@mu", local_dict=d)') == ["d", "model", "mu"]


def test_private_names_and_builtins_are_filtered_as_usual():
    result = analyze_cell('df.query("@_mu > @len")', call_readers=[pandas])
    assert result.references == ["df"]
    assert result.builtin_references == ["len"]


def test_a_global_write_the_function_also_reads_stays_a_reference():
    result = analyze_cell(
        'def f():\n    global mu\n    mu = df.query("@mu")', call_readers=[pandas]
    )
    assert result.defines == ["f", "mu"]
    assert "mu" in result.references


# --- readers that go wrong ----------------------------------------------------------


def test_a_failing_reader_is_a_warning_and_the_rest_stands():
    def boom(call):
        raise RuntimeError("nope")

    result = analyze_cell('x = df.query("@mu")', call_readers=[boom, pandas])
    assert result.references == ["df", "mu"]
    assert result.defines == ["x"]
    assert result.reader_failures == [
        "test_a_failing_reader_is_a_warning_and_the_rest_stands"
        ".<locals>.boom raised RuntimeError: nope"
    ]


@pytest.mark.parametrize(
    ("answer", "failure"),
    [
        ("mu", "returned str, not an iterable of names"),
        (["mu", "a.b"], "returned 'a.b', not a name"),
        (["class"], "returned 'class', not a name"),
        ([1], "returned 1, not a name"),
    ],
)
def test_a_reader_answer_that_is_not_names(answer, failure):
    result = analyze_cell('df.query("@mu")', call_readers=[lambda call: answer])
    assert any(failure in line for line in result.reader_failures)
    assert result.references == (["df", "mu"] if answer == ["mu", "a.b"] else ["df"])


def test_the_failure_is_a_diagnostic(vocab):
    def boom(call):
        raise ZeroDivisionError("division by zero")

    analysis = cg.analyze(
        [cg.Cell("a", 'df.query("@mu")')],
        vocab,
        analyzers={"python": cg.PythonAnalyzer(call_readers=[boom])},
    )
    (diagnostic,) = [d for d in analysis.diagnostics if d.code == "python_call_reader_failed"]
    assert diagnostic.severity == cg.Severity.WARN
    assert diagnostic.cell == "a"
    assert "ZeroDivisionError" in diagnostic.message


def test_a_reader_must_be_callable():
    with pytest.raises(TypeError, match="callable"):
        cg.PythonAnalyzer(call_readers=["pandas"])  # ty: ignore[invalid-argument-type]


# --- through analyze and the key -----------------------------------------------------


def analyzed(vocab, *sources, readers=(pandas,)):
    cells = [cg.Cell(f"c{i}", src) for i, src in enumerate(sources)]
    python = cg.PythonAnalyzer(call_readers=readers)
    return cg.analyze(cells, vocab, analyzers={"python": python})


def test_the_reading_cell_gets_the_edge_and_the_key(vocab):
    analysis = analyzed(vocab, "mu = 2\ndf = load()", 'print(df.query("a > @mu"))')
    assert {(e.from_cell_id, e.variable) for e in analysis.graph.edges if e.to_cell_id == "c1"} == {
        ("c0", "df"),
        ("c0", "mu"),
    }

    def key(mu):
        snapshot = cg.Published(
            outputs={
                "df": cg.OutputRef(cg.Provenance("df-hash")),
                "mu": cg.OutputRef(cg.Provenance(mu)),
            }
        )
        return analysis.execution_key("c1", published=lambda _: snapshot, env_hash=ENV).digest

    assert key("mu-2") != key("mu-3")


def test_each_analyzer_keeps_its_own_answers(vocab):
    source = 'print(df.query("a > @mu"))'
    with_reader = analyzed(vocab, source)
    without = analyzed(vocab, source, readers=())
    again = analyzed(vocab, source)
    assert with_reader.cells["c0"].references == ["df", "mu"]
    assert without.cells["c0"].references == ["df"]
    assert again.cells["c0"].references == ["df", "mu"]


def test_a_cell_that_cannot_be_written_back_keeps_its_module_level_names():
    # A decimal this long could not be parsed; a hex one parses, and cannot be unparsed.
    source = f'x = 0x{"f" * 4000}\ndf.query("@mu")\ndef f():\n    return df.query("@sig")'
    result = analyze_cell(source, call_readers=[pandas])
    assert result.references == ["df", "mu"]
    assert result.defines == ["f", "x"]
    (failure,) = result.reader_failures
    assert "function bodies" in failure


def module_reader(call):
    return ["mu"] if isinstance(call.func, ast.Attribute) and call.func.attr == "query" else None


def test_names_are_read_as_python_reads_identifiers():
    # NFKC, the same at module level and in a function body; a keyword lookalike is none.
    assert cg.at_names("@ﬁ") == ["fi"]
    assert refs('df.query("@ﬁ")') == ["df", "fi"]
    assert refs('def g():\n    return df.query("@ﬁ")') == ["df", "fi"]
    assert refs('df.query("@\U0001d422\U0001d41f")') == ["df"]
    assert analyze_cell('df.query("x")', call_readers=[lambda c: ["ﬁ"]]).references == [
        "df",
        "fi",
    ]


def test_a_cell_that_cannot_be_written_back_warns_only_for_function_bodies():
    source = f'x = 0x{"f" * 4000}\ndf.query("@mu")'
    result = analyze_cell(source, call_readers=[pandas])
    assert result.references == ["df", "mu"]
    assert result.reader_failures == []


def test_a_failure_message_is_one_short_line():
    def noisy(call):
        raise ValueError("first line\n" + "x" * 500)

    (failure,) = analyze_cell('df.query("@mu")', call_readers=[noisy]).reader_failures
    assert failure.endswith("raised ValueError: first line")


def test_the_analyzer_pickles_and_its_readers_are_fixed():
    import pickle

    analyzer = cg.PythonAnalyzer(call_readers=[module_reader])
    assert pickle.loads(pickle.dumps(analyzer)).call_readers == (module_reader,)
    with pytest.raises(AttributeError):
        analyzer.call_readers = ()  # ty: ignore[invalid-assignment]


def test_a_reader_must_be_hashable():
    class Unhashable:
        __hash__ = None

        def __call__(self, call):
            return None

    with pytest.raises(TypeError, match="hashable"):
        cg.PythonAnalyzer(call_readers=[Unhashable()])
