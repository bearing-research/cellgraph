import pytest

import cellgraph as cg


class Words:
    owns_key = True

    def analyze(self, cell):
        body = cg.GRAMMAR.strip(cell.source).split()
        return cg.Analyzed(defines=body[:1], references=body[1:])

    def normalize(self, body):
        return cg.strip_lines(body)


def test_strip_lines():
    assert cg.strip_lines("\n  a  \n\tb \n\n") == "a\n\tb"


def test_a_registered_analyzer_is_used_by_language():
    cg.register_language_analyzer("words", Words())
    analysis = cg.analyze(
        [cg.Cell("a", "x = 1"), cg.Cell("b", "y x", language="words")], cg.Vocabulary.core()
    )
    assert analysis.cells["b"].defines == ["y"]
    assert analysis.graph.cell_upstream["b"] == ["a"]


def test_an_unknown_language_is_an_error_not_a_silent_drop():
    with pytest.raises(cg.UnknownLanguageError):
        cg.analyze([cg.Cell("a", "x", language="cobol")], cg.Vocabulary.core())
    with pytest.raises(cg.UnknownLanguageError):
        cg.get_language_analyzer("cobol")


def test_explicit_analyzers_replace_the_registry():
    analysis = cg.analyze(
        [cg.Cell("a", "y x", language="words")],
        cg.Vocabulary.core(),
        analyzers={"words": Words()},
    )
    assert analysis.cells["a"].references == ["x"]
