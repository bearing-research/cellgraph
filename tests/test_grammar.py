import re

import pytest

from cellgraph.grammar import GRAMMAR, Directive, Grammar, source_lines


def test_the_block_ends_at_the_first_body_line():
    source = "# @a 1\n\n# plain\n# @b two words\nx = 1\n# @c later\n"
    assert GRAMMAR.directives(source) == [Directive("a", "1", 1), Directive("b", "two words", 4)]


def test_a_key_is_lower_cased_and_the_value_stripped():
    assert GRAMMAR.parse_directive("  #  @Worker   gpu-1  ") == ("worker", "gpu-1")
    assert GRAMMAR.parse_directive("# not a directive") is None


def test_strip_removes_the_whole_block():
    assert GRAMMAR.strip("# @a 1\n# note\n\nx = 1\ny = 2") == "x = 1\ny = 2"
    assert GRAMMAR.strip("# only comments\n") == ""


def test_strip_directives_keeps_other_comments():
    assert GRAMMAR.strip_directives("# @a 1\n# note\nx = 1  # @not-block\n") == (
        "# note\nx = 1  # @not-block"
    )


def test_set_replaces_the_first_and_drops_duplicates():
    source = "# @w a\n# @t 1\n# @w b\nx = 1\n"
    assert GRAMMAR.set_directive(source, "w", "c") == "# @w c\n# @t 1\nx = 1\n"


def test_set_inserts_after_the_last_directive():
    assert (
        GRAMMAR.set_directive("# @t 1\n# note\nx = 1", "w", "a") == "# @t 1\n# @w a\n# note\nx = 1"
    )
    assert GRAMMAR.set_directive("x = 1\n", "flag", "") == "# @flag\nx = 1\n"


def test_set_keeps_crlf():
    assert GRAMMAR.set_directive("# @w a\r\nx = 1\r\n", "w", "b") == "# @w b\r\nx = 1\r\n"


def test_remove_round_trips_with_parsing():
    source = "# @w a\n# @t 1\nx = 1\n"
    removed = GRAMMAR.remove_directive(source, "w")
    assert removed == "# @t 1\nx = 1\n"
    assert [d.key for d in GRAMMAR.directives(removed)] == ["t"]


def test_a_key_set_reads_and_strips_only_those_keys():
    source = "# @app hide\n# @after a1\n# note\nx = 1"
    assert GRAMMAR.directives(source, keys={"app"}) == [Directive("app", "hide", 1)]
    assert GRAMMAR.directives(source, keys=frozenset()) == []
    assert GRAMMAR.strip_directives(source, keys={"app"}) == "# @after a1\n# note\nx = 1"
    assert GRAMMAR.strip_directives(source, keys=frozenset()) == source


# --- a language whose comment marker is ``--`` ------------------------------------

DASHES = Grammar(marker="--")


def test_a_dash_grammar_reads_dash_directives():
    source = "-- @A 1\n\n-- note\n--@b two words\nSELECT 1\n-- @c later\n"
    assert DASHES.directives(source) == [Directive("a", "1", 1), Directive("b", "two words", 4)]
    assert DASHES.parse_directive("  --  @Worker   gpu-1  ") == ("worker", "gpu-1")


def test_under_dashes_a_hash_line_is_body():
    source = "# @a 1\nSELECT 1"
    assert DASHES.directives(source) == []
    assert list(DASHES.block(source)) == []
    assert DASHES.strip(source) == source
    assert DASHES.strip_directives(source) == source


def test_under_dashes_a_hash_line_ends_the_block():
    source = "-- @a 1\n# x\n-- @b 2\nSELECT 1"
    assert DASHES.directives(source) == [Directive("a", "1", 1)]
    assert DASHES.strip(source) == "# x\n-- @b 2\nSELECT 1"
    assert DASHES.strip_directives(source) == "# x\n-- @b 2\nSELECT 1"


def test_dash_strip_keeps_body_comments():
    source = "-- @a 1\n-- note\nSELECT 1 -- tail\n-- @x body\n"
    assert DASHES.strip(source) == "SELECT 1 -- tail\n-- @x body"
    assert DASHES.strip_directives(source) == "-- note\nSELECT 1 -- tail\n-- @x body"
    assert DASHES.strip_directives(source, keys={"b"}) == source.rstrip("\n")
    assert DASHES.strip_directives("-- @a 1\n-- @b 2\nSELECT 1", keys={"b"}) == "-- @a 1\nSELECT 1"


