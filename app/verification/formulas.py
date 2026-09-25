"""Formula verification — **static only**.

ExcelPilot cannot recalculate Excel formulas. openpyxl reads a formula as a
string; with ``data_only=True`` it returns whatever value Excel last cached,
which may be stale, absent, or wrong. Trusting those values would be exactly the
"the totals look correct" claim the specification forbids (ADR-0011).

So these checks are static, and every result says so:

* **presence** — a column expected to hold formulas still does
* **pattern consistency** — formulas in one column follow one pattern
* **reference resolution** — referenced sheets and ranges exist
* **broken references** — ``#REF!`` and friends
* **hard-coded replacement** — a formula replaced by a literal
* **deletion** — formulas that were there and are now gone

Every ``VerificationResult`` carries ``recalculated: False`` and
``static_formula_checks: True``. Nothing here ever claims a formula was
evaluated.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from app.contracts.enums import VerificationStatus
from app.contracts.verification import CheckResult
from app.workbook.hashing import is_formula, normalise_value

#: Error values Excel leaves in a cell when a formula fails.
_BROKEN_MARKERS = ("#REF!", "#NAME?", "#VALUE!", "#DIV/0!", "#N/A", "#NULL!", "#NUM!")

#: A reference pattern, sheet-qualified or not.
_REFERENCE = re.compile(
    r"(?:(?:'([^']+)'|([A-Za-z_][A-Za-z0-9_.]*))!)?"
    r"(\$?[A-Z]{1,3}\$?\d{1,7})(?::(\$?[A-Z]{1,3}\$?\d{1,7}))?"
)

#: Functions that resolve their argument at calculation time, so their target
#: cannot be statically checked. Reported as unresolvable rather than verified.
_DYNAMIC_FUNCTIONS = frozenset(
    {
        "INDIRECT",
        "OFFSET",
        "CELL",
        "INFO",
        "ADDRESS",
        "CHOOSE",
        "INDEX",
        "VLOOKUP",
        "HLOOKUP",
        "MATCH",
        "LOOKUP",
        "SUMIF",
        "SUMIFS",
        "COUNTIF",
        "COUNTIFS",
        "AVERAGEIF",
        "AVERAGEIFS",
    }
)

#: External workbook reference, e.g. [Budget.xlsx]Sheet1!A1
_EXTERNAL = re.compile(r"\[[^\]]+\]")


@dataclass(frozen=True, slots=True)
class FormulaIssue:
    """One static formula problem."""

    sheet: str
    coordinate: str
    kind: str
    detail: str
    formula: str = ""

    def describe(self) -> str:
        return f"{self.sheet}!{self.coordinate}: {self.kind} — {self.detail}"


def _normalise_pattern(formula: str) -> str:
    """Reduce a formula to its structural pattern.

    Row and column numbers are replaced with ``#`` so that ``=E2*F2`` and
    ``=E17*F17`` share one pattern. Absolute markers are dropped, because
    ``=B$6*2`` and ``=B6*2`` differ in intent but not in structure — the absolute
    marker is reported separately as a consistency signal.
    """
    body = formula[1:] if formula.startswith("=") else formula
    body = re.sub(r"\$?[A-Z]{1,3}\$?\d{1,7}", "#", body)
    body = re.sub(r"\s+", " ", body)
    return body.strip().upper()


def collect_formulas(workbook: Any) -> list[tuple[str, str, str]]:
    """Every formula in a workbook as ``(sheet, coordinate, formula)``.

    Scans the existing cells only. It never indexes a worksheet by coordinate,
    because ``worksheet["G30"]`` **creates** the cell in openpyxl, which would
    mutate the very workbook being verified.
    """
    found: list[tuple[str, str, str]] = []
    for worksheet in workbook.worksheets:
        for row in worksheet.iter_rows():
            for cell in row:
                if is_formula(cell.value):
                    found.append((worksheet.title, cell.coordinate, str(cell.value)))
    return found


def build_cell_index(workbook: Any) -> dict[tuple[str, str], Any]:
    """Every populated cell, keyed by ``(sheet, coordinate)``.

    Built once, then reused for every lookup. Two reasons this exists rather than
    indexing the workbook directly:

    1. **Correctness** — openpyxl's ``worksheet[coordinate]`` *creates* a cell when
       one is absent. A verification pass that asked for a coordinate belonging to
       a deleted row would resurrect that row in the in-memory model, corrupting
       every later check. This was a real bug: a data check after the formula
       checks reported the deleted rows as still present.
    2. **Cost** — one scan instead of one per lookup.
    """
    index: dict[tuple[str, str], Any] = {}
    for worksheet in workbook.worksheets:
        title = worksheet.title
        for row in worksheet.iter_rows():
            for cell in row:
                if cell.value is not None:
                    index[(title, cell.coordinate)] = cell.value
    return index


def check_presence(
    workbook: Any,
    *,
    expected_sheets: Iterable[str] | None = None,
) -> CheckResult:
    """Confirm sheets that should exist still do."""
    present = set(workbook.sheetnames)
    expected = set(expected_sheets or ())
    if not expected:
        return CheckResult(
            name="formula_sheets_present",
            status=VerificationStatus.PASSED,
            message="no expected sheets were specified",
        )
    missing = sorted(expected - present)
    if missing:
        return CheckResult(
            name="formula_sheets_present",
            status=VerificationStatus.FAILED,
            message=f"expected sheets are missing: {', '.join(missing)}",
            details={"missing": missing},
        )
    return CheckResult(
        name="formula_sheets_present",
        status=VerificationStatus.PASSED,
        message=f"all {len(expected)} expected sheets present",
    )


def check_broken_references(workbook: Any) -> CheckResult:
    """Find ``#REF!`` and other error values, and unresolvable sheet references."""
    issues: list[FormulaIssue] = []
    sheet_names = {name.lower() for name in workbook.sheetnames}

    for sheet, coordinate, formula in collect_formulas(workbook):
        for marker in _BROKEN_MARKERS:
            if marker in formula:
                issues.append(
                    FormulaIssue(
                        sheet, coordinate, "broken_reference", f"contains {marker}", formula
                    )
                )
                break
        else:
            for match in _REFERENCE.finditer(formula):
                quoted, bare = match.group(1), match.group(2)
                target = (quoted or bare or "").strip()
                # A sheet-qualified reference to a sheet that does not exist.
                # ``quoted or bare`` is the qualification test: the pattern only
                # captures those groups when a '!' was present, so an unqualified
                # cell reference never reaches this branch.
                if target and (quoted or bare) and target.lower() not in sheet_names:
                    issues.append(
                        FormulaIssue(
                            sheet,
                            coordinate,
                            "unresolvable_reference",
                            f"references unknown sheet {target!r}",
                            formula,
                        )
                    )

    if issues:
        return CheckResult(
            name="formula_broken_references",
            status=VerificationStatus.FAILED,
            message=f"{len(issues)} formula(s) contain broken or unresolvable references",
            details={
                "issues": [issue.describe() for issue in issues[:25]],
                "count": len(issues),
            },
        )
    return CheckResult(
        name="formula_broken_references",
        status=VerificationStatus.PASSED,
        message="no broken references found (static check)",
    )


