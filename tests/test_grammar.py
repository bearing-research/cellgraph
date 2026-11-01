from cellgraph.grammar import GRAMMAR, Directive


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
