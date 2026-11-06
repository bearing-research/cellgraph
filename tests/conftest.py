"""A small host vocabulary over core, so every effect has a directive in the tests."""

import pytest

import cellgraph as cg


def _source(value):
    return [cg.Binds(value), cg.Input("file", value, spec={"path": value})]


def _seed(value):
    cell_id, name = value.split()
    return [cg.Seeds(cell_id, name)]


def host_annotations():
    return [
        cg.Annotation("carry", effects=lambda v: [cg.Reads(v, even_if_defined=True)], in_key=False),
        cg.Annotation("seed", effects=_seed, in_key=False),
        cg.Annotation("ext", effects=lambda v: [cg.Reads(v.split())], in_key=False),
        cg.Annotation("source", effects=_source, in_key=False),
        cg.Annotation("out", effects=lambda v: [cg.Defines(v)]),
        cg.Annotation("keep", effects=lambda v: [cg.Keeps(v)], in_key=False),
        cg.Annotation("mode", parse=cg.scalar(lambda s: {"m": s})),
        cg.Annotation("opaque", parse=cg.scalar(lambda s: object())),
    ]


@pytest.fixture
def vocab():
    return cg.Vocabulary.core() | cg.Vocabulary(host_annotations())


@pytest.fixture
def run(vocab):
    def analyze(*sources, groups=None, **kwargs):
        cells = [cg.Cell(f"c{i}", src) for i, src in enumerate(sources)]
        return cg.analyze(cells, vocab, groups, **kwargs)

    return analyze


def codes(analysis, cell_id=None):
    return [d.code for d in analysis.diagnostics if cell_id is None or d.cell == cell_id]


def edges(analysis):
    return sorted((e.from_cell_id, e.to_cell_id, e.variable) for e in analysis.graph.edges)


def prov(hash_):
    """An output filed under *hash_*, as a key component."""
    return cg.canonical_json(["provenance", hash_])
