import cellgraph as cg
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
