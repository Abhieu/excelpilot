"""Typed workbook operations.

This is a **closed discriminated union** (ADR-0006). There is no free-form
parameter dict and no string-keyed dispatch. Adding a member to
``WorkbookOperation`` makes every exhaustive handler fail to type-check until it
is handled, and ``tests/test_executor_registry.py`` additionally requires a
policy rule and a dry-run preview for each one.

Every model sets ``extra="forbid"`` (inherited from ``ContractModel``), so a
hallucinated field such as ``colours=["red"]`` on ``normalize_values`` is a
validation error and the whole plan is rejected.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from app.contracts.base import ContractModel
from app.contracts.enums import OperationKind
from app.contracts.workbook import MAX_EXCEL_COLUMNS, MAX_EXCEL_ROWS

CellScalar = str | int | float | bool | None


class Target(ContractModel):
    """A resolvable location in a workbook.

    Resolution is explicit: the executor resolves a ``Target`` against the live
    workbook and raises :class:`TargetResolutionError` if it does not exist. It
    never guesses.
    """

    sheet: str = Field(min_length=1, max_length=255)
    cell_range: str | None = Field(
        default=None,
        description="A1 range such as 'A1:F100'. Omit to mean the whole used area of the sheet.",
    )
    table: str | None = Field(
        default=None,
        description="Table (ListObject) name. Takes precedence over cell_range when present.",
    )
    header_row: int | None = Field(
        default=None,
        ge=1,
        le=MAX_EXCEL_ROWS,
        description="1-based row holding column headers, if the range includes one.",
    )

    @field_validator("sheet")
    @classmethod
    def _no_control_chars(cls, value: str) -> str:
        if any(ord(char) < 32 for char in value):
            raise ValueError("sheet name must not contain control characters")
        return value

    def describe(self) -> str:
        location = (
            f"{self.sheet}!{self.table}"
            if self.table
            else f"{self.sheet}!{self.cell_range or '(used area)'}"
        )
        return location


class ResolvedTarget(ContractModel):
    """A ``Target`` after resolution against a real workbook."""

    sheet: str
    min_row: int = Field(ge=1, le=MAX_EXCEL_ROWS)
    max_row: int = Field(ge=1, le=MAX_EXCEL_ROWS)
    min_col: int = Field(ge=1, le=MAX_EXCEL_COLUMNS)
    max_col: int = Field(ge=1, le=MAX_EXCEL_COLUMNS)
    table_name: str | None = None
    header_row: int | None = None

    @property
    def cell_count(self) -> int:
        return (self.max_row - self.min_row + 1) * (self.max_col - self.min_col + 1)

    @property
    def data_row_count(self) -> int:
        """Rows excluding the header, when a header row is known."""
        return max(self.max_row - self.min_row + 1 - (1 if self.header_row is not None else 0), 0)

    def coordinate(self, row: int, col: int) -> str:
        from app.contracts.base import column_letter

        return f"{column_letter(col)}{row}"


# --------------------------------------------------------------------------
# Read
# --------------------------------------------------------------------------


class ReadRange(ContractModel):
    operation: Literal[OperationKind.READ_RANGE] = OperationKind.READ_RANGE
    target: Target
    include_formulas: bool = True
    max_rows: int = Field(default=1_000, ge=1, le=100_000)


# --------------------------------------------------------------------------
# Write
# --------------------------------------------------------------------------


class WriteRange(ContractModel):
    operation: Literal[OperationKind.WRITE_RANGE] = OperationKind.WRITE_RANGE
    target: Target
    values: list[list[CellScalar]] = Field(description="Row-major values")
    write_header: bool = Field(
        default=False,
        description="Treat the first row of values as headers and write them to the header row.",
    )
    neutralize_formula_injection: bool = Field(
        default=True,
        description="Write formula-like leading characters (= + - @) as literal text.",
    )

    @model_validator(mode="after")
    def _ragged_rows_rejected(self) -> WriteRange:
        if self.values and any(len(row) != len(self.values[0]) for row in self.values):
            raise ValueError("values must be rectangular; every row needs the same number of cells")
        return self


class SetFormula(ContractModel):
    operation: Literal[OperationKind.SET_FORMULA] = OperationKind.SET_FORMULA
    target: Target
    formulas: list[str] = Field(description="Row-major formulas, each starting with '='")
    fill_direction: Literal["rows", "columns"] = "rows"

    @field_validator("formulas")
    @classmethod
    def _must_start_with_equals(cls, values: list[str]) -> list[str]:
        for index, value in enumerate(values):
            if not value.startswith("="):
                raise ValueError(f"formula at index {index} must start with '='")
        return values


# --------------------------------------------------------------------------
# Structural
# --------------------------------------------------------------------------


class CreateWorksheet(ContractModel):
    operation: Literal[OperationKind.CREATE_WORKSHEET] = OperationKind.CREATE_WORKSHEET
    name: str = Field(
        min_length=1, max_length=31, description="Excel's 31-character sheet-name limit"
    )
    position: int | None = Field(default=None, ge=0)
    state: Literal["visible", "hidden"] = "visible"

    @field_validator("name")
    @classmethod
    def _excel_name_rules(cls, value: str) -> str:
        if any(char in value for char in "[]:*?/\\"):
            raise ValueError(f"invalid sheet name {value!r}: Excel forbids []:*?/\\")
        if value.startswith("'") or value.endswith("'"):
            raise ValueError("sheet name must not start or end with an apostrophe")
        return value


class RenameWorksheet(ContractModel):
    operation: Literal[OperationKind.RENAME_WORKSHEET] = OperationKind.RENAME_WORKSHEET
    from_name: str = Field(min_length=1, max_length=255)
    to_name: str = Field(min_length=1, max_length=31)

    @field_validator("to_name")
    @classmethod
    def _excel_name_rules(cls, value: str) -> str:
        if any(char in value for char in "[]:*?/\\"):
            raise ValueError(f"invalid sheet name {value!r}: Excel forbids []:*?/\\")
        return value

    @model_validator(mode="after")
    def _must_change(self) -> RenameWorksheet:
        if self.from_name.strip().lower() == self.to_name.strip().lower():
            raise ValueError("from_name and to_name are the same sheet")
        return self


# --------------------------------------------------------------------------
# Data operations
# --------------------------------------------------------------------------


class SortRange(ContractModel):
    operation: Literal[OperationKind.SORT_RANGE] = OperationKind.SORT_RANGE
    target: Target
    by_columns: list[str] = Field(
        min_length=1, description="Column header names, most significant first"
    )
    descending: list[bool] = Field(default_factory=list)
    has_header: bool = True

    @model_validator(mode="after")
    def _length_match(self) -> SortRange:
        if self.descending and len(self.descending) != len(self.by_columns):
            raise ValueError("descending must have the same length as by_columns")
        return self


class FilterRows(ContractModel):
    operation: Literal[OperationKind.FILTER_ROWS] = OperationKind.FILTER_ROWS
    target: Target
    conditions: list[FilterCondition] = Field(min_length=1)
    match: Literal["all", "any"] = "all"
    output_sheet: str | None = Field(
        default=None,
        description="Write matching rows to this new sheet instead of deleting non-matching rows.",
    )
    hide_non_matching: bool = Field(
        default=True,
        description="Hide rather than delete. Deleting is destructive and policy-gated.",
    )


class FilterCondition(ContractModel):
    column: str = Field(min_length=1)
    operator: Literal[
        "equals",
        "not_equals",
        "greater_than",
        "greater_or_equal",
        "less_than",
        "less_or_equal",
        "contains",
        "not_contains",
        "is_empty",
        "is_not_empty",
        "matches_pattern",
    ]
    value: str | None = None

    @model_validator(mode="after")
    def _value_required_except_empty_checks(self) -> FilterCondition:
        needs_value = self.operator not in {"is_empty", "is_not_empty"}
        if needs_value and (self.value is None or self.value == ""):
            raise ValueError(f"operator {self.operator!r} requires a value")
        return self


class RemoveDuplicates(ContractModel):
    operation: Literal[OperationKind.REMOVE_DUPLICATES] = OperationKind.REMOVE_DUPLICATES
    target: Target
    keys: list[str] = Field(
        default_factory=list,
        description="Column header names forming the duplicate key. Empty means whole-row duplicates.",
    )
    keep: Literal["first", "last"] = "first"
    case_sensitive: bool = False
    trim_whitespace: bool = True


class NormalizeRules(ContractModel):
    """Deterministic normalisation rules. No code, no expressions, no evaluation."""

    trim_whitespace: bool = False
    collapse_internal_whitespace: bool = False
    case: Literal["none", "lower", "upper", "title"] = "none"
    normalize_unicode: bool = Field(
        default=False,
        description="Apply NFKC normalisation. Helps with full-width and compatibility characters.",
    )
    blank_to_empty_string: bool = False
    strip_zero_width: bool = False
    number_format: Literal["none", "thousands_separated", "two_decimals", "fixed_2"] = "none"
    date_format: str | None = Field(
        default=None,
        description="strftime format applied to date/datetime cells, e.g. '%Y-%m-%d'.",
    )


class NormalizeValues(ContractModel):
    operation: Literal[OperationKind.NORMALIZE_VALUES] = OperationKind.NORMALIZE_VALUES
    target: Target
    columns: list[str] = Field(default_factory=list, description="Empty means every column.")
    rules: NormalizeRules


class ValidationRule(ContractModel):
    """A data-validation rule written to a column."""

    column: str = Field(min_length=1)
    rule: Literal[
        "not_empty",
        "numeric",
        "positive",
        "non_negative",
        "unique",
        "date",
        "email",
        "max_length",
        "min_max",
    ]
    value: str | None = None
    max_length: int | None = Field(default=None, ge=1)
    min_value: float | None = None
    max_value: float | None = None
    severity: Literal["error", "warning"] = "error"
    message: str | None = None

    @model_validator(mode="after")
    def _rule_specific_requirements(self) -> ValidationRule:
        if self.rule == "max_length" and self.max_length is None:
            raise ValueError("max_length rule requires max_length")
        if self.rule == "min_max" and (self.min_value is None or self.max_value is None):
            raise ValueError("min_max rule requires min_value and max_value")
        if (
            self.rule
            in {"not_empty", "numeric", "positive", "non_negative", "unique", "date", "email"}
            and self.value is not None
        ):
            raise ValueError(f"rule {self.rule!r} does not take a value")
        return self


class ApplyValidation(ContractModel):
    operation: Literal[OperationKind.APPLY_VALIDATION] = OperationKind.APPLY_VALIDATION
    target: Target
    rules: list[ValidationRule] = Field(min_length=1)
    report_only: bool = Field(
        default=False,
        description="Collect violations without writing Excel validation objects.",
    )


class SummarySpec(ContractModel):
    """Declarative summary sheet. Aggregation is enumerated, never computed by a model."""

    group_by: list[str] = Field(min_length=1)
    measures: list[Measure] = Field(min_length=1)
    sort_by: str | None = None
    descending: bool = False
    row_limit: int | None = Field(default=None, ge=1, le=1_000_000)


class Measure(ContractModel):
    column: str = Field(min_length=1)
    aggregation: Literal["sum", "count", "count_distinct", "mean", "min", "max", "median"]
    alias: str | None = None

    @property
    def label(self) -> str:
        return self.alias or f"{self.aggregation}({self.column})"


class CreateSummary(ContractModel):
    operation: Literal[OperationKind.CREATE_SUMMARY] = OperationKind.CREATE_SUMMARY
    target: Target
    output_sheet: str = Field(min_length=1, max_length=31)
    spec: SummarySpec
    add_total_row: bool = False


class CompareWorkbooks(ContractModel):
    operation: Literal[OperationKind.COMPARE_WORKBOOKS] = OperationKind.COMPARE_WORKBOOKS
    target: Target
    other_path: str = Field(min_length=1)
    key_columns: list[str] = Field(min_length=1)
    compare_columns: list[str] = Field(default_factory=list)


class ReconcileSpec(ContractModel):
    """A reconciliation check: compare an expected aggregate to an actual one.

    ``actual`` is always recomputed deterministically from cell values. ExcelPilot
    never asks a model whether totals "look right" (spec section 19).
    """

    name: str = Field(min_length=1, max_length=200)
    actual: Aggregate
    expected: Aggregate | None = None
    tolerance: float = Field(
        default=0.0,
        ge=0,
        description="Absolute tolerance. 0 means exact equality required.",
    )
    relative_tolerance: float = Field(default=0.0, ge=0, le=1)
    severity: Literal["error", "warning"] = "error"
    explanation: str | None = None

    @model_validator(mode="after")
    def _needs_a_comparison(self) -> ReconcileSpec:
        if self.expected is None and self.tolerance == 0 and self.relative_tolerance == 0:
            raise ValueError(
                "reconciliation needs an expected aggregate or a non-zero tolerance; "
                "a check with neither compares nothing"
            )
        return self

    @property
    def is_comparison(self) -> bool:
        """Whether this check actually compares two things."""
        return self.expected is not None or self.tolerance > 0 or self.relative_tolerance > 0


class Aggregate(ContractModel):
    """A named aggregate over a sheet's data, used as actual or expected."""

    sheet: str = Field(min_length=1)
    column: str = Field(min_length=1)
    aggregation: Literal["sum", "count", "count_distinct", "mean", "min", "max", "median"]
    where: FilterCondition | None = None