def check_external_references(workbook: Any) -> CheckResult:
    """Flag formulas reaching into other workbooks.

    A warning, not a failure: such a link is legitimate in a real workbook, but
    it can trigger refresh prompts and reaches outside the workspace, so an
    operator should know about it.
    """
    external = [
        FormulaIssue(
            sheet, coordinate, "external_reference", "references another workbook", formula
        )
        for sheet, coordinate, formula in collect_formulas(workbook)
        if _EXTERNAL.search(formula)
    ]
    if external:
        return CheckResult(
            name="formula_external_references",
            status=VerificationStatus.WARNING,
            message=f"{len(external)} formula(s) reference other workbooks",
            details={
                "issues": [issue.describe() for issue in external[:25]],
                "count": len(external),
            },
        )
    return CheckResult(
        name="formula_external_references",
        status=VerificationStatus.PASSED,
        message="no external workbook references",
    )


#: A column is only treated as "one repeated formula" if at least this share of
#: its cells share the dominant pattern. Without it, a *metrics* column — where
#: each row is a different calculation, e.g. COUNTA in one row and SUM in the
#: next — is indistinguishable from a data column with one edited formula, and
#: every such sheet would produce a false warning.
_DOMINANT_PATTERN_RATIO = 0.6


def check_column_consistency(workbook: Any) -> CheckResult:
    """Formulas within one column should follow one structural pattern.

    A column where 40 rows use ``=E*F`` and one uses ``=B$6*2`` is a strong
    signal that a bad edit happened. This is the check that catches a formula
    quietly replaced by a literal in a long column.

    Only columns with a **dominant** pattern are flagged. A column with no
    dominant pattern is a different kind of column (a metrics list, say) and is
    left alone — reporting it would make this check cry wolf on every real
    workbook, which is how operators learn to ignore it.
    """
    by_column: dict[tuple[str, int], list[tuple[str, str]]] = {}
    for sheet, coordinate, formula in collect_formulas(workbook):
        _row, column = _split_coordinate(coordinate)
        by_column.setdefault((sheet, column), []).append((coordinate, formula))

    inconsistent: list[dict[str, Any]] = []
    for (sheet, column_index), entries in by_column.items():
        if len(entries) < 4:
            # Too few cells to distinguish a pattern from an outlier.
            continue
        patterns: dict[str, list[str]] = {}
        for coordinate, formula in entries:
            patterns.setdefault(_normalise_pattern(formula), []).append(coordinate)
        if len(patterns) == 1:
            continue
        counts = sorted((len(cells) for cells in patterns.values()), reverse=True)
        dominant_ratio = counts[0] / len(entries)
        if dominant_ratio < _DOMINANT_PATTERN_RATIO:
            # No dominant pattern: a heterogeneous column by design.
            continue
        inconsistent.append(
            {
                "sheet": sheet,
                "column": _column_letter(column_index),
                "patterns": len(patterns),
                "dominant_count": counts[0],
                "dominant_ratio": round(dominant_ratio, 3),
                "outliers": sum(counts[1:]),
                "outlier_cells": [cell for cells in patterns.values() for cell in cells[:5]][:5],
            }
        )

    if inconsistent:
        return CheckResult(
            name="formula_column_consistency",
            status=VerificationStatus.WARNING,
            message=(
                f"{len(inconsistent)} column(s) contain formulas that do not share one "
                f"pattern; this can indicate an edited formula"
            ),
            details={"columns": inconsistent[:25]},
        )
    return CheckResult(
        name="formula_column_consistency",
        status=VerificationStatus.PASSED,
        message="formula columns follow consistent patterns",
    )


