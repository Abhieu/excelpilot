"""Content-based workbook diff.

**Never compares bytes.** A load/save cycle rewrites the OOXML zip, so an untouched
workbook still produces a different file — measured, not assumed (ADR-0009). A
byte comparison would report a change on every run, and an operator who learns
to ignore that signal has no signal at all.

Instead the diff runs on logical cell content: ``(sheet, coordinate, value,
data_type, number_format, style_id)``, keyed by ``(sheet, coordinate)``. Values
are normalised first so float noise and timezone representation do not produce
phantom changes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from openpyxl.workbook import Workbook

from app.contracts.verification import CellChange, SheetDiff, WorkbookDiff
from app.workbook.hashing import normalise_value

#: How many individual cell changes to record. Beyond this the per-sheet and
#: workbook totals stay exact, and the list is marked truncated — an operator
#: needs the count and a sample, not 4 million strings.
MAX_CELL_CHANGES = 500

#: How many sample changes to keep per sheet, for the report.
MAX_SAMPLES_PER_SHEET = 25


@dataclass(frozen=True, slots=True)
class _CellRecord:
    """One cell's logical content."""

    value: str
    data_type: str
    number_format: str
    style_id: int

    @property
    def is_formula(self) -> bool:
        return self.data_type == "f" or self.value.startswith("=")


def _scan(workbook: Workbook) -> dict[tuple[str, str], _CellRecord]:
    """Every non-empty cell, keyed by ``(sheet, coordinate)``.

    Iterates only populated cells rather than the full declared rectangle, which
    matters: a sheet declaring 37,883x120 is mostly empty and walking it blindly
    would dominate the diff's cost.
    """
    records: dict[tuple[str, str], _CellRecord] = {}
    for worksheet in workbook.worksheets:
        title = worksheet.title
        for row in worksheet.iter_rows():
            for cell in row:
                if cell.value is None:
                    continue
                records[(title, cell.coordinate)] = _CellRecord(
                    value=normalise_value(cell.value),
                    data_type=str(cell.data_type or ""),
                    number_format=cell.number_format or "General",
                    style_id=int(getattr(cell, "style_id", 0) or 0),
                )
    return records


def _sheet_index(workbook: Workbook) -> dict[str, set[str]]:
    """Sheet name -> set of table names defined on it."""
    index: dict[str, set[str]] = {}
    for worksheet in workbook.worksheets:
        # TableList.items() yields (name, ref) strings; dict() reaches the objects.
        index[worksheet.title] = {
            str(name) for name in dict(getattr(worksheet, "tables", None) or {})
        }
    return index


def _defined_names(workbook: Workbook) -> set[str]:
    names = {str(name) for name in getattr(workbook, "defined_names", {})}
    for worksheet in workbook.worksheets:
        names.update(str(name) for name in getattr(worksheet, "defined_names", {}))
    return names


def _classify(before: _CellRecord | None, after: _CellRecord | None) -> str | None:
    """Classify one cell's change, or ``None`` if it is unchanged."""
    if before is None and after is not None:
        return "added"
    if before is not None and after is None:
        return "removed"
    if before is None or after is None:
        return None

    was_formula = before.is_formula
    is_formula_now = after.is_formula
    if before.value != after.value:
        if was_formula and not is_formula_now:
            # A formula replaced by a literal is the most consequential kind of
            # change there is, so it is called out rather than lumped in.
            return "formula"
        if not was_formula and is_formula_now:
            return "formula"
        return "value"
    if before.number_format != after.number_format or before.style_id != after.style_id:
        # style_id is a workbook-local index and can be renumbered on save, so
        # this means "formatting may have changed", which the manifest states.
        return "formatting"
    return None


