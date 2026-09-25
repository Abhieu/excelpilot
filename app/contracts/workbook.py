"""Workbook inspection contracts.

These describe what ExcelPilot *found*. They are produced by
``app.workbook.inspector`` and consumed by the planner, policy engine, and
verification subsystem. Nothing here mutates.
"""

from __future__ import annotations

from typing import Any

from pydantic import Field, field_validator

from app.contracts.base import ContractModel
from app.contracts.enums import RiskLevel

#: Excel's hard limits. Requests beyond these are invalid, not merely large.
MAX_EXCEL_ROWS = 1_048_576
MAX_EXCEL_COLUMNS = 16_384


class TableMetadata(ContractModel):
    """An Excel table (ListObject) definition."""

    name: str
    display_name: str
    ref: str = Field(description="A1 range, e.g. 'A1:F100'")
    header_row: bool = True
    totals_row: bool = False
    column_names: list[str] = Field(default_factory=list)
    style_name: str | None = None

    @property
    def row_count(self) -> int:
        """Data rows, excluding the header (and totals) row."""
        return max(
            self._span_rows - (1 if self.header_row else 0) - (1 if self.totals_row else 0), 0
        )

    @property
    def column_count(self) -> int:
        return len(self.column_names)

    @property
    def _span_rows(self) -> int:
        # _parse_a1_range returns (min_col, min_row, max_col, max_row), so the
        # row count is the FOURTH element. Getting this wrong silently reports
        # the column count as the row count.
        _, _, _, max_row = _parse_a1_range(self.ref)
        return max_row


class DefinedNameMetadata(ContractModel):
    """A workbook- or sheet-scoped defined name (named range)."""

    name: str
    ref: str = Field(description="What the name resolves to, e.g. 'Sales!$A$1:$A$99'")
    scope: str = Field(default="workbook", description="'workbook' or a worksheet title")
    hidden: bool = False

    @field_validator("name")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("defined name must not be empty")
        return value


class FormulaMetadata(ContractModel):
    """One formula, with the information needed to reason about it statically."""

    sheet: str
    coordinate: str
    formula: str = Field(description="The formula text including the leading '='")
    references: list[str] = Field(
        default_factory=list,
        description="Sheet/range references found in the formula, e.g. 'Sales!$A$1:$A$9'",
    )
    has_external_reference: bool = Field(
        default=False,
        description="True if the formula references another workbook ([book]Sheet!A1).",
    )

    @property
    def function_names(self) -> list[str]:
        """Uppercased function names used, e.g. ``['SUM', 'IF']``."""
        found: list[str] = []
        remainder = self.formula
        while "(" in remainder:
            start = remainder.index("(")
            head = remainder[:start]
            token = ""
            for char in reversed(head):
                if char.isalnum() or char in "._":
                    token = char + token
                else:
                    break
            name = token.upper()
            if name and name not in found:
                found.append(name)
            remainder = remainder[start + 1 :]
        return found


class SheetMetadata(ContractModel):
    """Everything ExcelPilot learned about one worksheet."""

    name: str
    index: int = Field(default=0, ge=0)
    state: str = Field(default="visible", description="visible | hidden | veryHidden")
    max_row: int = Field(default=0, ge=0)
    max_column: int = Field(default=0, ge=0)
    dimensions: str = Field(default="A1", description="Declared dimension, e.g. 'A1:F100'")
    non_empty_cells: int = Field(default=0, ge=0)
    formula_count: int = Field(default=0, ge=0)
    header_row: list[str] = Field(
        default_factory=list,
        description="Best-effort first-row values, used by the planner to address columns by name.",
    )
    table_names: list[str] = Field(default_factory=list)
    defined_names: list[str] = Field(default_factory=list)
    has_data_validations: bool = False
    has_conditional_formatting: bool = False
    is_empty: bool = False

    @property
    def is_visible(self) -> bool:
        return self.state == "visible"

    @property
    def is_protected(self) -> bool:
        """A veryHidden sheet is ExcelPilot's structural equivalent of locked.

        Policy requires approval before touching one, because it is usually a
        lookup table or internal state sheet the operator did not mean to edit.
        """
        return self.state == "veryHidden"

    @property
    def approximate_cells(self) -> int:
        return self.max_row * self.max_column

    def column_index(self, name: str) -> int | None:
        """1-based index of a column by header name, case-insensitive."""
        target = name.strip().lower()
        for index, header in enumerate(self.header_row, start=1):
            if header.strip().lower() == target:
                return index
        return None

    def find_header(self, *candidates: str) -> int | None:
        """First matching column index among candidate names, case-insensitive.

        Tolerant of the small naming variations real workbooks contain
        (``Customer`` / ``CustomerId`` / ``customer_id``).
        """
        for candidate in candidates:
            exact = self.column_index(candidate)
            if exact is not None:
                return exact
        lowered = [header.strip().lower() for header in self.header_row]
        for candidate in candidates:
            target = candidate.strip().lower()
            for index, header in enumerate(lowered, start=1):
                if header and (target in header or header in target):
                    return index
        return None


class WorkbookMetadata(ContractModel):
    """Workbook-level facts: creator, dates, calculation settings, structure flags."""

    title: str | None = None
    creator: str | None = None
    last_modified_by: str | None = None
    created: str | None = None
    modified: str | None = None
    application: str | None = None
    has_vba: bool = False
    has_external_links: bool = False
    defined_name_count: int = Field(default=0, ge=0)
    table_count: int = Field(default=0, ge=0)
    sheet_count: int = Field(default=0, ge=0)


