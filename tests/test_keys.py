"""Keys against the formulas written out here, not against cellgraph's helpers."""

import hashlib
from collections.abc import Mapping

import pytest
from test_languages import Dashes, Words
from vestigo.provenance import derive_subkey as vestigo_subkey

import cellgraph as cg

ENV = "e" * 64
PINNED_PYTHON = "5cf60a1a11e9631e44ecda5728cb6584bffa0d33a3a318d18b7c63a9b2f5d883"
PINNED_WORDS = "ba1405d04d3e9d66b09f81db9ab7ac4226ff72a3dbdd54c0a1109a1bd16156a1"
PINNED_EXECUTION = "959300097c327545a4fb36aacffcd5dc1d072a8912c19ca606d943c39c2d338c"


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def published(**by_cell):
    return lambda cell_id: by_cell.get(cell_id, cg.Published())


def out(**refs):
    return cg.Published(
        outputs={
            k: cg.OutputRef(cg.Provenance(v)) if isinstance(v, str) else v for k, v in refs.items()
        }
    )


# --- definition keys ----------------------------------------------------------


def key_of(vocab, source, language="words"):
    return cg.analyze(
        [cg.Cell("a", source, language=language)],
        vocab,
        analyzers={"dashes": Dashes(), "words": Words(), "python": cg.PythonAnalyzer()},
    ).cells["a"]


def test_the_definition_key_is_the_language_the_body_and_the_fields(vocab):
    cell = key_of(vocab, "# @name n\n# @mode fast\ny  x  ")
    assert cell.normalized == "y  x"
    expected = ["definition", cg.PARTS_SCHEMA, "words", "y  x", {"mode": {"m": "fast"}}]
    assert cell.definition_key == sha(cg.canonical_json(expected))
    # A directive that is not in the key changes nothing. Other comments are the body's,
    # for its normalizer to judge.
    assert key_of(vocab, "# @name other\n# @mode fast\ny  x").definition_key == (
        cell.definition_key
    )
    assert key_of(vocab, "# note\n# @mode fast\ny  x").normalized == "# note\ny  x"
    assert key_of(vocab, "# @mode slow\ny  x").definition_key != cell.definition_key


def test_the_same_text_in_another_language_is_another_definition(vocab):
    words = key_of(vocab, "x = 1", "words")
    python = key_of(vocab, "x = 1", "python")
    assert words.normalized == python.normalized == "x = 1"
    assert words.definition_key != python.definition_key


def test_a_python_definition_key_is_the_ast_round_trip_of_its_body(vocab):
    cell = key_of(vocab, "# @mode fast\n# note\nx  =  1  # c\n", "python")
    assert cell.normalized == "x = 1"
    assert key_of(vocab, "x=1", "python").definition_key != cell.definition_key  # fields
    assert key_of(vocab, "# @mode fast\nx=1", "python").definition_key == cell.definition_key


def test_a_dash_languages_definition_key_strips_its_directives(vocab):
    cell = key_of(vocab, "-- @name n\n-- @mode fast\ny  x  ", "dashes")
    assert cell.normalized == "y  x"
    assert key_of(vocab, "-- @name other\n-- @mode fast\ny  x", "dashes").definition_key == (
        cell.definition_key
    )
    assert key_of(vocab, "-- @name n\n-- @mode fast\ny  z", "dashes").definition_key != (
        cell.definition_key
    )
    # Under ``--`` a ``#`` line is body, so it stays in the normalized text.
    assert key_of(vocab, "# @mode fast\ny  x", "dashes").normalized == "# @mode fast\ny  x"


def test_a_definition_key_is_pinned(vocab):
    source = "# @name n\n# @mode fast\n# note\nx  =  1  # c\n"
    assert key_of(vocab, source, "python").definition_key == PINNED_PYTHON
    assert key_of(vocab, source, "words").definition_key == PINNED_WORDS


def test_canonical_json_is_sorted_and_compact():
    assert cg.canonical_json({"b": [1, "é"], "a": None}) == '{"a":null,"b":[1,"é"]}'


@pytest.mark.parametrize("value", [{1: "a"}, {True: 1}, {None: 1}, [{"ok": {2: 0}}]])
def test_canonical_json_refuses_a_key_that_is_not_a_str(value):
    # ``json`` would write ``{1: "a"}`` as ``{"1": "a"}``, and the two would share a key.
    with pytest.raises(TypeError, match="must be a str"):
        cg.canonical_json(value)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), [-float("inf")]])
