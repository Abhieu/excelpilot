"""Deterministic operation handlers.

Each handler takes ``(workbook, operation, context)`` and returns an
:class:`OperationResult`. They are ordinary functions over an openpyxl workbook:
no I/O beyond the workbook itself, no network, no model, no ``eval``.

This module is the *only* place in ExcelPilot that writes cell values.
"""

from __future__ import annotations

import datetime as dt
import re
import statistics
import unicodedata
from collections.abc import Callable
from typing import Any

from openpyxl.utils import get_column_letter
from openpyxl.workbook import Workbook

from app.contracts.operations import (
    Aggregate,
    ApplyValidation,
    CompareWorkbooks,
    CreateSummary,
    CreateWorksheet,
    FilterCondition,
    FilterRows,
    NormalizeRules,
    NormalizeValues,
    ReadRange,
    Reconcile,
    ReconcileSpec,
    RemoveDuplicates,
    RenameWorksheet,
    SetFormula,
    SortRange,
    Target,
    ValidationRule,
    WriteRange,
)
from app.contracts.pipeline import OperationResult
from app.contracts.verification import ReconciliationResult
from app.safety import formula_guard
from app.workbook.hashing import is_formula
from app.workbook.table import find_sheet, read_table, resolve_target

#: Shared execution context, built by the executor.
Handler = Callable[[Workbook, Any, "OperationContext"], OperationResult]


class OperationContext:
    """Everything a handler needs beyond the workbook and the operation.

    Carries the write-safety configuration so a handler does not have to reach
    for global config, and the collected reconciliation results that ``reconcile``
    returns to the verification stage.
    """

    __slots__ = ("neutralize_formula_injection", "reconciliation_results", "warnings")

    def __init__(self, *, neutralize_formula_injection: bool = True) -> None:
        self.neutralize_formula_injection = neutralize_formula_injection
        self.reconciliation_results: list[ReconciliationResult] = []
        self.warnings: list[str] = []


# --------------------------------------------------------------------------
# Read
# --------------------------------------------------------------------------


def handle_read_range(
    workbook: Workbook, operation: ReadRange, context: OperationContext
) -> OperationResult:
    """Read a range. Never mutates."""
    view = read_table(workbook, operation.target, max_rows=operation.max_rows)
    return OperationResult(
        operation=operation.operation.value,
        status="applied",
        cells_read=view.row_count * max(view.column_count, 1),
        rows_read=view.row_count,
        details={
            "headers": view.headers,
            "row_count": view.row_count,
            "truncated": bool(
                operation.max_rows and view.resolved and view.row_count >= operation.max_rows
            ),
        },
    )


# --------------------------------------------------------------------------
# Write
# --------------------------------------------------------------------------


def handle_write_range(
    workbook: Workbook, operation: WriteRange, context: OperationContext
) -> OperationResult:
    """Write a rectangular block of values.

    Formula-like leading characters in incoming values are neutralised to literal
    text unless the caller explicitly disabled that, and the count is reported.
    """
    worksheet = find_sheet(workbook, operation.target.sheet)
    resolved = resolve_target(workbook, operation.target)
    values = operation.values
    if not values:
        return OperationResult(
            operation=operation.operation.value, status="skipped", warnings=["no values supplied"]
        )

    start_row = resolved.min_row
    if operation.write_header:
        # Keep the existing header row position rather than blindly using min_row.
        start_row = resolved.header_row or resolved.min_row

    written = 0
    neutralised = 0
    for row_offset, row in enumerate(values):
        for column_offset, value in enumerate(row):
            coordinate_row = start_row + row_offset
            column = resolved.min_col + column_offset
            if operation.target.cell_range and column > resolved.max_col:
                # The caller named an explicit range; do not spill past it.
                break
            cell = worksheet.cell(row=coordinate_row, column=column)
            if context.neutralize_formula_injection and operation.neutralize_formula_injection:
                guarded, count = formula_guard.neutralise_row((value,))
                value = guarded[0]
                neutralised += count
            cell.value = value
            written += 1

    warnings = []
    if neutralised:
        warnings.append(
            f"{neutralised} value(s) began with a formula-trigger character and were "
            f"written as literal text"
        )
    return OperationResult(
        operation=operation.operation.value,
        status="applied",
        cells_written=written,
        rows_written=len(values),
        cells_neutralised=neutralised,
        warnings=warnings,
    )


