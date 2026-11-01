"""What analysis says about a cell: a code, a severity and where."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Severity(StrEnum):
    """An ``ERROR`` keeps the cell, and every cell that depends on it, from planning."""

    ERROR = "error"
    WARN = "warn"
    INFO = "info"


@dataclass(frozen=True)
class Diagnostic:
    """One finding. ``annotation`` is the directive key it is about, if any."""

    code: str
    severity: Severity
    message: str
    cell: str | None = None
    annotation: str | None = None
    line: int | None = None