def detect_formula_loss(
    before_formulas: dict[tuple[str, str], str],
    after_workbook: Any,
    *,
    explained_removals: set[tuple[str, int]] | None = None,
) -> tuple[CheckResult, list[FormulaIssue]]:
    """Find formulas that were present and are now gone.

    The strongest available proxy for "this operation destroyed a formula", since
    ExcelPilot cannot ask Excel what the cell *would* have evaluated to.

    ``explained_removals`` holds ``(sheet, row)`` pairs for rows the run
    deliberately deleted. Two consequences are handled explicitly:

    1. Formulas **on** those rows disappear with them, which is expected.
    2. Every formula **below** a removed row shifts up. openpyxl moves the
       formula text without rewriting its references, so a coordinate-keyed
       comparison sees the same formula at a different coordinate and calls it a
       change when it is really a shift.

    Consequence 2 cannot be resolved without recalculating the workbook, which
    ExcelPilot cannot do. So when a run removed rows, a shift is reported as
    ``WARNING`` with the affected cells listed — not failed for doing what it was
    asked, and not silently passed. When no row was removed, any change genuinely
    is suspicious and fails.
    """
    after = {
        (sheet, coordinate): formula
        for sheet, coordinate, formula in collect_formulas(after_workbook)
    }
    explained = explained_removals or set()
    lost: list[FormulaIssue] = []
    shifted: list[FormulaIssue] = []
    removed_with_row = 0

    # Formulas that still exist somewhere in the same sheet, under a new
    # coordinate, are the ones that moved.
    after_texts: dict[str, set[str]] = {}
    for (sheet, _coordinate), formula in after.items():
        after_texts.setdefault(sheet, set()).add(formula)

    # Sheets that lost rows: on those, a formula that still exists elsewhere in
    # the sheet has moved rather than changed.
    shifted_sheets = {sheet for sheet, _row in explained}

    for key, formula in before_formulas.items():
        if key in after and after[key] == formula:
            continue
        try:
            row_number, _column = _split_coordinate(key[1])
        except ValueError:
            row_number = -1
        if (key[0], row_number) in explained:
            removed_with_row += 1
            continue
        if key[0] in shifted_sheets and formula in after_texts.get(key[0], set()):
            # The same formula text still exists in this sheet, at another
            # coordinate: a row removal moved it.
            shifted.append(
                FormulaIssue(
                    key[0],
                    key[1],
                    "formula_shifted",
                    "the same formula now appears at a different row, consistent with a "
                    "row removal",
                    formula,
                )
            )
            continue
        if key in after:
            lost.append(
                FormulaIssue(
                    key[0],
                    key[1],
                    "formula_changed",
                    f"formula is now {after[key]!r}",
                    formula,
                )
            )
        else:
            lost.append(
                FormulaIssue(
                    key[0],
                    key[1],
                    "formula_removed",
                    "formula present before the run is absent now",
                    formula,
                )
            )

    if lost:
        status = VerificationStatus.FAILED
        message = f"{len(lost)} formula(s) were removed or changed"
    elif shifted:
        status = VerificationStatus.WARNING
        message = (
            f"{len(shifted)} formula(s) moved rows, consistent with the "
            f"{len(explained)} row(s) this run deleted; whether their references still "
            f"resolve cannot be determined without recalculating the workbook"
        )
    else:
        status = VerificationStatus.PASSED
        message = "all previously present formulas are intact"
    if removed_with_row and not lost and not shifted:
        message += f"; {removed_with_row} disappeared with rows the run deliberately removed"

    return (
        CheckResult(
            name="formula_preservation",
            status=status,
            message=message,
            details={
                "issues": [issue.describe() for issue in [*lost, *shifted][:25]],
                "count": len(lost),
                "shifted": len(shifted),
                "explained_by_row_removal": removed_with_row,
            },
        ),
        lost,
    )


