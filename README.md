# cellgraph

The cell graph of a notebook or script. A cell is source with a leading
`# @key value` block (`-- @key value` in a language whose comment marker is
`--`); the set of annotations is the host's, composed from the
ten effects the package implements (binds, defines, reads, keeps, after, seeds,
member, fan-out, input, no-cache). `analyze` reads every cell's names and
annotations, applies their effects, wires the DAG, says which cells can be
planned and why the others cannot, and keys each cell: a definition key from its
normalized source and the annotations that change what it computes, and an
execution key from the outputs and external inputs the host looks up. The
package has no dependencies and runs nothing; the host executes, stores and
resolves. It was factored out of Strata's notebook, its first host.

```bash
pip install cellgraph
```

See [the API](https://github.com/bearing-research/cellgraph/blob/main/docs/api.md).

```python
import cellgraph as cg

vocab = cg.Vocabulary.core() | cg.Vocabulary(
    [
        cg.Annotation("gpu", parse=cg.scalar(str), in_key=False),
    ]
)
analysis = cg.analyze(
    [cg.Cell("a", "x = 1\n"), cg.Cell("b", "# @gpu A100\ny = x + 1\n")],
    vocab,
)
analysis.cells["b"].annotations["gpu"]  # 'A100'
analysis.dag.topological_order  # ['a', 'b']
```

## Development

```bash
uv sync
uv run pytest
uv run pre-commit run --all-files
```