def diff_workbooks(
    before_workbook: Workbook,
    after_workbook: Workbook,
    *,
    before_name: str = "before",
    after_name: str = "after",
) -> WorkbookDiff:
    """Diff two open workbooks by logical content."""
    before_cells = _scan(before_workbook)
    after_cells = _scan(after_workbook)
    before_sheets = list(before_workbook.sheetnames)
    after_sheets = list(after_workbook.sheetnames)

    added_sheets = [name for name in after_sheets if name not in before_sheets]
    removed_sheets = [name for name in before_sheets if name not in after_sheets]

    # A rename is inferred when a sheet disappears and an identical-content sheet
    # appears, which is reported as a rename rather than a delete plus an add.
    renamed = _detect_renames(removed_sheets, added_sheets, before_workbook, after_workbook)
    renamed_from = {entry["from"] for entry in renamed}
    renamed_to = {entry["to"] for entry in renamed}

    sheet_diffs: list[SheetDiff] = []
    cell_changes: list[CellChange] = []
    # Truncation is reported when *either* cap bit: the per-sheet sample cap, or
    # the workbook-wide cap. Reporting only the latter would silently imply the
    # list is complete when a large single sheet was actually sampled down.
    truncated = False

    # Diff each sheet exactly once. A sheet present in both workbooks is visited
    # a single time; a renamed-away sheet is skipped; a renamed-to sheet is
    # diffed as a wholly new sheet.
    to_visit: list[str] = []
    for title in before_sheets:
        if title not in renamed_from:
            to_visit.append(title)
    for title in after_sheets:
        if title not in before_sheets and title not in renamed_to:
            to_visit.append(title)

    for title in to_visit:
        diff, changes, sheet_truncated = _diff_sheet(
            title, before_cells, after_cells, before_workbook, after_workbook
        )
        sheet_diffs.append(diff)
        truncated = truncated or sheet_truncated
        remaining = MAX_CELL_CHANGES - len(cell_changes)
        if len(changes) > remaining:
            truncated = True
            cell_changes.extend(changes[:remaining])
        else:
            cell_changes.extend(changes)

    # Report in the order the sheets appear in the resulting workbook.
    order = {name: index for index, name in enumerate(after_sheets)}
    sheet_diffs.sort(key=lambda d: (order.get(d.name, len(order)), d.name))

    before_tables = _sheet_index(before_workbook)
    after_tables = _sheet_index(after_workbook)
    tables_added = sorted(
        {t for sheet, names in after_tables.items() for t in names}
        - {t for sheet, names in before_tables.items() for t in names}
    )
    tables_removed = sorted(
        {t for sheet, names in before_tables.items() for t in names}
        - {t for sheet, names in after_tables.items() for t in names}
    )

    before_names = _defined_names(before_workbook)
    after_names = _defined_names(after_workbook)

    structural = bool(
        added_sheets
        or removed_sheets
        or renamed
        or tables_added
        or tables_removed
        or (before_names != after_names)
    )

    return WorkbookDiff(
        before_hash="",
        after_hash="",
        before_name=before_name,
        after_name=after_name,
        sheets_added=added_sheets,
        sheets_removed=[name for name in removed_sheets if name not in renamed_from],
        sheets_renamed=renamed,
        sheet_diffs=sheet_diffs,
        cell_changes=cell_changes,
        cell_changes_truncated=truncated,
        tables_added=tables_added,
        tables_removed=tables_removed,
        defined_names_added=sorted(after_names - before_names),
        defined_names_removed=sorted(before_names - after_names),
        structural_change=structural,
        total_cell_changes=sum(d.cells_changed for d in sheet_diffs),
        total_formula_changes=sum(
            d.formulas_added + d.formulas_removed + d.formulas_changed for d in sheet_diffs
        ),
        total_formatting_changes=sum(d.formatting_changed for d in sheet_diffs),
        notes=[
            "diff is computed on logical cell content, not file bytes: saving a "
            "workbook rewrites the OOXML package even when nothing changed",
            "formatting counts use a workbook-local style index, which can be "
            "renumbered on save; treat them as 'may have changed'",
        ],
    )


def _diff_sheet(
    title: str,
    before_cells: dict[tuple[str, str], _CellRecord],
    after_cells: dict[tuple[str, str], _CellRecord],
    before_workbook: Workbook,
    after_workbook: Workbook,
) -> tuple[SheetDiff, list[CellChange], bool]:
    """Diff one sheet."""
    before_keys = {key for key in before_cells if key[0] == title}
    after_keys = {key for key in after_cells if key[0] == title}

    truncated = False
    cells_added = 0
    cells_removed = 0
    cells_changed = 0
    formulas_added = 0
    formulas_removed = 0
    formulas_changed = 0
    formatting = 0
    samples: list[CellChange] = []

    for key in before_keys | after_keys:
        before = before_cells.get(key)
        after = after_cells.get(key)
        kind = _classify(before, after)
        if kind is None:
            continue

        coordinate = key[1]
        if kind == "added":
            cells_added += 1
            if after and after.is_formula:
                formulas_added += 1
            change = CellChange(
                sheet=title,
                coordinate=coordinate,
                change=kind,
                after=after.value if after else None,
            )
        elif kind == "removed":
            cells_removed += 1
            if before and before.is_formula:
                formulas_removed += 1
            change = CellChange(
                sheet=title,
                coordinate=coordinate,
                change=kind,
                before=before.value if before else None,
            )
        else:
            assert before is not None and after is not None
            if kind == "formula":
                cells_changed += 1
                if before.is_formula and not after.is_formula:
                    formulas_removed += 1
                elif not before.is_formula and after.is_formula:
                    formulas_added += 1
                else:
                    formulas_changed += 1
            elif kind == "value":
                cells_changed += 1
            else:
                formatting += 1
            change = CellChange(
                sheet=title,
                coordinate=coordinate,
                change=kind,
                before=before.value,
                after=after.value,
            )
        if len(samples) < MAX_SAMPLES_PER_SHEET:
            samples.append(change)
        else:
            # Dropped from the sample list; the per-sheet counts above stay exact.
            truncated = True

    before_sheet = before_workbook[title] if title in before_workbook.sheetnames else None
    after_sheet = after_workbook[title] if title in after_workbook.sheetnames else None

    return (
        SheetDiff(
            name=title,
            state_before=_state(before_sheet),
            state_after=_state(after_sheet),
            rows_before=(before_sheet.max_row if before_sheet else 0) or 0,
            rows_after=(after_sheet.max_row if after_sheet else 0) or 0,
            columns_before=(before_sheet.max_column if before_sheet else 0) or 0,
            columns_after=(after_sheet.max_column if after_sheet else 0) or 0,
            cells_added=cells_added,
            cells_removed=cells_removed,
            cells_changed=cells_changed,
            formulas_added=formulas_added,
            formulas_removed=formulas_removed,
            formulas_changed=formulas_changed,
            formatting_changed=formatting,
        ),
        samples,
        truncated,
    )


