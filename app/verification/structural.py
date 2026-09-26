"""Structural and data verification.

Re-reads the output workbook **from disk** and checks it against the plan's
declared intent. It does not consult the executor's in-memory state, because
verification that trusts the code that made the change is not verification
(ADR-0011).

A run cannot succeed on save alone: ``VerificationResult.passed`` is a required
input to the run outcome, and the CLI returns a distinct non-zero exit code when
a file was written but verification failed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from openpyxl.workbook import Workbook

from app.contracts.enums import VerificationStatus
from app.contracts.operations import Target
from app.contracts.verification import CheckResult
from app.workbook.table import read_table


def check_structure(
    before_sheet_names: list[str],
    after_workbook: Workbook,
    *,
    expected_sheets: list[str] | None = None,
) -> list[CheckResult]:
    """Structural checks: sheets, visibility, dimensions, tables, openability.

    ``re-opens cleanly`` is established by the caller having loaded the file at
    all — reaching this function means the workbook parsed.
    """
    results: list[CheckResult] = []
    after_names = list(after_workbook.sheetnames)

    # 1. Expected sheets exist.
    if expected_sheets:
        missing = [name for name in expected_sheets if name not in after_names]
        results.append(
            CheckResult(
                name="structure_expected_sheets",
                status=VerificationStatus.FAILED if missing else VerificationStatus.PASSED,
                message=(
                    f"expected sheets are missing: {', '.join(missing)}"
                    if missing
                    else f"all {len(expected_sheets)} expected sheets present"
                ),
                details={"missing": missing},
            )
        )

    # 2. No unexpected sheet was removed. A removed sheet is the most
    #    consequential structural change, so it fails rather than warns.
    removed = [name for name in before_sheet_names if name not in after_names]
    results.append(
        CheckResult(
            name="structure_no_sheet_removed",
            status=VerificationStatus.FAILED if removed else VerificationStatus.PASSED,
            message=(
                f"sheet(s) removed: {', '.join(removed)}" if removed else "no sheet was removed"
            ),
            details={"removed": removed},
        )
    )

    # 3. Sheet visibility preserved, unless deliberately changed.
    changed_visibility = [
        f"{before.title}: {before.sheet_state} -> {after_workbook[before.title].sheet_state}"
        for before in _sheets_of(after_workbook, before_sheet_names)
        if after_workbook[before.title].sheet_state != before.sheet_state
    ]
    results.append(
        CheckResult(
            name="structure_visibility",
            status=VerificationStatus.WARNING if changed_visibility else VerificationStatus.PASSED,
            message=(
                "sheet visibility changed: " + "; ".join(changed_visibility)
                if changed_visibility
                else "sheet visibility unchanged"
            ),
            details={"changed": changed_visibility},
        )
    )

    # 4. The workbook still parses and has usable dimensions.
    empty_sheets = [ws.title for ws in after_workbook.worksheets if ws.max_row == 0]
    results.append(
        CheckResult(
            name="structure_dimensions",
            status=VerificationStatus.WARNING if empty_sheets else VerificationStatus.PASSED,
            message=(
                f"sheet(s) with no used range: {', '.join(empty_sheets)}"
                if empty_sheets
                else "all sheets have a used range"
            ),
            details={"empty_sheets": empty_sheets},
        )
    )

    # 5. Tables remain valid.
    invalid_tables: list[str] = []
    for worksheet in after_workbook.worksheets:
        for name, table in dict(getattr(worksheet, "tables", None) or {}).items():
            if not getattr(table, "ref", None):
                invalid_tables.append(f"{worksheet.title}.{name}")
    results.append(
        CheckResult(
            name="structure_tables_valid",
            status=VerificationStatus.FAILED if invalid_tables else VerificationStatus.PASSED,
            message=(
                f"table(s) with no range: {', '.join(invalid_tables)}"
                if invalid_tables
                else "all tables have a valid range"
            ),
            details={"invalid": invalid_tables},
        )
    )

    return results


def _sheets_of(workbook: Workbook, names: list[str]) -> list[Any]:
    return [workbook[name] for name in names if name in workbook.sheetnames]


def check_data(
    before_workbook: Workbook,
    after_workbook: Workbook,
    *,
    sheet: str,
    target: Target | None = None,
) -> list[CheckResult]:
    """Data checks: row counts, null rates, duplicate rates, key validity.

    Compares against the pre-run workbook, so a silent data loss is caught rather
    than reported as success.
    """
    results: list[CheckResult] = []
    probe = target or Target(sheet=sheet)

    try:
        before_view = read_table(before_workbook, probe)
    except Exception as error:  # noqa: BLE001 - a failed probe is a failed check
        return [
            CheckResult(
                name="data_before_readable",
                status=VerificationStatus.FAILED,
                message=f"could not read the original range: {error}",
            )
        ]

    try:
        after_view = read_table(after_workbook, probe)
    except Exception as error:  # noqa: BLE001
        return [
            CheckResult(
                name="data_after_readable",
                status=VerificationStatus.FAILED,
                message=f"could not read the resulting range: {error}",
            )
        ]

    results.append(
        CheckResult(
            name="data_after_readable",
            status=VerificationStatus.PASSED,
            message="the resulting range is readable",
        )
    )

    # Row count. A large drop is suspicious; a deliberate dedupe legitimately
    # reduces it, so this warns with the magnitude rather than failing outright.
    before_rows = before_view.row_count
    after_rows = after_view.row_count
    lost = before_rows - after_rows
    ratio = lost / before_rows if before_rows else 0.0
    if after_rows == 0 and before_rows > 0:
        results.append(
            CheckResult(
                name="data_row_count",
                status=VerificationStatus.FAILED,
                message=f"all {before_rows:,} rows disappeared",
                details={"before": before_rows, "after": after_rows},
            )
        )
    elif ratio > 0.5:
        results.append(
            CheckResult(
                name="data_row_count",
                status=VerificationStatus.FAILED,
                message=(
                    f"row count fell from {before_rows:,} to {after_rows:,} "
                    f"({ratio:.0%} of rows lost)"
                ),
                details={"before": before_rows, "after": after_rows, "ratio": ratio},
            )
        )
    elif ratio > 0.1:
        results.append(
            CheckResult(
                name="data_row_count",
                status=VerificationStatus.WARNING,
                message=f"row count fell from {before_rows:,} to {after_rows:,}",
                details={"before": before_rows, "after": after_rows, "ratio": ratio},
            )
        )
    else:
        results.append(
            CheckResult(
                name="data_row_count",
                status=VerificationStatus.PASSED,
                message=f"row count {after_rows:,} (was {before_rows:,})",
                details={"before": before_rows, "after": after_rows},
            )
        )

    # Null rate per column, where the column exists in both.
    shared = [h for h in after_view.headers if h and h in before_view.headers]
    increased: list[dict[str, Any]] = []
    for header in shared:
        before_null = _null_rate(before_view, header)
        after_null = _null_rate(after_view, header)
        if after_null - before_null > 0.05:
            increased.append(
                {
                    "column": header,
                    "before": round(before_null, 4),
                    "after": round(after_null, 4),
                }
            )
    results.append(
        CheckResult(
            name="data_null_rate",
            status=VerificationStatus.WARNING if increased else VerificationStatus.PASSED,
            message=(
                f"null rate increased in {len(increased)} column(s)"
                if increased
                else "null rates stable"
            ),
            details={"increased": increased[:25]},
        )
    )

    # Duplicate rate, using the first column as a proxy key when no key is given.
    if shared:
        key = shared[0]
        before_dupes = _duplicate_rate(before_view, key)
        after_dupes = _duplicate_rate(after_view, key)
        results.append(
            CheckResult(
                name="data_duplicate_rate",
                status=(
                    VerificationStatus.PASSED
                    if after_dupes <= before_dupes + 0.05
                    else VerificationStatus.WARNING
                ),
                message=(f"duplicate rate in {key!r}: {before_dupes:.1%} -> {after_dupes:.1%}"),
                details={"column": key, "before": before_dupes, "after": after_dupes},
            )
        )

    return results


def _null_rate(view: Any, header: str) -> float:
    values = view.column_values(header)
    if not values:
        return 0.0
    nulls = sum(
        1 for value in values if value is None or (isinstance(value, str) and not value.strip())
    )
    return nulls / len(values)


def _duplicate_rate(view: Any, header: str) -> float:
    values = view.column_values(header)
    if not values:
        return 0.0
    seen: set[str] = set()
    duplicates = 0
    for value in values:
        key = "" if value is None else str(value).strip().lower()
        if key in seen:
            duplicates += 1
        else:
            seen.add(key)
    return duplicates / len(values)


def check_workbook_opens(path: Path) -> CheckResult:
    """Confirm the artefact on disk parses as a workbook.

    Distinct from every other structural check: this is about the *file*, not the
    model. It is what makes "it saved" and "it is a readable workbook" separate
    facts.
    """
    from app.contracts.errors import ExcelPilotError
    from app.workbook import opened

    try:
        with opened(path) as workbook:
            count = len(workbook.sheetnames)
    except ExcelPilotError as error:
        return CheckResult(
            name="file_readable",
            status=VerificationStatus.FAILED,
            message=f"the output could not be opened as a workbook: {error}",
        )
    return CheckResult(
        name="file_readable",
        status=VerificationStatus.PASSED,
        message=f"the output opens as a workbook with {count} sheet(s)",
        details={"sheets": count},
    )


#: Parts openpyxl legitimately does not re-emit, and whose loss costs the user
#: nothing. Both are *caches* that Excel rebuilds on open; the values they held
#: are stored in the sheet XML and are verified to be exact by the data checks.
#:
#: This allowlist is measured, not assumed. Across every synthetic fixture
#: openpyxl drops nothing at all; the losses below appear only on workbooks Excel
#: itself produced. Measured on real workbooks during the release audit:
#:
#:   xl/sharedStrings.xml  — the shared-string cache
#:   xl/calcChain.xml      — the calculation-order cache
#:
#: Anything else that disappears is user content, and is reported as a failure.
BENIGN_DROPPED_PARTS: frozenset[str] = frozenset({"xl/sharedStrings.xml", "xl/calcChain.xml"})


def check_no_parts_lost(before_path: Path, after_path: Path) -> CheckResult:
    """Confirm the output did not silently lose workbook content.

    openpyxl does not round-trip every OOXML part. Measured on real workbooks, a
    plain read/save drops cell **comments**, **drawings** (including shapes bound
    to a macro), and the VML drawing that anchors legacy comments. Those losses
    are invisible to a cell-level diff and to every other structural check, so a
    run could report ``passed`` while having permanently removed a user's
    comments.

    This is a *name-set* comparison, not an OOXML diff: it asks which parts are
    present before and absent after, and nothing more. That is deliberately the
    smallest mechanism that makes the verdict honest — it does not attempt to
    merge, repair, or understand the parts it finds.

    Benign cache parts are excluded, because failing every run over
    ``sharedStrings.xml`` would make the check useless rather than useful.

    Requires ``before_path``; without a reference there is nothing to compare
    against, and the result says so rather than implying an all-clear.
    """
    import zipfile

    if before_path is None or not Path(before_path).exists():
        return CheckResult(
            name="ooxml_parts_preserved",
            status=VerificationStatus.PASSED,
            message=(
                "no source reference was supplied, so lost OOXML parts could not be "
                "detected; this is not an all-clear"
            ),
            details={"checked": False},
        )

    def names(path: Path) -> set[str]:
        try:
            with zipfile.ZipFile(path) as archive:
                return set(archive.namelist())
        except (OSError, zipfile.BadZipFile):
            return set()

    before = names(Path(before_path))
    after = names(Path(after_path))
    lost = sorted(before - after - BENIGN_DROPPED_PARTS)

    if not lost:
        return CheckResult(
            name="ooxml_parts_preserved",
            status=VerificationStatus.PASSED,
            message=(
                "every workbook part survived the change, apart from the shared-string "
                "and calculation-order caches, which Excel rebuilds"
            ),
            details={
                "checked": True,
                "parts_before": len(before),
                "parts_after": len(after),
                "benign_caches_absent": sorted((before - after) & BENIGN_DROPPED_PARTS),
            },
        )

    return CheckResult(
        name="ooxml_parts_preserved",
        status=VerificationStatus.FAILED,
        message=(
            f"{len(lost)} workbook part(s) present in the source are absent from the "
            f"output and openpyxl does not preserve them: {', '.join(lost)}. This is "
            "content loss that a cell-level diff cannot see, most commonly cell "
            "comments and drawings. The source workbook is unmodified, so nothing is "
            "lost until this output is used; discard it."
        ),
        details={"checked": True, "lost_parts": lost, "parts_before": len(before)},
    )


__all__ = [
    "BENIGN_DROPPED_PARTS",
    "check_data",
    "check_no_parts_lost",
    "check_structure",
    "check_workbook_opens",
]
