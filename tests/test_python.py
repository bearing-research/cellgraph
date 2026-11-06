import sys
from concurrent.futures import ThreadPoolExecutor

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
    # A lambda or a generator is read where it is written, since most run at once;
    # a lambda's walrus binds in the lambda.
    ("f = lambda: (y := 1)", ["f"], [], []),
    ("f = lambda: y\ny = 1", ["f", "y"], ["y"], []),
    ("f = lambda: k\nf()\nk = 1", ["f", "k"], ["k"], []),
    ("f = lambda: y", ["f"], ["y"], []),
    ("f = lambda x, *a, k, **kw: (x, a, k, kw, y)", ["f"], ["y"], []),
    ("class C:\n    n = 1\n    f = lambda: n", ["C"], ["n"], []),
    ("g = (a + z for a in xs)\nz = 1", ["g", "z"], ["xs", "z"], []),
    ("s = sum(y for _ in range(3))\ny = 2", ["s", "y"], ["y"], []),
    ("g = (a + z for a in xs)", ["g"], ["xs", "z"], []),
    # A def body still runs later, so a name the cell binds after it is the cell's own.
    ("def f():\n    return y\ny = 1", ["f", "y"], [], []),
    # A bare annotation binds nothing.
    ("x: int", [], [], []),
    ("x: int = 1", ["x"], [], []),
    ("obj.a: int", [], ["obj"], []),
    # An index in an assignment target is read.
    ("df[col] = 1", ["df"], ["col", "df"], ["df"]),
    ("grid[i][j] = 0", ["grid"], ["grid", "i", "j"], ["grid"]),
    ("del d[k]", ["d"], ["d", "k"], ["d"]),
    # ``except E as e`` unbinds e when the handler ends, so e is the handler's, also
    # to a lambda or def in the handler, in a class body or not.
    ("try:\n    pass\nexcept ValueError as e:\n    print(e)", [], [], []),
    ("try:\n    pass\nexcept E as e:\n    f = lambda: e", ["f"], ["E"], []),
    ("try:\n    pass\nexcept E as e:\n    def g():\n        return e", ["g"], ["E"], []),
    (
        "class C:\n    try:\n        pass\n    except E as e:\n        f = lambda: e",
        ["C"],
        ["E"],
        [],
    ),
    # Only the handler's: a def elsewhere reading e still reads the module's.
    ("try:\n    pass\nexcept E as e:\n    pass\ndef g():\n    return e", ["g"], ["E", "e"], []),
    # A handler inside a loop may continue it.
    (
        "for x in xs:\n    try:\n        f(x)\n    except E as e:\n        continue",
        ["x"],
        ["E", "f", "xs"],
        [],
    ),
    # A name a class binds, a mutation in the class body is the class's own.
    ("class A:\n    class Meta:\n        pass\n    Meta.ordering = ['x']", ["A"], [], []),
    ("class A:\n    def m(self):\n        pass\n    m.flag = True", ["A"], [], []),
    ("class A:\n    import os\n    os.sep = '/'", ["A"], [], []),
    # Likewise at module level: a def, class or import binds the name first.
    ("def m():\n    pass\nm.flag = True", ["m"], [], []),
    ("import os\nos.environ['K'] = '1'", ["os"], [], []),
    # A nested class body cannot see its enclosing class's names.
    ("class A:\n    x = 2\n    class B:\n        y = x\nx = 1", ["A", "x"], ["x"], []),
    # ... but its bases are evaluated in the enclosing class body.
    ("class A:\n    Base = object\n    class B(Base):\n        pass", ["A"], [], []),
    # ``del x`` unbinds x; binding it again defines it again. A ``del`` that may not
    # run (under ``if`` or ``try``) unbinds nothing, nor one a ``global`` write follows.
    ("x = 1\ndel x", [], [], []),
    ("x = 1\ndel x\nx = 2", ["x"], [], []),
    ("del x", [], ["x"], []),
    ("x = 1\nif reset:\n    del x", ["x"], ["reset"], []),
    ("c = load()\ntry:\n    del c\nexcept NameError:\n    pass", ["c"], ["load"], []),
    ("del x\ndef f():\n    global x\n    x = 2", ["f", "x"], ["x"], []),
    # Rebinding or mutating a handler's ``e`` binds nothing past the handler.
    ("try:\n    run()\nexcept E as e:\n    e.args = ()", [], ["E", "run"], []),
    ("try:\n    run()\nexcept E as err:\n    err = str(err)", [], ["E", "run"], []),
    # A star import in a handler leaves the cell's function bodies readable.
    (
        "try:\n    from fast import *\nexcept ImportError as e:\n    from slow import *\n"
        "    f = lambda: e\ndef g():\n    return df",
        ["f", "g"],
        ["df"],
        [],
    ),
    # A generator or a def parameter in a handler reads e as the handler's, or its own.
    ("try:\n    pass\nexcept E as e:\n    g = (e for _ in r)", ["g"], ["E", "r"], []),
    ("try:\n    pass\nexcept E as e:\n    def h(e):\n        return e + q", ["h"], ["E", "q"], []),
    # A top-level await in a def's default or a lambda's: its bodies are still read.
    ("def h(a=await g()):\n    return up", ["h"], ["g", "up"], []),
    ("f = lambda a=await g(): a\ndef h():\n    return up", ["f", "h"], ["g", "up"], []),
    # A walrus in a lambda's comprehension binds in the lambda, for the rest of it.
    ("f = lambda: [y := i for i in r] + [y]", ["f"], ["r"], []),
    # A form feed or a line separator in the source misplaces nothing.
    ("r = await g()\n\x0c\ndef f():\n    return z", ["f", "r"], ["g", "z"], []),
    ("s = 'a\u2028b'\nawait g()\ndef f():\n    return z", ["f", "s"], ["g", "z"], []),
]


@pytest.mark.parametrize(("source", "defines", "references", "mutations"), CASES)
def test_names(source, defines, references, mutations):
    assert names(source) == (defines, references, mutations)


def test_a_long_expression_is_analyzed():
    # Each ``+`` nests the tree one level, past the default recursion limit.
    deep = "x = " + " + ".join(["a"] * 3000)
    assert names(deep) == (["x"], ["a"], [])
    assert normalize(deep) == deep


def test_the_deep_retry_starts_afresh():
    # The first walk fails inside the handler; a reused analyzer would keep e local.
    deep = " + ".join(["a"] * 3000)
    source = f"try:\n    pass\nexcept E as e:\n    w = {deep}\nprint(e)"
    assert analyze_cell(source).references == ["E", "a", "e"]


def test_deep_cells_on_threads_leave_the_recursion_limit_alone():
    limit = sys.getrecursionlimit()
    sources = [f"x{i} = " + " + ".join(["a"] * 3000) for i in range(8)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(analyze_cell, sources))
    assert all(r.references == ["a"] for r in results)
    assert sys.getrecursionlimit() == limit


def test_a_cell_symtable_refuses_warns_that_its_bodies_are_unread():
    analysis = cg.analyze(
        [cg.Cell("a", "nonlocal x\ndef f():\n    return y")], cg.Vocabulary.core()
    )
    assert [(d.code, d.severity) for d in analysis.diagnostics] == [
        ("python_bodies_unread", cg.Severity.WARN)
    ]
    assert analysis.cells["a"].plannable


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