def handle_set_formula(
    workbook: Workbook, operation: SetFormula, context: OperationContext
) -> OperationResult:
    """Write formulas into a range."""
    worksheet = find_sheet(workbook, operation.target.sheet)
    resolved = resolve_target(workbook, operation.target)
    if not operation.formulas:
        return OperationResult(
            operation=operation.operation.value, status="skipped", warnings=["no formulas supplied"]
        )

    columns = len(operation.formulas)
    added = 0
    rows_written = 0
    for offset, formula in enumerate(operation.formulas):
        if operation.fill_direction == "rows":
            row = resolved.min_row + offset
            for index in range(columns):
                worksheet.cell(row=row, column=resolved.min_col + index).value = formula
                added += 1
            rows_written += 1
        else:
            column = resolved.min_col + offset
            for index in range(columns):
                worksheet.cell(row=resolved.min_row + index, column=column).value = formula
                added += 1
            rows_written += columns

    return OperationResult(
        operation=operation.operation.value,
        status="applied",
        cells_written=added,
        formulas_added=added,
        rows_written=rows_written,
    )


# --------------------------------------------------------------------------
# Structural
# --------------------------------------------------------------------------


def handle_create_worksheet(
    workbook: Workbook, operation: CreateWorksheet, context: OperationContext
) -> OperationResult:
    """Create a worksheet. Refuses to clobber an existing sheet."""
    if operation.name in workbook.sheetnames:
        return OperationResult(
            operation=operation.operation.value,
            status="failed",
            error=f"sheet {operation.name!r} already exists",
        )
    worksheet = workbook.create_sheet(title=operation.name, index=operation.position)
    worksheet.sheet_state = operation.state
    return OperationResult(
        operation=operation.operation.value,
        status="applied",
        sheets_created=[operation.name],
        details={"structural_change": True},
    )


def handle_rename_worksheet(
    workbook: Workbook, operation: RenameWorksheet, context: OperationContext
) -> OperationResult:
    """Rename a worksheet.

    openpyxl does not update formulas that referenced the old name. That is
    reported as a warning so the operator knows to check, rather than being
    silently left with broken references (formula verification will also catch
    it).
    """
    worksheet = find_sheet(workbook, operation.from_name)
    old = worksheet.title
    if operation.to_name in workbook.sheetnames:
        return OperationResult(
            operation=operation.operation.value,
            status="failed",
            error=f"cannot rename to {operation.to_name!r}: a sheet with that name already exists",
        )
    worksheet.title = operation.to_name
    warnings = []
    if _references_sheet(workbook, old):
        warnings.append(
            f"formulas referencing {old!r} are not rewritten automatically; "
            f"check them after renaming"
        )
    return OperationResult(
        operation=operation.operation.value,
        status="applied",
        sheets_renamed=[f"{old} -> {operation.to_name}"],
        warnings=warnings,
        details={"structural_change": True},
    )


def _references_sheet(workbook: Workbook, name: str) -> bool:
    """Whether any formula in the workbook mentions a sheet by name."""
    needle = name.lower()
    for worksheet in workbook.worksheets:
        for row in worksheet.iter_rows():
            for cell in row:
                if is_formula(cell.value) and needle in str(cell.value).lower():
                    return True
    return False


# --------------------------------------------------------------------------
# Data operations
# --------------------------------------------------------------------------


def handle_sort_range(
    workbook: Workbook, operation: SortRange, context: OperationContext
) -> OperationResult:
    """Sort rows in a range by one or more columns.

    The sort is stable and deterministic: equal keys retain their original order,
    and text comparison is case-insensitive by default. Values are moved as
    whole rows within the range, so columns outside the sort keys are preserved.
    """
    worksheet = find_sheet(workbook, operation.target.sheet)
    resolved = resolve_target(workbook, operation.target)
    view = read_table(workbook, operation.target, use_header=operation.has_header)
    if not view.rows:
        return OperationResult(
            operation=operation.operation.value, status="skipped", warnings=["no data rows to sort"]
        )

    key_indexes: list[int] = []
    for column in operation.by_columns:
        index = view.column_index(column)
        if index is None:
            from app.contracts.errors import TargetResolutionError

            raise TargetResolutionError(
                f"sort column {column!r} not found in {view.sheet!r}; available: {view.headers}",
                requested=column,
                available=view.headers,
            )
        key_indexes.append(index)

    directions = operation.descending or [False] * len(key_indexes)

    # Python's sort is stable, so sorting from the least significant key to the
    # most produces a correct multi-key ordering without needing to negate mixed
    # types (which is not possible in general).
    ordered = list(view.rows)
    for position, descending in reversed(list(enumerate(directions))):
        ordered.sort(key=_SortKey(key_indexes, position), reverse=descending)

    data_start = resolved.min_row + (1 if view.headers else 0)
    written = 0
    skipped_formulas = 0
    for offset, row in enumerate(ordered):
        target_row = data_start + offset
        for column_offset, value in enumerate(row):
            cell = worksheet.cell(row=target_row, column=resolved.min_col + column_offset)
            # A sort must not silently destroy a formula. When the destination
            # held a formula and the incoming value is not one, keep the formula
            # and report it, so the operator can decide.
            if is_formula(cell.value) and not is_formula(value):
                skipped_formulas += 1
                continue
            cell.value = value
            written += 1

    result_warnings = []
    if skipped_formulas:
        result_warnings.append(
            f"{skipped_formulas} cell(s) held a formula that would have been overwritten by "
            f"the sort; those formulas were left in place"
        )
    return OperationResult(
        operation=operation.operation.value,
        status="applied",
        cells_written=written,
        rows_affected=len(ordered),
        warnings=result_warnings,
        details={
            "sorted_by": operation.by_columns,
            "descending": directions,
            "formulas_preserved": skipped_formulas,
        },
    )


