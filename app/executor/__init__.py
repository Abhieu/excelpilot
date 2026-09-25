"""Deterministic executor: the only component that mutates a workbook.

Depends on ``app.contracts``, ``app.workbook``, ``app.policy``, and ``app.safety``.
Must not import the planner, the JEV client, or the CLI — enforced statically by
``tests/test_architecture.py`` (ADR-0006).
"""

from app.executor.engine import ExecutionError, Executor
from app.executor.operations import OperationContext
from app.executor.preview import (
    preview_operation,
    preview_plan,
    reconciliation_specs_from,
    verification_plan_for,
)
from app.executor.registry import REGISTRY, handler_for, registered_kinds

__all__ = [
    "REGISTRY",
    "ExecutionError",
    "Executor",
    "OperationContext",
    "handler_for",
    "preview_operation",
    "preview_plan",
    "reconciliation_specs_from",
    "registered_kinds",
    "verification_plan_for",
]
