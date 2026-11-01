"""What analysis says about a cell: a code, a severity and where."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class Severity(StrEnum):
    """An ``ERROR`` keeps the cell, and every cell that depends on it, from planning."""

    ERROR = "error"
    WARN = "warn"
    INFO = "info"


@dataclass(frozen=True)
class Diagnostic:
    """One finding. ``annotation`` is the directive key it is about, if any.

    ``args`` holds what the message was built from, such as the cell id an ``@after``
    names, so a host can word the finding its own way.
    """

    code: str
    severity: Severity
    message: str
    cell: str | None = None
    annotation: str | None = None
    line: int | None = None
    args: Mapping[str, Any] = field(default_factory=dict, compare=False)