def test_dash_set_replaces_dedupes_and_inserts():
    source = "-- @w a\n-- @t 1\n-- @w b\nSELECT 1\n"
    assert DASHES.set_directive(source, "w", "c") == "-- @w c\n-- @t 1\nSELECT 1\n"
    assert (
        DASHES.set_directive("-- @t 1\n-- note\nSELECT 1", "w", "a")
        == "-- @t 1\n-- @w a\n-- note\nSELECT 1"
    )
    assert DASHES.set_directive("SELECT 1\n", "flag", "") == "-- @flag\nSELECT 1\n"
    assert DASHES.set_directive("-- @w a\r\nSELECT 1\r\n", "w", "b") == "-- @w b\r\nSELECT 1\r\n"
    # A ``#`` line is body, so the directive goes in a new block above it.
    assert DASHES.set_directive("# @w a\nSELECT 1", "w", "b") == "-- @w b\n# @w a\nSELECT 1"


def test_dash_remove_round_trips_with_parsing():
    source = "-- @w a\n-- @t 1\nSELECT 1\n-- @w body\n"
    removed = DASHES.remove_directive(source, "w")
    assert removed == "-- @t 1\nSELECT 1\n-- @w body\n"
    assert [d.key for d in DASHES.directives(removed)] == ["t"]


def test_with_marker_is_a_copy_that_keeps_a_subclass():
    class Upper(Grammar):
        def _format(self, key, value):
            return super()._format(key.upper(), value)

    upper = Upper()
    dashes = upper.with_marker("--")
    assert type(dashes) is Upper and (upper.marker, dashes.marker) == ("#", "--")
    assert upper.with_marker("#") is upper
    assert dashes.set_directive("SELECT 1", "w", "a") == "-- @W a\nSELECT 1"
    assert GRAMMAR.with_marker("--").directives("-- @a 1") == [Directive("a", "1", 1)]
    assert GRAMMAR.marker == "#"


def test_a_marker_is_matched_literally():
    assert Grammar("*").directives("* @a 1\n*x\nbody") == [Directive("a", "1", 1)]
    assert Grammar("*").directives("x @a 1") == []


@pytest.mark.parametrize("marker", ["", "-- ", 3, "@", "#@"])
def test_a_bad_marker_is_refused(marker):
    with pytest.raises(TypeError, match="comment marker"):
        Grammar(marker)


def test_a_subclass_regex_and_init_work_as_before():
    class Quarto(Grammar):
        directive_re = re.compile(r"^#\|\s*(\w+):\s*(.*?)\s*$")

    assert Quarto().directives("#| name: x\nprint(1)") == [Directive("name", "x", 1)]

    class OwnInit(Grammar):
        def __init__(self, x=1):
            self.x = x

    assert OwnInit().directives("# @name x\ny = 1") == [Directive("name", "x", 1)]
    assert OwnInit().with_marker("--").directives("-- @a 1") == [Directive("a", "1", 1)]


def test_a_line_breaks_only_where_python_breaks_it():
    assert source_lines("a\r\nb\rc\nd\n") == ["a", "b", "c", "d"]
    assert source_lines("a\x0cb\u2028c") == ["a\x0cb\u2028c"]
    assert source_lines("") == []


@pytest.mark.parametrize("brk", ["\x0b", "\x0c", "\x1c", "\x85", "\u2028", "\u2029"])
def test_a_block_line_holding_another_break_is_body(brk):
    # Python reads one comment; JavaScript breaks at \u2028, and reads code after it.
    source = f"# @a 1{brk}x = 1\ny = 2"
    assert GRAMMAR.directives(source) == []
    assert GRAMMAR.strip_directives(source) == source
    assert GRAMMAR.strip(source) == source
    # As blank space around a line, it is not inside it.
    assert GRAMMAR.directives(f"{brk}\n# @a 1{brk}\nx = 1") == [Directive("a", "1", 2)]


def test_a_splice_keeps_a_lone_cr():
    assert GRAMMAR.set_directive("# @a 1\rx = 1\r", "b", "2") == "# @a 1\r# @b 2\rx = 1\r"
    assert GRAMMAR.remove_directive("# @a 1\rx = 1\r", "a") == "x = 1\r"


def test_a_splice_leaves_a_line_holding_another_break_whole():
    source = "# @a 1\x0cx = 1\n"
    assert GRAMMAR.set_directive(source, "a", "2") == "# @a 2\n# @a 1\x0cx = 1\n"
    assert GRAMMAR.remove_directive(source, "a") == source