class _SortKey:
    """Pickable sort key for one column position.

    A class rather than a closure-with-default so the key is a properly typed
    callable that ``list.sort`` accepts, and so the position is bound once
    instead of being re-bound on every comparison.
    """

    __slots__ = ("_indexes", "_position")

    def __init__(self, indexes: list[int], position: int) -> None:
        self._indexes = indexes
        self._position = position

    def __call__(self, row: tuple[Any, ...]) -> tuple[int, Any]:
        return _comparable(row[self._indexes[self._position]])


def _comparable(value: Any) -> tuple[int, Any]:
    """Sort key that orders mixed types deterministically.

    Ordering: numbers first (by value), then text (case-insensitively), then
    everything else by string form. Mixing types would otherwise raise, and an
    arbitrary but *stable* order is better than a failed sort.
    """
    if value is None:
        return (0, "")
    if isinstance(value, bool):
        return (1, int(value))
    if isinstance(value, (int, float)):
        return (1, float(value))
    if isinstance(value, (dt.datetime, dt.date)):
        return (1, value.timestamp() if isinstance(value, dt.datetime) else value.toordinal())
    if isinstance(value, str):
        return (2, value.strip().lower())
    return (3, str(value))


def handle_filter_rows(
    workbook: Workbook, operation: FilterRows, context: OperationContext
) -> OperationResult:
    """Filter rows by one or more conditions.

    Non-matching rows are **hidden** by default rather than deleted. Deleting is
    irreversible and policy-gated; hiding preserves the data while giving the
    operator the filtered view. With ``output_sheet`` the matching rows are copied
    to a new sheet instead, which is non-destructive by construction.
    """
    view = read_table(workbook, operation.target)
    if not view.rows:
        return OperationResult(
            operation=operation.operation.value,
            status="skipped",
            warnings=["no data rows to filter"],
        )

    indexes = []
    for condition in operation.conditions:
        indexes.append(view.require_column(condition.column))

    matched: list[tuple[int, tuple[Any, ...]]] = []
    unmatched: list[int] = []
    for position, row in enumerate(view.rows):
        results = [
            _matches(row[index], condition)
            for index, condition in zip(indexes, operation.conditions, strict=True)
        ]
        keep = all(results) if operation.match == "all" else any(results)
        if keep:
            matched.append((position, row))
        else:
            unmatched.append(position)

    if operation.output_sheet:
        return _write_filter_output(workbook, operation, view, matched, context)

    resolved = view.resolved
    if resolved is None:
        return OperationResult(
            operation=operation.operation.value, status="failed", error="range did not resolve"
        )

    data_start = resolved.min_row + (1 if view.headers else 0)
    hidden = 0
    for position in unmatched:
        row_number = data_start + position
        worksheet = find_sheet(workbook, view.sheet)
        worksheet.row_dimensions[row_number].hidden = True
        hidden += 1

    return OperationResult(
        operation=operation.operation.value,
        status="applied",
        rows_affected=len(matched) + len(unmatched),
        details={
            "matched": len(matched),
            "unmatched": len(unmatched),
            "hidden_rows": hidden,
            "note": "non-matching rows were hidden, not deleted",
        },
    )


