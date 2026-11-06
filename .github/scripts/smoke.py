"""Run against an installed distribution, never the checkout: release.yml's smoke test."""

from pathlib import Path

import cellgraph as cg

assert "site-packages" in Path(cg.__file__).parts, f"cellgraph is not installed: {cg.__file__}"
cells = [cg.Cell("a", "x = 1\n"), cg.Cell("b", "y = x + 1\n")]
analysis = cg.analyze(cells, cg.Vocabulary.core())
assert analysis.dag.topological_order == ["a", "b"], analysis.dag.topological_order
print(f"cellgraph analyzes ok, from {cg.__file__}")
