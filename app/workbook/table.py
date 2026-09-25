"""Target resolution and tabular reads.

The single place that turns a :class:`Target` (a *name*) into a
:class:`ResolvedTarget` (actual coordinates), and the single place that reads a
range into rows.

Resolution never guesses. A sheet or table that does not exist raises
:class:`TargetResolutionError` listing what *was* found, because
"TargetResolutionError: sheet 'Sale' not found; available: ['Sales', 'Summary']"
tells an operator what to fix, and a silent partial write does not.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from app.contracts.base import column_index, parse_a1
from app.contracts.errors import TargetResolutionError
from app.contracts.operations import ResolvedTarget, Target

#: Whole-column reference, e.g. "A:A" or "$A:$C"
_WHOLE_COLUMNS = re.compile(r"^\$?([A-Za-z]{1,3})(?::\$?([A-Za-z]{1,3}))?$")
#: Whole-row reference, e.g. "1:1"
_WHOLE_ROWS = re.compile(r"^\$?(\d{1,7})(?::\$?(\d{1,7}))?$")

#: Excel's last row/column. Used to bound whole-column/row references so they
#: do not imply a 1M-row scan.
_EXCEL_MAX_ROW = 1_048_576
_EXCEL_MAX_COL = 16_384


@dataclass(slots=True)
class TableView:
    """A resolved range read into memory, with its header row separated out.

    ``rows`` are tuples of *raw* cell values (not normalised strings) so
    downstream numeric aggregation stays numeric. Use
    :meth:`WorkbookInspector._read_header`-style normalisation only where a
    string is genuinely wanted.
    """

    sheet: str
    headers: list[str] = field(default_factory=list)
    rows: list[tuple[Any, ...]] = field(default_factory=list)
    resolved: ResolvedTarget | None = None

    @property
    def row_count(self) -> int:
        return len(self.rows)

    @property
    def column_count(self) -> int:
        return len(self.headers)

    def column_index(self, name: str) -> int | None:
        """0-based index of a column by header name, case-insensitive."""
        target = name.strip().lower()
        for index, header in enumerate(self.headers):
            if header.strip().lower() == target:
                return index
        return None

    def require_column(self, name: str) -> int:
        """0-based column index, or a resolution error naming what exists.

        Used by operations that were given a column name, so a typo produces
        "column 'Amunt' not found in Sales; available: [...]" rather than an
        IndexError.
        """
        index = self.column_index(name)
        if index is None:
            raise TargetResolutionError(
                f"column {name!r} not found in sheet {self.sheet!r}; "
                f"available: {self.headers or '(no header row)'}",
                requested=name,
                available=self.headers,
            )
        return index

    def value(self, row_index: int, column: str) -> Any:
        return self.rows[row_index][self.require_column(column)]

    def column_values(self, column: str) -> list[Any]:
        index = self.require_column(column)
        return [row[index] for row in self.rows]

    def to_dicts(self) -> list[dict[str, Any]]:
        """Rows as dicts keyed by header. Duplicate headers get a numeric suffix."""
        seen: dict[str, int] = {}
        keys: list[str] = []
        for header in self.headers:
            if header in seen:
                seen[header] += 1
                keys.append(f"{header}_{seen[header]}")
            else:
                seen[header] = 0
                keys.append(header)
        return [dict(zip(keys, row, strict=False)) for row in self.rows]


def find_sheet(workbook: Any, name: str) -> Worksheet:
    """Case-insensitive sheet lookup, or a resolution error listing the options."""
    target = name.strip().lower()
    for worksheet in workbook.worksheets:
        if worksheet.title.strip().lower() == target:
            return worksheet
    raise TargetResolutionError(
        f"sheet {name!r} not found; available: {[ws.title for ws in workbook.worksheets]}",
        requested=name,
        available=[ws.title for ws in workbook.worksheets],
    )


def resolve_range(reference: str, worksheet: Worksheet) -> tuple[int, int, int, int]:
    """Resolve an A1-style reference to ``(min_row, min_col, max_row, max_col)``.

    Handles single cells, rectangular ranges, and whole-column/whole-row forms.
    Whole-column forms are bounded by the sheet's used area rather than Excel's
    full 1,048,576 rows, so a request for ``A:A`` on a small sheet does not imply
    scanning a million rows.
    """
    text = reference.strip().replace("$", "")
    if not text:
        raise ValueError("empty range reference")

    whole_column = _WHOLE_COLUMNS.match(text)
    if whole_column:
        start, end = whole_column.groups()
        min_col = column_index(start)
        max_col = column_index(end) if end else min_col
        max_row = max(worksheet.max_row or 1, 1)
        return 1, min_col, max_row, max_col

    whole_row = _WHOLE_ROWS.match(text)
    if whole_row:
        start, end = whole_row.groups()
        min_row = int(start)
        max_row = int(end) if end else min_row
        max_col = max(worksheet.max_column or 1, 1)
        return min(min_row, max_row), 1, max(min_row, max_row), max_col

    if ":" not in text:
        row, col = parse_a1(text)
        return row, col, row, col

    start_text, end_text = text.split(":", 1)
    min_row, min_col = parse_a1(start_text)
    max_row, max_col = parse_a1(end_text)
    return (
        min(min_row, max_row),
        min(min_col, max_col),
        max(min_row, max_row),
        max(max_col, max_col),
    )


def resolve_target(workbook: Any, target: Target) -> ResolvedTarget:
    """Resolve a Target against a live workbook, or raise.

    A table name takes precedence over an explicit range, because naming a table
    is the more specific statement of intent.
    """
    worksheet = find_sheet(workbook, target.sheet)

    if target.table:
        # See the note in app/workbook/inspector.py: TableList.items() returns
        # (name, ref) strings, so dict() is required to reach the Table objects.
        table_list = getattr(worksheet, "tables", None) or {}
        for name, table in dict(table_list).items():
            if str(name).strip().lower() == target.table.strip().lower():
                min_row, min_col, max_row, max_col = resolve_range(str(table.ref), worksheet)
                return ResolvedTarget(
                    sheet=worksheet.title,
                    min_row=min_row,
                    max_row=max_row,
                    min_col=min_col,
                    max_col=max_col,
                    table_name=str(name),
                    header_row=min_row if getattr(table, "headerRowCount", 1) else None,
                )
        raise TargetResolutionError(
            f"table {target.table!r} not found on sheet {worksheet.title!r}; "
            f"available tables: {list(table_list) or '(none)'}",
            requested=target.table,
            available=[str(name) for name in table_list],
        )

    if target.cell_range:
        try:
            min_row, min_col, max_row, max_col = resolve_range(target.cell_range, worksheet)
        except (ValueError, KeyError) as error:
            raise TargetResolutionError(
                f"could not parse range {target.cell_range!r} on sheet {worksheet.title!r}: {error}",
                requested=target.cell_range,
            ) from error
    else:
        # Whole used area.
        min_row, min_col = 1, 1
        max_row = max(worksheet.max_row or 1, 1)
        max_col = max(worksheet.max_column or 1, 1)

    if min_row < 1 or min_col < 1 or max_row > _EXCEL_MAX_ROW or max_col > _EXCEL_MAX_COL:
        raise TargetResolutionError(
            f"range {target.cell_range or '(used area)'} on {worksheet.title!r} resolves outside "
            f"Excel's bounds",
            requested=target.cell_range or "(used area)",
        )

    header_row = target.header_row
    if header_row is not None and not (min_row <= header_row <= max_row):
        raise TargetResolutionError(
            f"header_row {header_row} is outside the resolved range "
            f"{min_row}:{max_row} on {worksheet.title!r}",
            requested=str(header_row),
        )

    return ResolvedTarget(
        sheet=worksheet.title,
        min_row=min_row,
        max_row=max_row,
        min_col=min_col,
        max_col=max_col,
        table_name=None,
        header_row=header_row,
    )


def read_table(
    workbook: Any,
    target: Target,
    *,
    max_rows: int | None = None,
    use_header: bool = True,
) -> TableView:
    """Read a resolved range into a :class:`TableView`.

    When ``use_header`` and the target names a header row (or the range starts at
    row 1), the first row becomes ``headers`` and is excluded from ``rows``.
    """
    resolved = resolve_target(workbook, target)
    worksheet = find_sheet(workbook, resolved.sheet)

    header_index = resolved.header_row
    # Heuristic: when the range starts at row 1 and no header row was named,
    # treat row 1 as a header if it is entirely non-numeric text and has a row
    # beneath it. Real operational sheets follow this convention; a caller who
    # knows better passes header_row explicitly.
    if (
        use_header
        and header_index is None
        and resolved.min_row == 1
        and resolved.max_row > resolved.min_row
        and _looks_like_header(worksheet, resolved)
    ):
        header_index = resolved.min_row

    headers: list[str] = []
    data_start = resolved.min_row
    if header_index is not None:
        for column in range(resolved.min_col, resolved.max_col + 1):
            value = worksheet.cell(row=header_index, column=column).value
            headers.append("" if value is None else str(value).strip())
        data_start = header_index + 1

    limit = (
        resolved.max_row if max_rows is None else min(resolved.max_row, data_start + max_rows - 1)
    )
    rows: list[tuple[Any, ...]] = []
    for row in range(data_start, limit + 1):
        rows.append(
            tuple(
                worksheet.cell(row=row, column=column).value
                for column in range(resolved.min_col, resolved.max_col + 1)
            )
        )

    return TableView(
        sheet=resolved.sheet,
        headers=headers,
        rows=rows,
        resolved=resolved,
    )


def _looks_like_header(worksheet: Worksheet, resolved: ResolvedTarget) -> bool:
    """Whether row 1 reads as a header: all text, and row 2 has some content."""
    header_cells = [
        worksheet.cell(row=resolved.min_row, column=column).value
        for column in range(resolved.min_col, resolved.max_col + 1)
    ]
    present = [value for value in header_cells if value is not None]
    if not present:
        return False
    if not all(isinstance(value, str) for value in present):
        return False
    second_row = [
        worksheet.cell(row=resolved.min_row + 1, column=column).value
        for column in range(resolved.min_col, resolved.max_col + 1)
    ]
    return any(value is not None for value in second_row)


def expand_range(reference: str, max_cells: int = 100_000) -> list[str]:
    """Expand a small range into individual coordinates.

    Refuses ranges beyond ``max_cells`` so a request for ``A1:XFD1048576`` fails
    fast instead of building a list of 17 billion strings.
    """
    total = 0
    min_col = min_row = 0
    max_col = max_row = 0
    text = reference.strip().replace("$", "")
    if ":" in text:
        start_text, end_text = text.split(":", 1)
        min_row, min_col = parse_a1(start_text)
        max_row, max_col = parse_a1(end_text)
    else:
        min_row, min_col = parse_a1(text)
        max_row, max_col = min_row, min_col
    total = (max_row - min_row + 1) * (max_col - min_col + 1)
    if total > max_cells:
        raise ValueError(
            f"range {reference!r} covers {total:,} cells, above the {max_cells:,} limit for expansion"
        )
    return [
        f"{get_column_letter(column)}{row}"
        for row in range(min_row, max_row + 1)
        for column in range(min_col, max_col + 1)
    ]


__all__ = [
    "TableView",
    "expand_range",
    "find_sheet",
    "read_table",
    "resolve_range",
    "resolve_target",
]
