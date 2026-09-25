"""Workbook inspection.

Produces a :class:`WorkbookInspection`: everything ExcelPilot knows about a
workbook before it plans anything. Read-only, deterministic, and free of any AI
or policy dependency.

This is the grounding for the whole pipeline. The planner addresses columns by
the header names recorded here, and policy reads the sensitivity classification.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import date, datetime
from pathlib import Path
from typing import Any

from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from app.contracts.config import LimitsConfig
from app.contracts.workbook import (
    DefinedNameMetadata,
    FormulaMetadata,
    SensitivityClassification,
    SheetMetadata,
    TableMetadata,
    WorkbookInspection,
    WorkbookMetadata,
)
from app.workbook.hashing import file_sha256, is_formula, normalise_value
from app.workbook.limits import (
    check_formula_count,
    check_sheet_count,
    check_sheet_shape,
)
from app.workbook.reader import (
    has_external_links,
    has_vba,
    load_workbook,
    sheet_state,
)

#: Header names that suggest sensitive data. Deliberately conservative: a false
#: positive raises scrutiny, which is the safe direction to be wrong in.
SENSITIVE_PATTERNS: tuple[tuple[str, str], ...] = (
    ("ssn", "restricted"),
    ("social security", "restricted"),
    ("passport", "restricted"),
    ("national id", "restricted"),
    ("pan", "restricted"),
    ("card number", "restricted"),
    ("credit card", "restricted"),
    ("cvv", "restricted"),
    ("password", "restricted"),
    ("passwd", "restricted"),
    ("secret", "restricted"),
    ("api key", "restricted"),
    ("api_key", "restricted"),
    ("token", "confidential"),
    ("private key", "restricted"),
    ("salary", "confidential"),
    ("bank account", "confidential"),
    ("account number", "confidential"),
    ("routing", "confidential"),
    ("dob", "confidential"),
    ("date of birth", "confidential"),
    ("medical", "confidential"),
    ("diagnosis", "confidential"),
)

#: A1-style reference inside a formula, with optional sheet qualifier.
_REFERENCE = re.compile(
    r"(?:(?:'([^']+)'|([A-Za-z_][A-Za-z0-9_.]*))!)?"
    r"(\$?[A-Z]{1,3}\$?\d{1,7})(?::(\$?[A-Z]{1,3}\$?\d{1,7}))?"
)

#: An external workbook reference, e.g. [Budget.xlsx]Sheet1!A1 or [1]Sheet1!A1
_EXTERNAL = re.compile(r"\[[^\]]+\]")


class WorkbookInspector:
    """Builds a :class:`WorkbookInspection` from a workbook on disk."""

    def __init__(self, limits: LimitsConfig | None = None) -> None:
        self.limits = limits or LimitsConfig()

    def inspect(self, path: str | Path) -> WorkbookInspection:
        """Inspect a workbook. Read-only: the file is never modified."""
        path = Path(path).expanduser()
        workbook = load_workbook(path, limits=self.limits)
        try:
            return self._build(path, workbook)
        finally:
            workbook.close()

    def _build(self, path: Path, workbook: Any) -> WorkbookInspection:
        check_sheet_count(len(workbook.sheetnames), self.limits)

        sheets: list[SheetMetadata] = []
        tables: list[TableMetadata] = []
        formulas: list[FormulaMetadata] = []
        total_rows = 0
        total_non_empty = 0
        hidden_count = 0

        for index, worksheet in enumerate(workbook.worksheets):
            sheet_meta, sheet_formulas, sheet_tables, stats = self._inspect_sheet(worksheet, index)
            sheets.append(sheet_meta)
            formulas.extend(sheet_formulas)
            tables.extend(sheet_tables)
            total_rows += stats["rows"]
            total_non_empty += stats["cells"]
            if not sheet_meta.is_visible:
                hidden_count += 1

        check_formula_count(len(formulas), self.limits)

        defined_names = self._inspect_defined_names(workbook)
        metadata = WorkbookMetadata(
            **_document_properties(workbook),
            has_vba=has_vba(path),
            has_external_links=has_external_links(path),
            defined_name_count=len(defined_names),
            table_count=len(tables),
            sheet_count=len(sheets),
        )

        return WorkbookInspection(
            path=str(path),
            file_name=path.name,
            file_size_bytes=path.stat().st_size,
            content_hash=file_sha256(path),
            extension=path.suffix.lower().lstrip("."),
            sheet_names=list(workbook.sheetnames),
            sheets=sheets,
            tables=tables,
            defined_names=defined_names,
            formulas=formulas,
            metadata=metadata,
            total_rows=total_rows,
            total_formulas=len(formulas),
            total_non_empty_cells=total_non_empty,
            hidden_sheet_count=hidden_count,
            sensitivity=self._classify_sensitivity(sheets),
        )

    def _inspect_sheet(
        self, worksheet: Worksheet, index: int
    ) -> tuple[SheetMetadata, list[FormulaMetadata], list[TableMetadata], dict[str, int]]:
        max_row = worksheet.max_row or 0
        max_col = worksheet.max_column or 0
        state = sheet_state(worksheet)

        non_empty = 0
        formula_count = 0
        formulas: list[FormulaMetadata] = []
        header_row: list[str] = []

        for row in worksheet.iter_rows():
            for cell in row:
                if cell.value is None:
                    continue
                non_empty += 1
                if is_formula(cell.value):
                    formula_count += 1
                    if len(formulas) < self.limits.max_formula_count:
                        formulas.append(self._formula_metadata(worksheet.title, cell))

        if max_row and max_col:
            check_sheet_shape(
                sheet_name=worksheet.title,
                max_row=max_row,
                max_column=max_col,
                non_empty_cells=non_empty,
                limits=self.limits,
            )

        header_row = self._read_header(worksheet, max_col)
        tables = self._inspect_tables(worksheet)

        # Defined names scoped to this sheet.
        scoped = list(getattr(worksheet, "defined_names", {}).values())

        metadata = SheetMetadata(
            name=worksheet.title,
            index=index,
            state=state,
            max_row=max_row,
            max_column=max_col,
            dimensions=worksheet.calculate_dimension() if max_row and max_col else "A1",
            non_empty_cells=non_empty,
            formula_count=formula_count,
            header_row=header_row,
            table_names=[table.name for table in tables],
            defined_names=[str(getattr(name, "name", name)) for name in scoped],
            has_data_validations=bool(
                getattr(worksheet, "data_validations", None)
                and len(worksheet.data_validations.dataValidation) > 0
            ),
            has_conditional_formatting=bool(
                list(getattr(worksheet, "conditional_formatting", []) or [])
            ),
            is_empty=non_empty == 0,
        )
        return metadata, formulas, tables, {"rows": max_row, "cells": non_empty}

    def _read_header(self, worksheet: Worksheet, max_col: int) -> list[str]:
        """Best-effort first-row values, used to address columns by name.

        Empty when the sheet has no data. Header detection beyond row 1 is out of
        scope; a caller that needs a different header row passes ``header_row``
        explicitly on the operation.
        """
        if not max_col or worksheet.max_row < 1:
            return []
        headers: list[str] = []
        for column in range(1, min(max_col, 256) + 1):
            value = worksheet.cell(row=1, column=column).value
            headers.append("" if value is None else normalise_value(value))
        return headers

    def _inspect_tables(self, worksheet: Worksheet) -> list[TableMetadata]:
        """Read the worksheet's Excel tables.

        ``worksheet.tables`` is openpyxl's ``TableList``, a ``dict`` subclass with
        a **surprising API**: it iterates over names, ``.values()`` yields ``Table``
        objects, but ``.items()`` yields ``(name, table.ref)`` string pairs
        rather than ``(name, Table)``. Iterating ``dict(table_list).items()``
        bypasses that override and gives the real objects, which is what this
        needs. Guarded with a type check so a future openpyxl change surfaces as
        a clear error rather than a confusing AttributeError.
        """
        tables: list[TableMetadata] = []
        table_list = getattr(worksheet, "tables", None)
        if not table_list:
            return tables
        for name, table in dict(table_list).items():
            columns = [
                str(getattr(column, "name", column))
                for column in getattr(table, "tableColumns", []) or []
            ]
            tables.append(
                TableMetadata(
                    name=str(name),
                    display_name=str(getattr(table, "displayName", name) or name),
                    ref=str(getattr(table, "ref", "") or ""),
                    header_row=bool(getattr(table, "headerRowCount", 1)),
                    totals_row=bool(getattr(table, "totalsRowCount", 0)),
                    column_names=columns,
                    style_name=getattr(getattr(table, "tableStyleInfo", None), "name", None),
                )
            )
        return tables

    def _inspect_defined_names(self, workbook: Any) -> list[DefinedNameMetadata]:
        names: list[DefinedNameMetadata] = []
        for name, defined in getattr(workbook, "defined_names", {}).items():
            names.append(
                DefinedNameMetadata(
                    name=str(name),
                    ref=str(getattr(defined, "attr_text", "") or getattr(defined, "value", "")),
                    scope="workbook",
                    hidden=bool(getattr(defined, "hidden", False)),
                )
            )
        for worksheet in workbook.worksheets:
            for name, defined in getattr(worksheet, "defined_names", {}).items():
                names.append(
                    DefinedNameMetadata(
                        name=str(name),
                        ref=str(getattr(defined, "attr_text", "") or getattr(defined, "value", "")),
                        scope=worksheet.title,
                        hidden=bool(getattr(defined, "hidden", False)),
                    )
                )
        return names

    def _formula_metadata(self, sheet: str, cell: Any) -> FormulaMetadata:
        formula = str(cell.value)
        return FormulaMetadata(
            sheet=sheet,
            coordinate=cell.coordinate,
            formula=formula,
            references=sorted(set(self._extract_references(formula))),
            has_external_reference=bool(_EXTERNAL.search(formula)),
        )

    @staticmethod
    def _extract_references(formula: str) -> Iterator[str]:
        """Yield the sheet/range references a formula contains.

        String literals are skipped so a cell containing the text "=SUM(A1:A2)"
        is not mistaken for a reference.
        """
        body = formula[1:] if formula.startswith("=") else formula
        for match in _REFERENCE.finditer(body):
            quoted, bare, start, end = match.groups()
            sheet = quoted or bare
            if sheet:
                yield f"{sheet}!{start}{':' + end if end else ''}"
            else:
                yield f"{start}{':' + end if end else ''}"

    def _classify_sensitivity(self, sheets: list[SheetMetadata]) -> SensitivityClassification:
        """Conservative sensitivity classification from header names.

        A heuristic, and labelled as one: it is not a data-protection assessment.
        It exists so policy can raise scrutiny on obviously sensitive columns.
        """
        matched: list[str] = []
        level = "public"
        ranking = {"public": 0, "internal": 1, "confidential": 2, "restricted": 3}
        for sheet in sheets:
            for header in sheet.header_row:
                lowered = header.strip().lower()
                if not lowered:
                    continue
                for needle, assigned in SENSITIVE_PATTERNS:
                    if needle in lowered and ranking[assigned] > ranking[level]:
                        level = assigned
                        matched.append(f"{sheet.name}.{header} ({needle})")
        return SensitivityClassification(level=level, matched_signals=matched)


def inspect_workbook(path: str | Path, *, limits: LimitsConfig | None = None) -> WorkbookInspection:
    """Convenience wrapper around :class:`WorkbookInspector`."""
    return WorkbookInspector(limits).inspect(path)


def column_letters(index: int) -> str:
    """Re-exported so callers need not import openpyxl directly."""
    return str(get_column_letter(index))


def _clean(value: Any) -> str | None:
    """Normalise a possibly-missing metadata string to None or stripped text."""
    if value is None:
        return None
    return str(value).strip() or None


def _document_properties(workbook: Any) -> dict[str, Any]:
    """Extract document properties defensively.

    openpyxl's ``DocumentProperties`` does not expose every field the OOXML
    spec allows, and a hand-edited or exotic workbook can leave fields unset.
    Reading through ``getattr`` with a default keeps inspection working on
    unusual-but-valid files instead of failing the whole run.
    """
    properties = getattr(workbook, "properties", None)
    if properties is None:
        return {}

    def _stamp(name: str) -> str | None:
        value = getattr(properties, name, None)
        return value.isoformat() if isinstance(value, (datetime, date)) else _clean(value)

    return {
        "title": _clean(getattr(properties, "title", None)),
        "creator": _clean(getattr(properties, "creator", None)),
        "last_modified_by": _clean(getattr(properties, "lastModifiedBy", None)),
        "created": _stamp("created"),
        "modified": _stamp("modified"),
        # openpyxl stores the producing application under a non-obvious name.
        "application": _clean(getattr(properties, "appVersion", None))
        or _clean(getattr(properties, "application", None)),
    }


__all__ = ["SENSITIVE_PATTERNS", "WorkbookInspector", "column_letters", "inspect_workbook"]
