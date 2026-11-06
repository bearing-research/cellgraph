import pytest

import cellgraph as cg
from cellgraph import python
from cellgraph.python import analyze_cell, normalize


def test_defines_and_references():
    result = analyze_cell("import numpy as np\ny = np.sum(x)\ndef f():\n    return z\n")
    assert set(result.defines) == {"np", "y", "f"}
    assert set(result.references) == {"x", "z"}


def test_a_builtin_shadow_is_kept_apart():
    result = analyze_cell("y = input + 1")
    assert "input" in result.builtin_references
    assert "input" not in result.references


def test_normalize_drops_comments_and_layout():
    assert normalize("x  =  1  # note\n\n") == normalize("x = 1")
    assert normalize("x = (\n") == "x = ("


def test_a_syntax_error_is_a_diagnostic():
    analysis = cg.analyze([cg.Cell("a", "x = (")], cg.Vocabulary.core())
    assert [d.code for d in analysis.diagnostics] == ["syntax_error"]
    assert not analysis.cells["a"].plannable


def test_the_memo_hands_out_its_own_lists():
    """A memoized result is copied, so one caller's change is not the next caller's."""
    analyzer = cg.get_language_analyzer("python")
    first = analyzer.analyze(cg.Cell("a", "x = y", "python"))
    first.defines.append("z")
    first.references.clear()
    second = analyzer.analyze(cg.Cell("b", "x = y", "python"))
    assert (second.defines, second.references) == (["x"], ["y"])


def names(source):
    result = analyze_cell(source)
    return result.defines, result.references, result.mutation_defines


# (source, defines, references, mutation_defines): each a missed or a spurious edge.
CASES = [
    # Top-level await, async for and async with: Python 3.14's symtable refuses them,
    # which once dropped every name read in a function or class body.
    ("def f():\n    return y\nawait g()", ["f"], ["g", "y"], []),
    ("class C:\n    def m(self):\n        return cfg\nawait setup()", ["C"], ["cfg", "setup"], []),
    ("def f():\n    global n\n    n = 1\nawait g()", ["f", "n"], ["g"], []),
    ("async for x in src():\n    print(x)", ["x"], ["src"], []),
    (
        "async with opener() as f:\n    use(f)\ndef h():\n    return z",
        ["f", "h"],
        ["opener", "use", "z"],
        [],
    ),
    ("[v async for v in src()]", [], ["src"], []),
    # The statements symtable refuses are read one by one: an async def beside them
    # keeps its awaits, and a lambda inside one still reads its globals.
    ("async def f():\n    await h()\n    return y\nawait f()", ["f"], ["h", "y"], []),
    ("x = [await f(lambda: y) for _ in r]", ["x"], ["f", "r", "y"], []),
    # ``_ = expr`` still runs expr.
    ("_ = plt.plot(df.x)", [], ["df", "plt"], []),
    # ``global n; n += 1`` reads n, as ``n = n + 1`` does.
    ("def f():\n    global n\n    n += 1\nf()", ["f", "n"], ["n"], ["n"]),
    ("def f():\n    global n\n    n = n + 1\nf()", ["f", "n"], ["n"], ["n"]),
    # A mutation or a del reads the name even when the cell rebinds it later.
    ("df['c'] = 1\ndf = None", ["df"], ["df"], []),
    ("d['k'] += 1\nd = {}", ["d"], ["d"], []),
    ("del x\nx = 1", ["x"], ["x"], []),
    # ``del`` of an item or an attribute mutates its root.
    ("del df['c']", ["df"], ["df"], ["df"]),
    ("del obj.attr", ["obj"], ["obj"], ["obj"]),
    # A class body runs when the class is defined, in order.
    ("class C:\n    y = Z\nZ = 1", ["C", "Z"], ["Z"], []),
    ("class C:\n    x = x", ["C"], ["x"], []),
    ("class C:\n    n = 3\n    m = n + 1", ["C"], [], []),
    (
        "@register(k)\nclass C(Base, metaclass=M):\n    pass",
        ["C"],
        ["Base", "M", "k", "register"],
        [],
    ),
    # A class body that mutates a module name produces it.
    ("class C:\n    df['x'] = 1", ["C", "df"], ["df"], ["df"]),
    # A comprehension in a class body cannot see the class's names: it reads globals.
    ("class C:\n    n = 3\n    vals = [i * n for i in range(2)]", ["C"], ["n"], []),
    # A method body runs later, so a name the cell binds after it is the cell's own.
    ("class C:\n    def m(self):\n        return k\nk = 1", ["C", "k"], [], []),
    # A lambda body runs later, like a def body; its walrus binds in the lambda.
    ("f = lambda: (y := 1)", ["f"], [], []),
    ("f = lambda: y\ny = 1", ["f", "y"], [], []),
    ("f = lambda: y", ["f"], ["y"], []),
    # A generator's elements run when it is iterated; its first iterable runs now.
    ("g = (a + z for a in xs)\nz = 1", ["g", "z"], ["xs"], []),
    ("g = (a + z for a in xs)", ["g"], ["xs", "z"], []),
    # A bare annotation binds nothing.
    ("x: int", [], [], []),
    ("x: int = 1", ["x"], [], []),
    ("obj.a: int", [], ["obj"], []),
    # An index in an assignment target is read.
    ("df[col] = 1", ["df"], ["col", "df"], ["df"]),
    ("grid[i][j] = 0", ["grid"], ["grid", "i", "j"], ["grid"]),
    ("del d[k]", ["d"], ["d", "k"], ["d"]),
    # ``except E as e`` unbinds e when the handler ends, so e is the handler's.
    ("try:\n    pass\nexcept ValueError as e:\n    print(e)", [], [], []),
]


@pytest.mark.parametrize(("source", "defines", "references", "mutations"), CASES)
def test_names(source, defines, references, mutations):
    assert names(source) == (defines, references, mutations)


def test_a_long_expression_is_analyzed():
    # Each ``+`` nests the tree one level, past the default recursion limit.
    deep = "x = " + " + ".join(["a"] * 3000)
    assert names(deep) == (["x"], ["a"], [])
    assert normalize(deep) == deep


def test_a_cell_too_deep_to_walk_is_a_diagnostic_not_a_crash(monkeypatch):
    def too_deep(self, node):
        raise RecursionError

    python._analyzed("y = 1")  # b is analyzed, and memoized, before the walk breaks
    monkeypatch.setattr(python.VariableAnalyzer, "visit", too_deep)
    source = "x = 1  # unique to this test, past the memo"
    analysis = cg.analyze([cg.Cell("a", source), cg.Cell("b", "y = 1")], cg.Vocabulary.core())
    assert [(d.cell, d.code) for d in analysis.diagnostics] == [("a", "python_too_deep")]
    assert not analysis.cells["a"].plannable and analysis.cells["b"].plannable


def test_normalize_survives_what_unparse_refuses():
    huge = "x = 0x" + "f" * 5000
    assert normalize(huge) == huge
