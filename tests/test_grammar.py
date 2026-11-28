import re

import pytest

from cellgraph.grammar import GRAMMAR, Directive, Grammar, source_lines
from cellgraph.vocabulary import Annotation


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


# --- found by the audit -------------------------------------------------------------


def test_a_long_run_of_whitespace_parses_in_linear_time():
    import time

    start = time.perf_counter()
    assert GRAMMAR.parse_directive("# @a x" + " \t" * 20000 + "y") == (
        "a",
        "x" + " \t" * 20000 + "y",
    )
    assert time.perf_counter() - start < 0.5


@pytest.mark.parametrize("value", ["a\nimport os", "a\rb", "a b"])
def test_a_value_with_a_line_break_is_refused(value):
    with pytest.raises(ValueError, match="one line"):
        GRAMMAR.set_directive("x = 1\n", "name", value)


@pytest.mark.parametrize("key", ["per-variant", "", "a b", "İD", "name:"])
def test_a_key_the_grammar_would_not_read_back_is_refused(key):
    with pytest.raises(ValueError, match="reads back"):
        GRAMMAR.set_directive("x = 1\n", key, "v")
    # No directive has it, so removing it changes nothing.
    assert GRAMMAR.remove_directive("# @per x\nx = 1\n", key) == "# @per x\nx = 1\n"


class Quarto(Grammar):
    """``#| key: value``, whose keys may hold a hyphen."""

    directive_re = re.compile(r"^#\|\s*([\w-]+):\s*(.*)$")

    def _in_block(self, stripped):
        return stripped.startswith("#|")

    def _format(self, key, value):
        return f"#| {key}: {value.strip()}"


def test_a_grammar_says_what_a_key_may_hold():
    quarto = Quarto()
    assert quarto.set_directive("x = 1\n", "fig-cap", "A plot") == "#| fig-cap: A plot\nx = 1\n"
    assert quarto.remove_directive("#| fig-cap: A\nx = 1\n", "fig-cap") == "x = 1\n"
    assert Annotation("fig-cap").key == "fig-cap"


def test_an_outer_line_break_in_a_value_is_stripped():
    assert GRAMMAR.set_directive("x\n", "a", "v\n") == "# @a v\nx\n"
    assert GRAMMAR.set_directive("x\n", "a", "\x0cv ") == "# @a v\nx\n"
    with pytest.raises(ValueError, match="one line"):
        GRAMMAR.set_directive("x\n", "a", "v\nw")


def test_a_non_str_key_or_value_is_a_type_error():
    with pytest.raises(TypeError):
        GRAMMAR.set_directive("x\n", 1, "v")  # ty: ignore[invalid-argument-type]
    with pytest.raises(TypeError):
        GRAMMAR.set_directive("x\n", "name", 1)  # ty: ignore[invalid-argument-type]


@pytest.mark.parametrize(
    ("source", "key", "value", "expected"),
    [
        ("# @a 1\nx = 1\ny = 2\r\n", "b", "2", "# @a 1\n# @b 2\nx = 1\ny = 2\r\n"),
        ("# @a 1\r\nx = 1\ny = 2\r", "a", "3", "# @a 3\r\nx = 1\ny = 2\r"),
        ("x = 1\r\ny = 2\n", "a", "1", "# @a 1\r\nx = 1\r\ny = 2\n"),
        ("# @a 1", "b", "2", "# @a 1\n# @b 2"),
        ("# @a 1\r\n# @a 2\nx\n", "a", "3", "# @a 3\r\nx\n"),
    ],
)
def test_an_edit_keeps_each_lines_ending(source, key, value, expected):
    assert GRAMMAR.set_directive(source, key, value) == expected


def test_an_edit_that_changes_nothing_returns_the_source():
    for source in ("x = 1\ny = 2\r\n", "# @a 1\r\nx\r\n"):
        assert GRAMMAR.remove_directive(source, "zzz") is source
    for source in ("# @a 1\ny = 2\r\n", "# @a 1\r\nx\r\n"):
        assert GRAMMAR.set_directive(source, "a", "1") is source
    assert GRAMMAR.remove_directive("# @a\n# @b", "b") == "# @a"


def test_toggling_a_cells_only_directive_round_trips():
    source = "# @nocache\n"
    removed = GRAMMAR.remove_directive(source, "nocache")
    assert removed == ""
    assert GRAMMAR.set_directive(removed, "nocache", "") == "# @nocache"
    assert GRAMMAR.remove_directive(GRAMMAR.set_directive(removed, "nocache", ""), "nocache") == ""


def test_a_byte_order_mark_hides_no_directive_and_stays_first():
    source = "﻿# @name n\nx = 1\n"
    assert GRAMMAR.directives(source) == [Directive("name", "n", 1)]
    assert GRAMMAR.strip_directives(source) == "x = 1"
    assert GRAMMAR.set_directive(source, "nocache", "") == "﻿# @name n\n# @nocache\nx = 1\n"
    assert GRAMMAR.set_directive("﻿x = 1\n", "a", "1") == "﻿# @a 1\nx = 1\n"
    assert GRAMMAR.remove_directive(source, "name") == "﻿x = 1\n"


class Bare(Quarto):
    def _format(self, key, value):
        return f"#| {key}: {value}"


class Unquoting(Grammar):
    def parse_directive(self, line):
        parsed = super().parse_directive(line)
        return parsed and (parsed[0], parsed[1].strip('"'))


def test_a_grammar_writes_the_stripped_value_and_may_read_it_its_way():
    assert Bare().set_directive("x = 1\n", "fig-cap", "\nv") == "#| fig-cap: v\nx = 1\n"
    assert Unquoting().set_directive("x\n", "cap", '"A plot"') == '# @cap "A plot"\nx\n'
    assert GRAMMAR.reads_key("a_1") and not GRAMMAR.reads_key("per-variant")