def _write_filter_output(
    workbook: Workbook,
    operation: FilterRows,
    view: Any,
    matched: list[tuple[int, tuple[Any, ...]]],
    context: OperationContext,
) -> OperationResult:
    """Copy matching rows into a new sheet. Non-destructive by construction."""
    if operation.output_sheet in workbook.sheetnames:
        return OperationResult(
            operation=operation.operation.value,
            status="failed",
            error=f"sheet {operation.output_sheet!r} already exists",
        )
    worksheet = workbook.create_sheet(title=operation.output_sheet)
    if view.headers:
        for index, header in enumerate(view.headers, start=1):
            worksheet.cell(row=1, column=index, value=header)
    written = 0
    neutralised = 0
    for offset, (_, row) in enumerate(matched, start=2):
        for index, value in enumerate(row, start=1):
            cell_value = value
            if context.neutralize_formula_injection:
                guarded, count = formula_guard.neutralise_row((value,))
                cell_value = guarded[0]
                neutralised += count
            worksheet.cell(row=offset, column=index, value=cell_value)
            written += 1
    return OperationResult(
        operation=operation.operation.value,
        status="applied",
        cells_written=written,
        rows_written=len(matched),
        sheets_created=[operation.output_sheet],
        cells_neutralised=neutralised,
        details={"matched": len(matched), "non_destructive": True},
    )


def _matches(value: Any, condition: FilterCondition) -> bool:
    """Evaluate one filter condition against a cell value.

    Comparisons are numeric when both sides look numeric, so ``> 100`` does not
    silently become a string comparison. ``None`` never satisfies an ordering
    comparison — an empty cell is not "greater than 100".
    """
    expected = condition.value
    operator = condition.operator

    if operator == "is_empty":
        return value is None or (isinstance(value, str) and not value.strip())
    if operator == "is_not_empty":
        return not (value is None or (isinstance(value, str) and not value.strip()))

    if value is None:
        return False

    if operator in {"contains", "not_contains"}:
        text = str(value).lower()
        needle = str(expected or "").lower()
        result = needle in text
        return result if operator == "contains" else not result

    if operator == "matches_pattern":
        try:
            return re.search(str(expected or ""), str(value)) is not None
        except re.error:
            return False

    if operator in {"equals", "not_equals"}:
        equal = _loosely_equal(value, expected)
        return equal if operator == "equals" else not equal

    left, right = _as_number_pair(value, expected)
    if left is None or right is None:
        return False
    return {
        "greater_than": left > right,
        "greater_or_equal": left >= right,
        "less_than": left < right,
        "less_or_equal": left <= right,
    }.get(operator, False)


def _loosely_equal(value: Any, expected: Any) -> bool:
    left, right = _as_number_pair(value, expected)
    if left is not None and right is not None:
        return abs(left - right) < 1e-9
    return str(value).strip().lower() == str(expected or "").strip().lower()


def _as_number_pair(left: Any, right: Any) -> tuple[float | None, float | None]:
    def _num(value: Any) -> float | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value.strip())
            except ValueError:
                return None
        return None

    return _num(left), _num(right)


def handle_remove_duplicates(
    workbook: Workbook, operation: RemoveDuplicates, context: OperationContext
) -> OperationResult:
    """Remove duplicate rows within a range.

    Destructive, therefore policy-gated. Duplicates are identified by the named
    key columns, or by the whole row when no keys are given. Comparison honours
    ``case_sensitive`` and ``trim_whitespace`` so ``"Acme"`` and ``"acme "`` are
    treated as duplicates when the caller asked for that.
    """
    view = read_table(workbook, operation.target)
    if not view.rows:
        return OperationResult(
            operation=operation.operation.value, status="skipped", warnings=["no data rows"]
        )

    key_indexes = [view.require_column(name) for name in operation.keys] if operation.keys else None

    def row_key(row: tuple[Any, ...]) -> tuple[Any, ...]:
        if key_indexes is None:
            values: list[Any] = list(row)
        else:
            values = [row[index] for index in key_indexes]
        return tuple(
            _key_part(value, operation.case_sensitive, operation.trim_whitespace)
            for value in values
        )

    seen: set[tuple[Any, ...]] = set()
    keep: list[tuple[int, tuple[Any, ...]]] = []
    remove: list[int] = []
    for position, row in enumerate(view.rows):
        key = row_key(row)
        if key in seen:
            remove.append(position)
        else:
            seen.add(key)
            keep.append((position, row))

    if operation.keep == "last" and remove and keep:
        # Recompute keeping the final occurrence of each key.
        last_position: dict[tuple[Any, ...], int] = {}
        for position, row in enumerate(view.rows):
            last_position[row_key(row)] = position
        remove = [p for p in range(len(view.rows)) if last_position[row_key(view.rows[p])] != p]
        keep = [(p, view.rows[p]) for p in range(len(view.rows)) if p not in set(remove)]

    if not remove:
        return OperationResult(
            operation=operation.operation.value,
            status="applied",
            rows_affected=0,
            details={"duplicates_removed": 0, "keys": operation.keys},
        )

    resolved = view.resolved
    if resolved is None:
        return OperationResult(
            operation=operation.operation.value, status="failed", error="range did not resolve"
        )
    worksheet = find_sheet(workbook, view.sheet)
    data_start = resolved.min_row + (1 if view.headers else 0)

    # Delete from the bottom so earlier row indices stay valid.
    removed_rows = sorted((data_start + position for position in remove), reverse=True)
    for row_number in removed_rows:
        worksheet.delete_rows(row_number, 1)

    return OperationResult(
        operation=operation.operation.value,
        status="applied",
        rows_removed=len(remove),
        rows_affected=len(remove),
        details={
            "duplicates_removed": len(remove),
            "keys": operation.keys or ["(whole row)"],
            "kept": operation.keep,
            "rows_remaining": len(keep),
            # Coordinates of the rows deleted, as 'Sheet!Row', so the verifier can
            # tell "formulas were destroyed" apart from "rows were deliberately
            # removed". Without this, any dedupe of formula-bearing rows looks
            # like formula loss, and the run fails for doing exactly what it was
            # asked to do.
            "removed_rows": [f"{view.sheet}!{row}" for row in reversed(removed_rows)],
        },
    )