def test_canonical_json_refuses_nan_and_infinity(value):
    with pytest.raises(ValueError):
        cg.canonical_json(value)


# --- execution keys -----------------------------------------------------------


def test_an_execution_key_is_the_digest_of_its_parts(run):
    analysis = run("x = 1", "y = x")
    key = analysis.execution_key("c1", published=published(c0=out(x="h0")), env_hash=ENV)
    assert key.digest == cg.parts_digest(key.parts) == sha(cg.canonical_json(key.parts))
    assert key.parts == {
        "parts_schema": cg.PARTS_SCHEMA,
        "scope": "cell",
        "instance": None,
        "definition_key": analysis.cells["c1"].definition_key,
        "fields": {},
        "env_hash": ENV,
        "upstream": [["output:c0.x", "h0"]],
        "inputs": [],
    }
    assert cg.PARTS_SCHEMA == 2
    assert key.digest == PINNED_EXECUTION


def test_which_variable_a_component_is_counts(run):
    # Swapping two outputs' contents is a different input, so a different key.
    analysis = run("# @nocache\nx, y = 1, 2", "z = x - y")

    def key(dx, dy):
        refs = {
            "x": cg.OutputRef(cg.Provenance("P"), dx),
            "y": cg.OutputRef(cg.Provenance("P"), dy),
        }
        snapshot = cg.Published(outputs=refs)
        return analysis.execution_key("c1", published=published(c0=snapshot), env_hash=ENV)

    assert key("d1", "d2").digest != key("d2", "d1").digest


def test_run_identity_is_tagged_content_or_run(run):
    analysis = run("x = 1", "y = x")

    def component(ref):
        key = analysis.execution_key("c1", published=published(c0=out(x=ref)), env_hash=ENV)
        return key.parts["upstream"][0][1]

    assert component(cg.OutputRef(cg.Run("r1"))) == "run:r1"
    assert component(cg.OutputRef(cg.Run("r1"), content_digest="d")) == "content:d"


def test_a_nocache_upstream_is_keyed_by_its_bytes(run):
    analysis = run("# @nocache\nx = 1\nunread = 2", "y = x")
    ref = cg.OutputRef(cg.Provenance("h0"), content_digest="d0")
    snapshot = out(x=ref, unread=cg.OutputRef(cg.Provenance("h1"), content_digest="d1"))
    key = analysis.execution_key("c1", published=published(c0=snapshot), env_hash=ENV)
    # Only what the reader reads, and by content.
    assert key.parts["upstream"] == [["output:c0.x", vestigo_subkey("h0", "content=d0")]]


def test_a_nocache_upstream_without_a_digest_is_unavailable(run):
    # Its key follows its bytes, which its provenance does not name.
    snapshot = out(x=cg.OutputRef(cg.Provenance("h0")))
    analysis = run("# @nocache\nx = 1", "y = x")
    key = analysis.execution_key("c1", published=published(c0=snapshot), env_hash=ENV)
    assert isinstance(key, cg.Unavailable) and key.unresolved == ("output:c0.x",)


def test_an_unreadable_output_is_unavailable(run):
    snapshot = out(x=cg.Unavailable("gone"), z="hz")
    analysis = run("x = 1\nz = 1", "y = x + z")
    key = analysis.execution_key("c1", published=published(c0=snapshot), env_hash=ENV)
    assert isinstance(key, cg.Unavailable)
    assert key.unresolved == ("output:c0.x",)
    assert key.parts["upstream"] == [["output:c0.z", "hz"]]


def test_the_older_cell_level_ref(run):
    analysis = run("x = 1", "y = x")
    snapshot = cg.Published(cell_ref=cg.OutputRef(cg.Provenance("hc")))
    key = analysis.execution_key("c1", published=published(c0=snapshot), env_hash=ENV)
    assert key.parts["upstream"] == [["cell:c0", "hc"]]


def test_a_shadowed_name_adds_its_wired_producer(run):
    # c0 is upstream for z and also defines x, which c2 reads from c1.
    analysis = run("x = 1\nz = 1", "x = 2", "y = x + z")
    snapshot = published(c0=out(x="h0", z="hz"), c1=out(x="h1"))
    key = analysis.execution_key("c2", published=snapshot, env_hash=ENV)
    assert ["wired:c1.x", "wired:x=h1"] in key.parts["upstream"]
    assert not any(address.startswith("wired:c0") for address, _ in key.parts["upstream"])