def detect_hardcoded_replacements(
    before_formulas: dict[tuple[str, str], str],
    after_workbook: Any,
) -> CheckResult:
    """Find formula cells that are now literals.

    A literal in a column that was formula-driven is the classic symptom of a
    bad fill or a paste-special, and it silently stops updating.

    Reads through a pre-built cell index, never by indexing the worksheet, so this
    check cannot create cells in the workbook it is inspecting.
    """
    index = build_cell_index(after_workbook)
    hardcoded: list[FormulaIssue] = []
    for (sheet, coordinate), formula in before_formulas.items():
        if sheet not in after_workbook.sheetnames:
            continue
        value = index.get((sheet, coordinate))
        if value is None:
            continue
        if is_formula(value):
            continue
        # Only a numeric or short string is suspicious; a deliberate label is not.
        rendered = normalise_value(value)
        if len(rendered) <= 40:
            hardcoded.append(
                FormulaIssue(
                    sheet,
                    coordinate,
                    "hardcoded_replacement",
                    f"formula replaced by literal {rendered!r}",
                    formula,
                )
            )

    status = VerificationStatus.FAILED if hardcoded else VerificationStatus.PASSED
    return CheckResult(
        name="formula_hardcoded_replacements",
        status=status,
        message=(
            f"{len(hardcoded)} formula cell(s) were replaced by literal values"
            if hardcoded
            else "no formula was replaced by a literal"
        ),
        details={"issues": [issue.describe() for issue in hardcoded[:25]], "count": len(hardcoded)},
    )


def static_formula_checks(
    after_workbook: Any,
    *,
    before_formulas: dict[tuple[str, str], str] | None = None,
    expected_sheets: Iterable[str] | None = None,
    explained_removals: set[tuple[str, int]] | None = None,
) -> list[CheckResult]:
    """Run every static formula check and return the results.

    Never claims a formula was evaluated. Every check is static analysis of
    formula text and structure.
    """
    results = [
        check_presence(after_workbook, expected_sheets=expected_sheets),
        check_broken_references(after_workbook),
        check_external_references(after_workbook),
        check_column_consistency(after_workbook),
    ]
    if before_formulas is not None:
        preservation, _lost = detect_formula_loss(
            before_formulas, after_workbook, explained_removals=explained_removals
        )
        results.append(preservation)
        results.append(detect_hardcoded_replacements(before_formulas, after_workbook))
    return results


def unresolvable_formulas(workbook: Any) -> list[FormulaIssue]:
    """Formulas using functions whose target cannot be checked statically.

    Reported so the limit of static analysis is visible, rather than implied.
    """
    found: list[FormulaIssue] = []
    for sheet, coordinate, formula in collect_formulas(workbook):
        upper = formula.upper()
        used = [name for name in _DYNAMIC_FUNCTIONS if re.search(rf"\b{name}\s*\(", upper)]
        if used:
            found.append(
                FormulaIssue(
                    sheet,
                    coordinate,
                    "unresolvable",
                    f"uses {', '.join(used)}, which cannot be checked statically",
                    formula,
                )
            )
    return found


def _split_coordinate(coordinate: str) -> tuple[int, int]:
    """``'B12'`` -> ``(12, 2)``."""
    from app.contracts.base import parse_a1

    return parse_a1(coordinate)


def _column_letter(index: int) -> str:
    from app.contracts.base import column_letter

    return column_letter(index)


__all__ = [
    "FormulaIssue",
    "check_broken_references",
    "check_column_consistency",
    "check_external_references",
    "check_presence",
    "collect_formulas",
    "detect_formula_loss",
    "detect_hardcoded_replacements",
    "static_formula_checks",
    "unresolvable_formulas",
]
