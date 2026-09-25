"""Operation registry.

Maps each :class:`OperationKind` to its handler. Because
:class:`WorkbookOperation` is a closed discriminated union, adding a variant
makes every exhaustive match fail to type-check until handled here — and
``tests/test_executor_registry.py`` additionally requires a **policy rule** and a
**dry-run preview** for each kind, so a new operation cannot ship without an
authorisation path (ADR-0006).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from app.contracts.enums import OperationKind
from app.executor.operations import (
    OperationContext,
    handle_apply_validation,
    handle_compare_workbooks,
    handle_create_summary,
    handle_create_worksheet,
    handle_filter_rows,
    handle_normalize_values,
    handle_read_range,
    handle_reconcile,
    handle_remove_duplicates,
    handle_rename_worksheet,
    handle_set_formula,
    handle_sort_range,
    handle_write_range,
)

Handler = Callable[[Any, Any, OperationContext], Any]

#: Every operation kind must appear here. Asserted complete by test.
REGISTRY: dict[OperationKind, Handler] = {
    OperationKind.READ_RANGE: handle_read_range,
    OperationKind.WRITE_RANGE: handle_write_range,
    OperationKind.SET_FORMULA: handle_set_formula,
    OperationKind.CREATE_WORKSHEET: handle_create_worksheet,
    OperationKind.RENAME_WORKSHEET: handle_rename_worksheet,
    OperationKind.SORT_RANGE: handle_sort_range,
    OperationKind.FILTER_ROWS: handle_filter_rows,
    OperationKind.REMOVE_DUPLICATES: handle_remove_duplicates,
    OperationKind.NORMALIZE_VALUES: handle_normalize_values,
    OperationKind.APPLY_VALIDATION: handle_apply_validation,
    OperationKind.CREATE_SUMMARY: handle_create_summary,
    OperationKind.COMPARE_WORKBOOKS: handle_compare_workbooks,
    OperationKind.RECONCILE: handle_reconcile,
}


def handler_for(kind: OperationKind) -> Handler:
    """Look up a handler, or raise a clear error naming the gap.

    A missing handler is a build-time bug, not a runtime condition to be handled
    gracefully — the error says so explicitly.
    """
    try:
        return REGISTRY[kind]
    except KeyError as error:
        from app.contracts.errors import UnsupportedOperationError

        raise UnsupportedOperationError(
            f"no handler registered for operation {kind.value!r}; "
            f"this is an ExcelPilot build error",
            capability=kind.value,
        ) from error


def registered_kinds() -> set[OperationKind]:
    return set(REGISTRY)


__all__ = ["REGISTRY", "Handler", "handler_for", "registered_kinds"]