def test_an_input_is_its_kind_name_identity_and_fingerprint(run):
    analysis = run("# @source data\ny = data")
    with pytest.raises(ValueError, match="no resolver"):
        analysis.execution_key("c0", published=published(), env_hash=ENV)

    def key(fingerprint):
        return analysis.execution_key(
            "c0",
            published=published(),
            env_hash=ENV,
            resolve={"file": lambda spec: cg.Resolved({"uri": "u"}, fingerprint)},
        )

    component = cg.canonical_json(["file", "data", {"uri": "u"}, "fp"])
    assert key("fp").parts["inputs"] == [["file", "data", component]]
    assert key("fp").digest == cg.parts_digest(key("fp").parts)
    assert key("other").digest != key("fp").digest
    with pytest.raises(TypeError, match="must be a str"):
        key({1: "a"})


def test_an_unresolved_input_keeps_the_parts_it_built(run):
    analysis = run("x = 1", "# @source data\ny = data + x")
    key = analysis.execution_key(
        "c1",
        published=published(c0=out(x="h0")),
        env_hash=ENV,
        resolve={"file": lambda spec: cg.Unavailable("missing")},
    )
    assert isinstance(key, cg.Unavailable)
    assert key.unresolved == ("input:file:data",)
    assert key.parts["upstream"] == [["output:c0.x", "h0"]]


def test_a_host_keyed_language_gets_no_digest(vocab):
    class HostOwned(Words):
        owns_key = False

    analysis = cg.analyze(
        [cg.Cell("p", "y = 1"), cg.Cell("a", "z y", language="h")],
        vocab,
        analyzers={"python": cg.get_language_analyzer("python"), "h": HostOwned()},
    )
    key = analysis.execution_key("a", published=published(p=out(y="hp")), env_hash=ENV)
    assert isinstance(key, cg.HostKeyed)
    # What it reads upstream, for the host's own key.
    assert key.parts["upstream"] == [["output:p.y", "hp"]]
    assert key.parts["inputs"] == [] and key.unresolved == ()
    # An upstream it cannot read is named, so the host does not key on what is there.
    snapshot = out(y=cg.Unavailable("gone"))
    missing = analysis.execution_key("a", published=published(p=snapshot), env_hash=ENV)
    assert isinstance(missing, cg.HostKeyed) and missing.unresolved == ("output:p.y",)


def test_a_blocked_cell_and_its_dependents_are_keyed(run):
    """A result cached under a blocked cell's key is still its result."""
    analysis = run("# @nocache maybe\nx = 1", "y = x")
    assert not analysis.cells["c0"].plannable and not analysis.cells["c1"].plannable
    blocked = analysis.execution_key("c0", published=published(), env_hash=ENV)
    assert blocked.digest == cg.parts_digest(blocked.parts)
    below = analysis.execution_key("c1", published=published(c0=out(x="h0")), env_hash=ENV)
    assert below.parts["upstream"] == [["output:c0.x", "h0"]]


def test_a_field_the_key_cannot_encode_leaves_no_key(vocab):
    # Its value is missing from the definition key, so cells that differ in it would share one.
    odd = cg.Vocabulary([cg.Annotation("m", parse=cg.scalar(lambda s: {1: s}))])
    analysis = cg.analyze([cg.Cell("c0", "# @m fast\nx = 1")], cg.Vocabulary.core() | odd)
    key = analysis.execution_key("c0", published=published(), env_hash=ENV)
    assert isinstance(key, cg.Unavailable) and key.unresolved == ("field:m",)


def test_the_shadowed_reads(run):
    analysis = run("x = 1\nz = 1", "x = 2", "y = x + z")
    assert analysis.shadowed_reads("c2") == {"x"}
    assert analysis.shadowed_reads("c1") == set()


class _Lookups(Mapping):
    """Outputs that note which ones were looked up."""

    def __init__(self, **refs):
        self.refs = refs
        self.looked_up: list[str] = []

    def __getitem__(self, key):
        self.looked_up.append(key)
        return self.refs[key]

    def __iter__(self):
        return iter(self.refs)

    def __len__(self):
        return len(self.refs)


def test_an_output_is_looked_up_only_when_it_counts(run):
    analysis = run("# @nocache\nx = 1\nunread = 2", "y = x")
    outputs = _Lookups(
        x=cg.OutputRef(cg.Provenance("h0"), content_digest="d0"),
        unread=cg.OutputRef(cg.Provenance("h1"), content_digest="d1"),
    )
    analysis.execution_key(
        "c1", published=published(c0=cg.Published(outputs=outputs)), env_hash=ENV
    )
    assert outputs.looked_up == ["x"]