def _state(worksheet: Any) -> str:
    return str(getattr(worksheet, "sheet_state", "visible") or "visible")


def _detect_renames(
    removed: list[str],
    added: list[str],
    before_workbook: Workbook,
    after_workbook: Workbook,
) -> list[dict[str, str]]:
    """Infer renames by matching a removed sheet to an added one with identical content.

    Content-based rather than name-based, because a rename is exactly a change of
    name with no change of content. Ambiguous matches (two sheets with identical
    content) are left alone — reporting a rename we are not sure about would be
    worse than reporting an add and a delete.
    """
    if not removed or not added:
        return []

    def fingerprint(workbook: Workbook, title: str) -> str:
        return _content_fingerprint(workbook[title])

    before_map: dict[str, list[str]] = {}
    for title in removed:
        before_map.setdefault(fingerprint(before_workbook, title), []).append(title)
    after_map: dict[str, list[str]] = {}
    for title in added:
        after_map.setdefault(fingerprint(after_workbook, title), []).append(title)

    renames: list[dict[str, str]] = []
    for fingerprint_value, old_names in before_map.items():
        new_names = after_map.get(fingerprint_value)
        # Only a unique one-to-one match is a confident rename.
        if new_names and len(old_names) == 1 and len(new_names) == 1:
            renames.append({"from": old_names[0], "to": new_names[0]})
    return renames


def _content_fingerprint(worksheet: Any) -> str:
    """Hash a sheet's logical content, for rename detection."""
    import hashlib

    digest = hashlib.sha256()
    for row in worksheet.iter_rows():
        for cell in row:
            if cell.value is None:
                continue
            digest.update(
                f"{cell.coordinate}\x1f{normalise_value(cell.value)}\x1e".encode(
                    "utf-8", errors="replace"
                )
            )
    return digest.hexdigest()


def diff_paths(before_path: Any, after_path: Any, **kwargs: Any) -> WorkbookDiff:
    """Diff two workbooks on disk."""
    from app.workbook import file_sha256, opened

    with opened(before_path) as before, opened(after_path) as after:
        result = diff_workbooks(
            before,
            after,
            before_name=str(before_path),
            after_name=str(after_path),
            **kwargs,
        )
    return result.model_copy(
        update={
            "before_hash": file_sha256(before_path),
            "after_hash": file_sha256(after_path),
        }
    )


def summarise(diff: WorkbookDiff, *, limit: int = 20) -> str:
    """Human-readable diff summary."""
    lines: list[str] = []
    for rename in diff.sheets_renamed:
        lines.append(f"  renamed {rename['from']} -> {rename['to']}")
    for name in diff.sheets_added:
        lines.append(f"  + sheet {name}")
    for name in diff.sheets_removed:
        lines.append(f"  - sheet {name}")
    lines.append(f"  cells changed:   {diff.total_cell_changes:,}")
    lines.append(f"  formulas changed: {diff.total_formula_changes:,}")
    lines.append(f"  formatting:      {diff.total_formatting_changes:,}")
    if diff.cell_changes:
        lines.append("")
        lines.append("Sample changes:")
        for change in diff.cell_changes[:limit]:
            lines.append(f"  {change.describe()}")
        if diff.cell_changes_truncated:
            lines.append("  ... (list truncated; totals above are exact)")
    elif not diff.structural_change:
        lines.append("  no changes")
    return "\n".join(lines)


__all__ = ["MAX_CELL_CHANGES", "diff_paths", "diff_workbooks", "summarise"]