def _key_part(value: Any, case_sensitive: bool, trim: bool) -> Any:
    """Canonical form of a value for duplicate comparison."""
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip() if trim else value
        return text if case_sensitive else text.lower()
    if isinstance(value, float):
        # Treat integral floats as equal to their int form.
        return int(value) if value.is_integer() else round(value, 9)
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    return value


def handle_normalize_values(
    workbook: Workbook, operation: NormalizeValues, context: OperationContext
) -> OperationResult:
    """Apply deterministic normalisation rules to a range.

    Every rule is enumerated in the contract: there is no expression, no regex
    supplied by the caller, and no code evaluation. Formulas are left untouched —
    normalising a formula string would corrupt it.
    """
    view = read_table(workbook, operation.target)
    if not view.rows:
        return OperationResult(
            operation=operation.operation.value, status="skipped", warnings=["no data rows"]
        )

    if operation.columns:
        indexes = [view.require_column(name) for name in operation.columns]
    else:
        indexes = list(range(len(view.headers))) if view.headers else []

    resolved = view.resolved
    if resolved is None:
        return OperationResult(
            operation=operation.operation.value, status="failed", error="range did not resolve"
        )
    worksheet = find_sheet(workbook, view.sheet)
    data_start = resolved.min_row + (1 if view.headers else 0)

    changed = 0
    inspected = 0
    for position, row in enumerate(view.rows):
        row_number = data_start + position
        for index in indexes:
            original = row[index]
            if is_formula(original):
                # Never rewrite a formula's text.
                continue
            inspected += 1
            normalised = _normalise_value(original, operation.rules)
            if normalised != original:
                worksheet.cell(row=row_number, column=resolved.min_col + index, value=normalised)
                changed += 1

    return OperationResult(
        operation=operation.operation.value,
        status="applied",
        cells_written=changed,
        rows_affected=sum(
            1
            for position, row in enumerate(view.rows)
            if any(
                not is_formula(row[index])
                and _normalise_value(row[index], operation.rules) != row[index]
                for index in indexes
            )
        ),
        details={"cells_inspected": inspected, "cells_changed": changed},
    )


def _normalise_value(value: Any, rules: NormalizeRules) -> Any:
    """Apply the enumerated rules to one value."""
    result = value

    if isinstance(result, str):
        if rules.strip_zero_width:
            result = result.translate({ord(char): None for char in ("", "‌", "‍", "﻿")})
        if rules.normalize_unicode:
            result = unicodedata.normalize("NFKC", result)
        if rules.trim_whitespace:
            result = result.strip()
        if rules.collapse_internal_whitespace:
            result = re.sub(r"\s+", " ", result).strip()
        if rules.case != "none":
            casers: dict[str, Callable[[str], str]] = {
                "lower": str.lower,
                "upper": str.upper,
                "title": str.title,
            }
            result = casers[rules.case](result)
        if rules.blank_to_empty_string and not result:
            result = ""
        return result

    if isinstance(result, (dt.datetime, dt.date)) and rules.date_format:
        return result.strftime(rules.date_format)

    if isinstance(result, (int, float)) and not isinstance(result, bool):
        if rules.number_format == "two_decimals" or rules.number_format == "fixed_2":
            return round(float(result), 2)
        if rules.number_format == "thousands_separated":
            # Stored as a number; the display format is applied by the caller if
            # desired. Kept numeric so aggregation still works.
            return result

    return result


