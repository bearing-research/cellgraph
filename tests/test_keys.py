"""Keys against the formulas written out here, not against cellgraph's helpers."""

import ast
import hashlib
import json
from collections.abc import Mapping

import pytest
from test_languages import Words
from vestigo.provenance import derive_subkey as vestigo_subkey

import cellgraph as cg

ENV = "e" * 64


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def strata_source_hash(source, fields=None):
    """Strata's compute_source_hash: the AST round trip, then the key fields."""
    try:
        text = ast.unparse(ast.parse(source))
    except SyntaxError:
        text = "\n".join(line.rstrip() for line in source.splitlines()).strip()
    if fields:
        text += "\0" + json.dumps(fields, sort_keys=True, separators=(",", ":"))
    return sha(text)


def strata_provenance(inputs, source_hash, env_hash):
    hasher = hashlib.sha256()
    for component in sorted(inputs):
        hasher.update(component.encode() + b"\0")
    hasher.update(source_hash.encode() + b"\0")
    hasher.update(env_hash.encode())
    return hasher.hexdigest()


def published(**by_cell):
    return lambda cell_id: by_cell.get(cell_id, cg.Published())


def out(**refs):
    return cg.Published(
        outputs={
            k: cg.OutputRef(cg.Provenance(v)) if isinstance(v, str) else v for k, v in refs.items()
        }
    )


# --- definition keys ----------------------------------------------------------


def test_the_legacy_definition_key_is_strata_source_hash(run):
    source = "# @mode fast\nx  =  1  # comment"
    analysis = run(source)
    assert analysis.cells["c0"].definition_key == strata_source_hash(
        source, {"mode": {"m": "fast"}}
    )
    plain = run("y = 2")
    assert plain.cells["c0"].definition_key == strata_source_hash("y = 2")


def test_the_legacy_normalization_is_the_whole_source_in_any_language(vocab):
    source = "# @name n\ny  x"
    analysis = cg.analyze(
        [cg.Cell("a", source, language="words")], vocab, analyzers={"words": Words()}
    )
    # ``y x`` does not parse, so the line strip keeps the directive in the text.
    assert analysis.cells["a"].definition_key == strata_source_hash(source)


def test_cellgraph_1_strips_directives_and_uses_the_language_normalizer(vocab):
    def key(source):
        return cg.analyze(
            [cg.Cell("a", source, language="words")],
            vocab,
            analyzers={"words": Words()},
            normalization=cg.NORMALIZE_CELLGRAPH_1,
            digest=cg.DIGEST_CELLGRAPH_1,
        ).cells["a"]

    cell = key("# @name n\n# @mode fast\ny  x  ")
    assert cell.normalized == "y  x"
    expected = ["cellgraph-1", "definition", "y  x", {"mode": {"m": "fast"}}]
    assert cell.definition_key == sha(cg.canonical_json(expected))
    assert key("# @name other\n# @mode fast\ny  x").definition_key == cell.definition_key


def test_canonical_json_is_sorted_and_compact():
    assert cg.canonical_json({"b": [1, "é"], "a": None}) == '{"a":null,"b":[1,"é"]}'


# --- execution keys -----------------------------------------------------------


def test_a_legacy_execution_key_is_strata_provenance_hash(run):
    analysis = run("x = 1", "y = x")
    key = analysis.execution_key("c1", published=published(c0=out(x="h0")), env_hash=ENV)
    defn = analysis.cells["c1"].definition_key
    assert key.digest == strata_provenance(["h0"], defn, ENV)
    assert key.parts["parts_schema"] == cg.PARTS_SCHEMA == 1
    assert key.parts["upstream"] == [["output:c0.x", "h0"]]
    assert key.parts["scope"] == "cell" and key.parts["instance"] is None


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


def test_a_nocache_upstream_without_a_digest(run, vocab):
    snapshot = out(x=cg.OutputRef(cg.Provenance("h0")))
    legacy = run("# @nocache\nx = 1", "y = x")
    key = legacy.execution_key("c1", published=published(c0=snapshot), env_hash=ENV)
    assert key.parts["upstream"] == [["output:c0.x", "h0"]]
    cg1 = run("# @nocache\nx = 1", "y = x", digest=cg.DIGEST_CELLGRAPH_1)
    key = cg1.execution_key("c1", published=published(c0=snapshot), env_hash=ENV)
    assert isinstance(key, cg.Unavailable) and key.unresolved == ("output:c0.x",)


def test_an_unreadable_output_is_skipped_by_legacy_and_unresolved_by_cellgraph_1(run):
    snapshot = out(x=cg.Unavailable("gone"), z="hz")
    legacy = run("x = 1\nz = 1", "y = x + z")
    key = legacy.execution_key("c1", published=published(c0=snapshot), env_hash=ENV)
    assert key.digest == strata_provenance(["hz"], legacy.cells["c1"].definition_key, ENV)
    cg1 = run("x = 1\nz = 1", "y = x + z", digest=cg.DIGEST_CELLGRAPH_1)
    key = cg1.execution_key("c1", published=published(c0=snapshot), env_hash=ENV)
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


def test_inputs_need_a_resolver_and_a_legacy_string(run):
    analysis = run("# @source data\ny = data")
    with pytest.raises(ValueError, match="no resolver"):
        analysis.execution_key("c0", published=published(), env_hash=ENV)
    with pytest.raises(ValueError, match="legacy"):
        analysis.execution_key(
            "c0",
            published=published(),
            env_hash=ENV,
            resolve={"file": lambda spec: cg.Resolved("data", "fp")},
        )
    key = analysis.execution_key(
        "c0",
        published=published(),
        env_hash=ENV,
        resolve={
            "file": lambda spec: cg.Resolved("data", "fp", legacy=f"file:{spec.spec['path']}")
        },
    )
    assert key.digest == strata_provenance(["file:data"], analysis.cells["c0"].definition_key, ENV)


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


def test_the_cellgraph_1_execution_digest(run):
    analysis = run("# @source data\ny = data", digest=cg.DIGEST_CELLGRAPH_1)
    key = analysis.execution_key(
        "c0",
        published=published(),
        env_hash=ENV,
        resolve={"file": lambda spec: cg.Resolved({"uri": "u"}, "fp")},
    )
    component = cg.canonical_json(["file", "data", {"uri": "u"}, "fp"])
    defn = analysis.cells["c0"].definition_key
    assert key.digest == sha(
        cg.canonical_json(["cellgraph-1", "execution", defn, ENV, [component], "cell", None])
    )
    assert key.parts["digest"] == "cellgraph-1"


def test_a_host_keyed_language_gets_no_digest(vocab):
    class HostOwned(Words):
        owns_key = False

    analysis = cg.analyze([cg.Cell("a", "y", language="h")], vocab, analyzers={"h": HostOwned()})
    assert isinstance(
        analysis.execution_key("a", published=published(), env_hash=ENV), cg.HostKeyed
    )


def test_a_blocked_cell_and_its_dependents_are_keyed(run):
    """A result cached under a blocked cell's key is still its result."""
    analysis = run("# @nocache maybe\nx = 1", "y = x")
    assert not analysis.cells["c0"].plannable and not analysis.cells["c1"].plannable
    blocked = analysis.execution_key("c0", published=published(), env_hash=ENV)
    assert blocked.digest == strata_provenance([], analysis.cells["c0"].definition_key, ENV)
    below = analysis.execution_key("c1", published=published(c0=out(x="h0")), env_hash=ENV)
    assert below.digest == strata_provenance(["h0"], analysis.cells["c1"].definition_key, ENV)


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
