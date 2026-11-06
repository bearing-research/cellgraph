"""What to run before a cell: readiness, not keys.

A downstream key can depend on an upstream's bytes and on inputs resolved at run
time, so it does not exist before the upstream runs. The plan therefore asks the
host which cells are ready, as Strata's cascade planner does.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cellgraph.analysis import Analysis


def plan(analysis: Analysis, target: str, ready: Callable[[str], bool]) -> list[str]:
    """The upstream-reachable cells that are not ready, in topological order, then *target*.

    Ordering edges count as dependencies; a fan-out cell is one step. A target that
    is not plannable raises ``NotPlannableError`` with the diagnostics that block it,
    and an inactive variant ``InactiveVariantError``.
    """
    from cellgraph.analysis import InactiveVariantError, NotPlannableError

    if target in analysis.graph.inactive_cells:
        raise InactiveVariantError(target)
    if not analysis.cells[target].plannable:
        raise NotPlannableError(target, analysis.blocking(target))
    dag = analysis.dag
    reachable = dag.upstream_reachable(target)
    steps = [
        cell_id
        for cell_id in dag.topological_order
        if cell_id in reachable and cell_id != target and not ready(cell_id)
    ]
    return [*steps, target]