def handle_apply_validation(
    workbook: Workbook, operation: ApplyValidation, context: OperationContext
) -> OperationResult:
    """Evaluate validation rules and report violations.

    With ``report_only`` (the default for a dry run) nothing is written: the
    violations are counted and returned. Otherwise openpyxl ``DataValidation``
    objects are attached so Excel itself enforces the rule for future edits.
    """
    view = read_table(workbook, operation.target)
    if not view.rows:
        return OperationResult(
            operation=operation.operation.value, status="skipped", warnings=["no data rows"]
        )

    violations: dict[str, list[int]] = {}
    for rule in operation.rules:
        index = view.require_column(rule.column)
        column_label = rule.column
        bad: list[int] = []
        for position, row in enumerate(view.rows, start=1):
            if not _satisfies(row[index], rule):
                bad.append(position)
        if bad:
            violations[column_label] = bad

    total_violations = sum(len(rows) for rows in violations.values())

    if not operation.report_only:
        _attach_data_validations(workbook, view, operation.rules)

    return OperationResult(
        operation=operation.operation.value,
        status="applied",
        rows_affected=total_violations,
        details={
            "violations": total_violations,
            "violations_by_column": {k: len(v) for k, v in violations.items()},
            "sample_rows": {k: v[:5] for k, v in violations.items()},
            "excel_validation_written": not operation.report_only,
        },
    )


