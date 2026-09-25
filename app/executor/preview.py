"""Dry-run preview: measured, never estimated.

Every number in a :class:`DryRunPreview` is **calculated** by inspecting the real
workbook and, for mutating operations, by simulating the operation against an
in-memory copy. Nothing is fabricated and nothing is a guess.

The preview works on a throwaway copy of the workbook, so it can report exactly
how many cells *would* change, exactly which formulas *would* be removed, and
exactly how many records *would* require review — without touching the source
and without writing any output.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from openpyxl.workbook import Workbook

from app.contracts.config import LimitsConfig
from app.contracts.enums import RiskLevel
from app.contracts.operations import (
    CreateWorksheet,
    Reconcile,
    ReconcileSpec,
    RenameWorksheet,
    Target,
)
from app.contracts.pipeline import DryRunPreview
from app.contracts.workbook import WorkbookInspection
from app.executor.operations import OperationContext
from app.executor.registry import handler_for
from app.workbook import load_workbook
from app.workbook.hashing import is_formula


def preview_operation(
    operation: Any, inspection: WorkbookInspection, limits: LimitsConfig | None = None
) -> DryRunPreview:
    """Preview a single operation in isolation.

    Loads the workbook read-only, simulates the operation in memory, and reports
    the measured effect. The source file is never written.
    """
    workbook = load_workbook(Path(inspection.path), limits=limits)
    try:
        return _simulate(workbook, [operation], inspection)
    finally:
        workbook.close()


def preview_plan(
    operations: list[Any],
    inspection: WorkbookInspection,
    *,
    run_id: str,
    output_path: str | Path,
    risk: RiskLevel = RiskLevel.LOW,
    risk_reasons: list[str] | None = None,
    approval_required: bool = False,
    policy_explanation: str = "",
    policy_rule_ids: list[str] | None = None,
    jev_summary: str = "JEV not consulted",
    limits: LimitsConfig | None = None,
) -> DryRunPreview:
    """Preview a whole plan, simulating every operation in order.

    Operations are applied to one in-memory workbook so that later operations see
    the effect of earlier ones — which is what makes a plan's interaction
    measurable rather than assumed.
    """
    workbook = load_workbook(Path(inspection.path), limits=limits)
    try:
        preview = _simulate(workbook, operations, inspection)
    finally:
        workbook.close()

    extra_warnings: list[str] = []
    if policy_explanation:
        extra_warnings.append(policy_explanation)
    if policy_rule_ids:
        extra_warnings.append(f"policy rules fired: {', '.join(policy_rule_ids)}")
    if jev_summary and jev_summary != "JEV not consulted":
        extra_warnings.append(jev_summary)

    return preview.model_copy(
        update={
            "run_id": run_id,
            "proposed_output_path": str(output_path),
            "risk": risk,
            "risk_reasons": risk_reasons or [],
            "approval_required": approval_required,
            "warnings": _merge(preview.warnings, extra_warnings),
        }
    )


def _simulate(
    workbook: Workbook, operations: list[Any], inspection: WorkbookInspection
) -> DryRunPreview:
    """Apply operations to an in-memory workbook and measure the effect.

    ``before`` is captured first so "cells that would change" means *actually
    different*, not "cells the operation touches".
    """
    before = _snapshot_cells(workbook)
    context = OperationContext(neutralize_formula_injection=True)

    breakdown: list[dict[str, Any]] = []
    sheets_affected: set[str] = set()
    formulas_to_add = 0
    records_to_remove = 0
    records_to_normalize = 0
    records_requiring_review = 0
    structural = False
    warnings: list[str] = []
    _results: list[Any] = []

    for operation in operations:
        handler = handler_for(operation.operation)
        try:
            result = handler(workbook, operation, context)
        except Exception as error:  # noqa: BLE001 - a preview must not crash the run
            breakdown.append(
                {
                    "operation": operation.operation.value,
                    "status": "would_fail",
                    "error": f"{type(error).__name__}: {error}",
                }
            )
            warnings.append(
                f"{operation.operation.value} would fail: {type(error).__name__}: {error}"
            )
            continue

        _results.append(result)
        target_sheet = _sheet_of(operation)
        if target_sheet:
            sheets_affected.add(target_sheet)
        if result.sheets_created:
            sheets_affected.update(result.sheets_created)
            structural = True

        formulas_to_add += result.formulas_added
        records_to_remove += result.rows_removed
        if operation.operation.value == "normalize_values":
            records_to_normalize += result.details.get("cells_changed", result.cells_written)
        if operation.operation.value == "apply_validation":
            records_requiring_review += result.details.get("violations", 0)

        breakdown.append(
            {
                "operation": operation.operation.value,
                "status": "would_apply",
                "cells_written": result.cells_written,
                "cells_read": result.cells_read,
                "formulas_added": result.formulas_added,
                "rows_removed": result.rows_removed,
                "rows_affected": result.rows_affected,
                "cells_neutralised": result.cells_neutralised,
                "sheets_created": result.sheets_created,
            }
        )
        warnings.extend(result.warnings)

    after = _snapshot_cells(workbook)
    changed, removed, added, formulas_to_remove = _compare(before, after)
    # `changed` only counts cells present in both snapshots. A sheet the run
    # *creates* is all new cells, so it is added separately — otherwise a plan
    # that writes a summary sheet reports a far smaller change than it will
    # actually make, and the planned-vs-actual check then fires on a correct run.
    created = sum(
        result.cells_written for result in (entry for entry in _results) if result.sheets_created
    )
    changed += created

    return DryRunPreview(
        run_id="preview",
        workbook_name=inspection.file_name,
        source_path=inspection.path,
        proposed_output_path="",
        cells_to_change=changed,
        sheets_affected=len(sheets_affected),
        sheets_affected_names=sorted(sheets_affected),
        formulas_to_add=formulas_to_add,
        formulas_to_remove=formulas_to_remove,
        records_to_normalize=records_to_normalize,
        records_to_remove=records_to_remove,
        records_requiring_review=records_requiring_review,
        structural_change=structural,
        risk=RiskLevel.LOW,
        operation_breakdown=breakdown,
        warnings=warnings,
    )


def _snapshot_cells(workbook: Workbook) -> dict[tuple[str, str], tuple[Any, bool]]:
    """Every non-empty cell as ``(sheet, coordinate) -> (value, is_formula)``.

    Taken on the in-memory workbook only; nothing is read from disk.
    """
    snapshot: dict[tuple[str, str], tuple[Any, bool]] = {}
    for worksheet in workbook.worksheets:
        for row in worksheet.iter_rows():
            for cell in row:
                if cell.value is None:
                    continue
                snapshot[(worksheet.title, cell.coordinate)] = (
                    cell.value,
                    is_formula(cell.value),
                )
    return snapshot


def _compare(
    before: dict[tuple[str, str], tuple[Any, bool]],
    after: dict[tuple[str, str], tuple[Any, bool]],
) -> tuple[int, int, int, int]:
    """Return ``(changed, removed, added, formulas_removed)`` — all measured."""
    changed = 0
    formulas_removed = 0
    for key, (value, was_formula) in before.items():
        if key not in after:
            if was_formula:
                formulas_removed += 1
            continue
        new_value, is_formula_now = after[key]
        if was_formula and not is_formula_now:
            # A formula replaced by a literal is a removal *and* a change.
            formulas_removed += 1
        if value != new_value:
            changed += 1
    added = len(set(after) - set(before))
    removed = len(set(before) - set(after))
    return changed, removed, added, formulas_removed


def _sheet_of(operation: Any) -> str | None:
    if isinstance(operation, CreateWorksheet):
        return operation.name
    if isinstance(operation, RenameWorksheet):
        return operation.to_name
    target = getattr(operation, "target", None)
    return target.sheet if isinstance(target, Target) else None


def _merge(*groups: list[str]) -> list[str]:
    seen: set[str] = set()
    merged: list[str] = []
    for group in groups:
        for item in group:
            if item and item not in seen:
                seen.add(item)
                merged.append(item)
    return merged


def verification_plan_for(operations: list[Any]) -> list[str]:
    """Which verification checks this plan warrants, derived from the operations.

    Derived rather than hard-coded so a plan that reconciles gets a reconciliation
    check and one that does not, does not claim one.
    """
    kinds = {operation.operation.value for operation in operations}
    plan = ["structural_check", "data_check"]
    if kinds & {"reconcile", "create_summary"}:
        plan.append("reconciliation")
    if kinds & {"set_formula", "write_range", "remove_duplicates", "sort_range", "create_summary"}:
        plan.append("formula_validation")
    if kinds & {"remove_duplicates", "apply_validation", "filter_rows"}:
        plan.append("value_check")
    return plan


def reconciliation_specs_from(operations: list[Any]) -> list[ReconcileSpec]:
    """Extract reconciliation checks declared in the plan."""
    specs: list[ReconcileSpec] = []
    for operation in operations:
        if isinstance(operation, Reconcile):
            specs.extend(operation.checks)
    return specs


__all__ = [
    "preview_operation",
    "preview_plan",
    "reconciliation_specs_from",
    "verification_plan_for",
]