class DataProfile(ContractModel):
    """Deterministic profile of a column's values.

    Computed by counting, never by asking a model. Used by the planner to ground
    requests in facts and by anomaly detection to spot distributions that moved.
    """

    column: str
    row_count: int = Field(ge=0)
    null_count: int = Field(ge=0, le=1_048_576)
    distinct_count: int = Field(ge=0)
    numeric_count: int = Field(ge=0)
    text_count: int = Field(ge=0)
    formula_count: int = Field(ge=0)
    duplicate_count: int = Field(ge=0)
    min_value: float | None = None
    max_value: float | None = None
    sum_value: float | None = None

    @property
    def null_rate(self) -> float:
        return self.null_count / self.row_count if self.row_count else 0.0

    @property
    def duplicate_rate(self) -> float:
        return self.duplicate_count / self.row_count if self.row_count else 0.0

    @property
    def is_numeric(self) -> bool:
        return self.row_count > 0 and self.numeric_count == self.row_count - self.null_count


class SensitivityClassification(ContractModel):
    """Data-sensitivity assessment, from deterministic header-name and content signals.

    Feeds policy. Currently conservative: it looks for indicators such as
    ``ssn``, ``pan``, ``credit card``, ``password``, ``api key``, ``dob``,
    ``passport``, and high-entropy numeric columns. It is a heuristic and is
    labelled as such — it is not a data-protection certification.
    """

    level: str = Field(
        default="public", description="public | internal | confidential | restricted"
    )
    matched_signals: list[str] = Field(default_factory=list)
    method: str = "header_name_and_pattern_heuristic"

    @property
    def risk(self) -> RiskLevel:
        return {
            "public": RiskLevel.LOW,
            "internal": RiskLevel.LOW,
            "confidential": RiskLevel.MEDIUM,
            "restricted": RiskLevel.HIGH,
        }.get(self.level, RiskLevel.MEDIUM)


class WorkbookInspection(ContractModel):
    """The complete result of inspecting a workbook. Read-only."""

    path: str
    file_name: str
    file_size_bytes: int = Field(ge=0)
    content_hash: str = Field(description="SHA-256 of the source file bytes")
    extension: str = Field(description="xlsx | xlsm")
    sheet_names: list[str] = Field(default_factory=list)
    sheets: list[SheetMetadata] = Field(default_factory=list)
    tables: list[TableMetadata] = Field(default_factory=list)
    defined_names: list[DefinedNameMetadata] = Field(default_factory=list)
    formulas: list[FormulaMetadata] = Field(default_factory=list)
    metadata: WorkbookMetadata = Field(default_factory=WorkbookMetadata)

    total_rows: int = Field(default=0, ge=0)
    total_formulas: int = Field(default=0, ge=0)
    total_non_empty_cells: int = Field(default=0, ge=0)
    hidden_sheet_count: int = Field(default=0, ge=0)
    sensitivity: SensitivityClassification = Field(default_factory=SensitivityClassification)

    def sheet(self, name: str) -> SheetMetadata | None:
        """Case-insensitive sheet lookup."""
        target = name.strip().lower()
        for sheet in self.sheets:
            if sheet.name.strip().lower() == target:
                return sheet
        return None

    def table(self, name: str) -> TableMetadata | None:
        target = name.strip().lower()
        for table in self.tables:
            if table.name.strip().lower() == target or table.display_name.strip().lower() == target:
                return table
        return None

    def summary(self) -> dict[str, Any]:
        """Compact dict for CLI and prompt use."""
        return {
            "file": self.file_name,
            "sheets": [
                {
                    "name": s.name,
                    "state": s.state,
                    "rows": s.max_row,
                    "columns": s.max_column,
                    "formulas": s.formula_count,
                    "tables": s.table_names,
                }
                for s in self.sheets
            ],
            "total_rows": self.total_rows,
            "total_formulas": self.total_formulas,
            "hidden_sheets": self.hidden_sheet_count,
            "tables": [t.name for t in self.tables],
            "defined_names": [d.name for d in self.defined_names],
            "sensitivity": self.sensitivity.level,
        }


def _parse_a1_range(reference: str) -> tuple[int, int, int, int]:
    """Parse ``'A1:F100'`` into ``(min_col, min_row, max_col, max_row)``, all 1-based.

    A deliberately small A1 subset parser: single cells, rectangular ranges, and
    whole-column forms. It exists to answer "how big is this table" without
    pulling a formula parser into a contract module.
    """
    from app.contracts.base import column_index, parse_a1

    text = reference.strip().replace("$", "")
    if ":" not in text:
        row, col = parse_a1(text)
        return col, row, col, row
    start_text, end_text = text.split(":", 1)
    if start_text.isalpha() or end_text.isalpha():
        min_col, max_col = column_index(start_text), column_index(end_text)
        min_row, max_row = 1, MAX_EXCEL_ROWS
    else:
        min_row, min_col = parse_a1(start_text)
        max_row, max_col = parse_a1(end_text)
    return (
        min(min_col, max_col),
        min(min_row, max_row),
        max(min_col, max_col),
        max(min_row, max_row),
    )


__all__ = [
    "MAX_EXCEL_COLUMNS",
    "MAX_EXCEL_ROWS",
    "DataProfile",
    "DefinedNameMetadata",
    "FormulaMetadata",
    "SensitivityClassification",
    "SheetMetadata",
    "TableMetadata",
    "WorkbookInspection",
    "WorkbookMetadata",
]