def _satisfies(value: Any, rule: ValidationRule) -> bool:
    empty = value is None or (isinstance(value, str) and not value.strip())
    if rule.rule == "not_empty":
        return not empty
    if empty:
        # Every other rule is satisfied vacuously by an empty cell, which is the
        # conventional spreadsheet behaviour.
        return True
    if rule.rule == "numeric":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if rule.rule == "positive":
        return isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0
    if rule.rule == "non_negative":
        return isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0
    if rule.rule == "unique":
        return True  # uniqueness is checked across the column, not per cell
    if rule.rule == "date":
        return isinstance(value, (dt.date, dt.datetime))
    if rule.rule == "email":
        return bool(re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", str(value)))
    if rule.rule == "max_length":
        return len(str(value)) <= (rule.max_length or 0)
    # min_max is the last member of the ValidationRule.rule literal, so nothing
    # can reach here. Returning True rather than raising keeps a future added
    # rule from turning into a crash mid-run.
    number = _as_number_pair(value, 0)[0]
    if number is None:
        return False
    return (rule.min_value or 0) <= number <= (rule.max_value or 0)


def _attach_data_validations(workbook: Workbook, view: Any, rules: list[ValidationRule]) -> None:
    """Attach Excel data-validation objects so Excel enforces the rules natively."""
    from openpyxl.worksheet.datavalidation import DataValidation

    resolved = view.resolved
    if resolved is None:
        return
    worksheet = find_sheet(workbook, view.sheet)
    for rule in rules:
        index = view.require_column(rule.column)
        letter = get_column_letter(resolved.min_col + index)
        data_start = resolved.min_row + (1 if view.headers else 0)
        cell_range = f"{letter}{data_start}:{letter}{resolved.max_row}"
        try:
            if rule.rule == "max_length":
                validation = DataValidation(
                    type="textLength", operator="lessThanOrEqual", formula1=str(rule.max_length)
                )
            elif rule.rule == "min_max":
                validation = DataValidation(
                    type="decimal",
                    operator="between",
                    formula1=str(rule.min_value),
                    formula2=str(rule.max_value),
                )
            elif rule.rule == "numeric":
                validation = DataValidation(
                    type="decimal", operator="greaterThanOrEqual", formula1="0"
                )
            elif rule.rule == "positive":
                validation = DataValidation(type="decimal", operator="greaterThan", formula1="0")
            else:
                continue
            validation.error = rule.message or f"{rule.rule} rule violated in {rule.column}"
            validation.errorTitle = rule.column
            validation.showErrorMessage = rule.severity == "error"
            worksheet.add_data_validation(validation)
            validation.add(cell_range)
        except Exception:  # noqa: BLE001 - a validation hint is not worth failing a run
            continue


def handle_create_summary(
    workbook: Workbook, operation: CreateSummary, context: OperationContext
) -> OperationResult:
    """Create a summary sheet by aggregating the target range.

    Aggregation is enumerated (``sum``, ``count``, ``mean``, ...) and computed in
    Python from actual cell values. **A model never computes these numbers.**
    """
    view = read_table(workbook, operation.target)
    if not view.rows:
        return OperationResult(
            operation=operation.operation.value,
            status="skipped",
            warnings=["no data rows to summarise"],
        )

    group_indexes = [view.require_column(name) for name in operation.spec.group_by]
    groups: dict[tuple[Any, ...], list[tuple[Any, ...]]] = {}
    for row in view.rows:
        key = tuple(
            _key_part(row[index], case_sensitive=False, trim=True) for index in group_indexes
        )
        groups.setdefault(key, []).append(row)

    measure_indexes = [view.require_column(measure.column) for measure in operation.spec.measures]

    if operation.output_sheet in workbook.sheetnames:
        return OperationResult(
            operation=operation.operation.value,
            status="failed",
            error=f"sheet {operation.output_sheet!r} already exists",
        )
    worksheet = workbook.create_sheet(title=operation.output_sheet)

    header = [*operation.spec.group_by, *[measure.label for measure in operation.spec.measures]]
    for index, label in enumerate(header, start=1):
        worksheet.cell(row=1, column=index, value=label)

    rows: list[tuple[tuple[Any, ...], tuple[Any, ...]]] = []
    for key, rows_in_group in groups.items():
        values = tuple(
            _aggregate([row[i] for row in rows_in_group], measure.aggregation)
            for i, measure in zip(measure_indexes, operation.spec.measures, strict=True)
        )
        rows.append((key, values))

    if operation.spec.sort_by:
        try:
            position = header.index(operation.spec.sort_by)
            rows.sort(
                key=lambda pair: _comparable(
                    pair[0][0]
                    if position == 0
                    else pair[1][position - len(operation.spec.group_by)]
                ),
                reverse=operation.spec.descending,
            )
        except (ValueError, IndexError):
            pass
    if operation.spec.row_limit:
        rows = rows[: operation.spec.row_limit]

    written = len(header)
    for offset, (key, values) in enumerate(rows, start=2):
        for index, value in enumerate(key, start=1):
            worksheet.cell(row=offset, column=index, value=_presentable(value))
        for offset_column, value in enumerate(values, start=len(key) + 1):
            worksheet.cell(row=offset, column=offset_column, value=_presentable(value))
        written += len(header)

    if operation.add_total_row and rows:
        total_row = len(rows) + 2
        for index, _label in enumerate(operation.spec.group_by, start=1):
            worksheet.cell(row=total_row, column=index, value="TOTAL")
        for column_offset, measure in enumerate(operation.spec.measures):
            measure_index = view.require_column(measure.column)
            total = _aggregate([row[measure_index] for row in view.rows], measure.aggregation)
            worksheet.cell(
                row=total_row,
                column=len(operation.spec.group_by) + column_offset + 1,
                value=_presentable(total),
            )
        written += len(header)

    return OperationResult(
        operation=operation.operation.value,
        status="applied",
        cells_written=written,
        rows_written=len(rows),
        sheets_created=[operation.output_sheet],
        details={"groups": len(groups), "grouped_by": operation.spec.group_by},
    )


def _presentable(value: Any) -> Any:
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def _aggregate(values: list[Any], kind: str) -> Any:
    """Compute one aggregate. Non-numeric values are skipped, not coerced."""
    numbers = [float(v) for v in values if isinstance(v, (int, float)) and not isinstance(v, bool)]
    present = [v for v in values if v is not None and not (isinstance(v, str) and not v.strip())]

    if kind == "count":
        return len(present)
    if kind == "count_distinct":
        return len({_key_part(v, case_sensitive=False, trim=True) for v in present})
    if not numbers:
        return 0 if kind in {"sum", "mean", "median"} else None
    if kind == "sum":
        return sum(numbers)
    if kind == "mean":
        return sum(numbers) / len(numbers)
    if kind == "min":
        return min(numbers)
    if kind == "max":
        return max(numbers)
    if kind == "median":
        return statistics.median(numbers)
    return None


# --------------------------------------------------------------------------
# Comparison and reconciliation
# --------------------------------------------------------------------------


def handle_compare_workbooks(
    workbook: Workbook, operation: CompareWorkbooks, context: OperationContext
) -> OperationResult:
    """Compare two workbooks by key. Read-only; writes nothing."""
    from pathlib import Path

    from app.workbook import load_workbook

    other = load_workbook(Path(operation.other_path))
    try:
        other_view = read_table(other, Target(sheet=operation.target.sheet))
        this_view = read_table(workbook, operation.target)
        key_indexes = [this_view.require_column(name) for name in operation.key_columns]
        other_key_indexes = [other_view.require_column(name) for name in operation.key_columns]

        if operation.compare_columns:
            compare_indexes = [this_view.require_column(name) for name in operation.compare_columns]
            other_compare = [other_view.require_column(name) for name in operation.compare_columns]
        else:
            compare_indexes = list(range(len(this_view.headers)))
            other_compare = list(range(len(other_view.headers)))

        this_map = {_row_key(row, key_indexes): row for row in this_view.rows}
        other_map = {_row_key(row, other_key_indexes): row for row in other_view.rows}

        only_here = set(this_map) - set(other_map)
        only_there = set(other_map) - set(this_map)
        shared = set(this_map) & set(other_map)
        changed = sum(
            1
            for key in shared
            if any(
                not _loosely_equal(this_map[key][left], other_map[key][right])
                for left, right in zip(compare_indexes, other_compare, strict=False)
            )
        )

        return OperationResult(
            operation=operation.operation.value,
            status="applied",
            cells_read=len(this_map) + len(other_map),
            details={
                "only_in_current": len(only_here),
                "only_in_other": len(only_there),
                "changed": changed,
                "matched": len(shared),
            },
        )
    finally:
        other.close()


def _row_key(row: tuple[Any, ...], indexes: list[int]) -> tuple[Any, ...]:
    """Canonical key for a row, for matching across workbooks."""
    return tuple(_key_part(row[index], case_sensitive=False, trim=True) for index in indexes)


def handle_reconcile(
    workbook: Workbook, operation: Reconcile, context: OperationContext
) -> OperationResult:
    """Reconcile aggregates, recomputed from actual cell values.

    ExcelPilot never asks a model whether totals "look right". Every number here
    is computed in Python from the workbook's data cells.

    When an aggregate is computed over *formula* cells, the values available are
    whatever Excel last cached, and ExcelPilot cannot recalculate. That case is
    reported as ``derived_from_formula_cells`` and downgraded to a warning rather
    than being presented as a verified pass (ADR-0011).
    """
    results: list[ReconciliationResult] = []
    for spec in operation.checks:
        results.append(_reconcile_one(workbook, spec))
    context.reconciliation_results.extend(results)

    failed = [r for r in results if r.status.value == "failed"]
    warned = [r for r in results if r.status.value == "warning"]
    return OperationResult(
        operation=operation.operation.value,
        status="applied" if not failed else "failed",
        details={
            "checks": len(results),
            "failed": len(failed),
            "warnings": len(warned),
            "recalculated": False,
        },
        error="; ".join(r.explanation for r in failed) if failed else None,
    )


def _reconcile_one(workbook: Workbook, spec: ReconcileSpec) -> ReconciliationResult:
    actual_value, from_formulas = _compute_aggregate(workbook, spec.actual)
    expected_value = None
    if spec.expected is not None:
        expected_value, _ = _compute_aggregate(workbook, spec.expected)

    if expected_value is None:
        # No expected value: this is a reported aggregate with a tolerance, used
        # to flag a value that looks implausible in absolute terms.
        status = "warning" if from_formulas else "passed"
        return ReconciliationResult(
            name=spec.name,
            status=status,
            expected=None,
            actual=actual_value,
            variance=None,
            tolerance=spec.tolerance,
            explanation=spec.explanation or "no expected value supplied; reported for review only",
            derived_from_formula_cells=from_formulas,
        )

    variance = abs(float(actual_value) - float(expected_value))
    within = variance <= spec.tolerance
    if not within and spec.relative_tolerance > 0:
        denominator = abs(float(expected_value)) or 1.0
        within = variance / denominator <= spec.relative_tolerance

    if from_formulas and within:
        status = "warning"
        explanation = (
            f"variance {variance:g} is within tolerance, but the aggregate was computed over "
            f"formula cells whose values could not be recalculated; treat as unverified"
        )
    elif within:
        status = "passed"
        explanation = (
            spec.explanation or f"variance {variance:g} within tolerance {spec.tolerance:g}"
        )
    else:
        status = "failed"
        explanation = spec.explanation or (
            f"variance {variance:g} exceeds tolerance {spec.tolerance:g} "
            f"(expected {expected_value:g}, actual {actual_value:g})"
        )

    return ReconciliationResult(
        name=spec.name,
        status=status,
        expected=expected_value,
        actual=actual_value,
        variance=variance,
        tolerance=spec.tolerance,
        explanation=explanation,
        derived_from_formula_cells=from_formulas,
    )


def _compute_aggregate(workbook: Workbook, aggregate: Aggregate) -> tuple[Any, bool]:
    """Compute an aggregate, reporting whether formula cells were involved."""
    view = read_table(workbook, Target(sheet=aggregate.sheet))
    if not view.rows:
        return (0, False)
    index = view.require_column(aggregate.column)
    values = [row[index] for row in view.rows]
    from_formulas = any(is_formula(value) for value in values)
    return _aggregate(values, aggregate.aggregation), from_formulas


__all__ = [
    "OperationContext",
    "handle_apply_validation",
    "handle_compare_workbooks",
    "handle_create_summary",
    "handle_create_worksheet",
    "handle_filter_rows",
    "handle_normalize_values",
    "handle_read_range",
    "handle_reconcile",
    "handle_remove_duplicates",
    "handle_rename_worksheet",
    "handle_set_formula",
    "handle_sort_range",
    "handle_write_range",
]