class Reconcile(ContractModel):
    operation: Literal[OperationKind.RECONCILE] = OperationKind.RECONCILE
    target: Target
    checks: list[ReconcileSpec] = Field(min_length=1)


WorkbookOperation = Annotated[
    ReadRange
    | WriteRange
    | SetFormula
    | CreateWorksheet
    | RenameWorksheet
    | SortRange
    | FilterRows
    | RemoveDuplicates
    | NormalizeValues
    | ApplyValidation
    | CreateSummary
    | CompareWorkbooks
    | Reconcile,
    Field(discriminator="operation"),
]

SUPPORTED_OPERATION_KINDS: frozenset[OperationKind] = frozenset(member for member in OperationKind)


__all__ = [
    "Aggregate",
    "ApplyValidation",
    "CellScalar",
    "CompareWorkbooks",
    "CreateSummary",
    "CreateWorksheet",
    "FilterCondition",
    "FilterRows",
    "Measure",
    "NormalizeRules",
    "NormalizeValues",
    "ReadRange",
    "Reconcile",
    "ReconcileSpec",
    "RemoveDuplicates",
    "RenameWorksheet",
    "ResolvedTarget",
    "SetFormula",
    "SortRange",
    "SUPPORTED_OPERATION_KINDS",
    "SummarySpec",
    "Target",
    "ValidationRule",
    "WorkbookOperation",
    "WriteRange",
]
