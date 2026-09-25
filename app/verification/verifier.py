"""The verifier: assembles every check into a single verdict.

The rule this module exists to enforce: **a run cannot succeed because a file was
saved.** ``VerificationResult.passed`` is a required input to the run outcome, and
the CLI maps "written but not verified" to a distinct non-zero exit code, so the
distinction is impossible to miss in a script (ADR-0011).

Verification re-reads the output from disk and compares it against the pre-run
snapshot. It never consults the executor's in-memory state.
"""

from __future__ import annotations

from contextlib import ExitStack
from pathlib import Path

from app.contracts.config import AnomalyConfig
from app.contracts.enums import VerificationStatus
from app.contracts.errors import ExcelPilotError
from app.contracts.operations import Target
from app.contracts.verification import (
    Anomaly,
    CheckResult,
    ReconciliationReport,
    VerificationResult,
)
from app.verification import anomalies as anomaly_checks
from app.verification import formulas as formula_checks
from app.verification import structural as structural_checks
from app.verification.recalc import NullRecalculator, RecalcResult, Recalculator
from app.workbook import opened


class Verifier:
    """Runs every check family and produces one verdict.

    A recalculator is optional. When one is available and it actually completes,
    ``recalculated`` is True and the result is a genuinely stronger claim; when
    not, ``recalculated`` is False and the static limits are stated. The flag is
    never inferred from a library merely being installed.
    """

    def __init__(
        self,
        anomaly_config: AnomalyConfig | None = None,
        *,
        recalculator: Recalculator | None = None,
    ) -> None:
        self.anomaly_config = anomaly_config or AnomalyConfig()
        self.recalculator = recalculator if recalculator is not None else NullRecalculator()

    def verify(
        self,
        run_id: str,
        output_path: Path,
        *,
        before_path: Path | None = None,
        expected_sheets: list[str] | None = None,
        data_targets: list[Target] | None = None,
        planned_cells_changed: int = 0,
        actual_cells_changed: int = 0,
        reconciliation: ReconciliationReport | None = None,
        output_hash: str | None = None,
        removed_rows: list[str] | None = None,
    ) -> VerificationResult:
        """Verify an output workbook against the pre-run state and the plan."""
        structural: list[CheckResult] = []
        data: list[CheckResult] = []
        formula: list[CheckResult] = []
        found_anomalies: list[Anomaly] = []

        # The file itself: does it exist and parse? This is checked first and
        # separately, so "the artefact is not a readable workbook" is a
        # verification *result* rather than an exception escaping the verifier.
        structural.append(structural_checks.check_workbook_opens(output_path))

        if not output_path.exists():
            return VerificationResult(
                run_id=run_id,
                status=VerificationStatus.FAILED,
                structural=structural,
                data=data,
                formula=formula,
                reconciliation=reconciliation,
                anomalies=found_anomalies,
                output_hash=output_hash,
                notes=["the output file does not exist"],
            )

        recalc = self.recalculator.recalculate(output_path)

        try:
            return self._verify_open(
                run_id,
                output_path,
                structural=structural,
                data=data,
                formula=formula,
                reconciliation=reconciliation,
                before_path=before_path,
                expected_sheets=expected_sheets,
                data_targets=data_targets,
                planned_cells_changed=planned_cells_changed,
                actual_cells_changed=actual_cells_changed,
                output_hash=output_hash,
                removed_rows=removed_rows,
                recalc=recalc,
            )
        except ExcelPilotError as error:
            # The file passed the readability check but could not be analysed.
            # Report it as a failed verification rather than propagating.
            return VerificationResult(
                run_id=run_id,
                status=VerificationStatus.FAILED,
                structural=[
                    *structural,
                    CheckResult(
                        name="output_analysable",
                        status=VerificationStatus.FAILED,
                        message=f"the output could not be analysed: {error}",
                    ),
                ],
                data=data,
                formula=formula,
                reconciliation=reconciliation,
                anomalies=found_anomalies,
                output_hash=output_hash,
            )

    def _verify_open(
        self,
        run_id: str,
        output_path: Path,
        *,
        structural: list[CheckResult],
        data: list[CheckResult],
        formula: list[CheckResult],
        reconciliation: ReconciliationReport | None,
        before_path: Path | None,
        expected_sheets: list[str] | None,
        data_targets: list[Target] | None,
        planned_cells_changed: int,
        actual_cells_changed: int,
        output_hash: str | None,
        removed_rows: list[str] | None,
        recalc: RecalcResult,
    ) -> VerificationResult:
        """Run the checks that need the workbook open.

        Both workbooks are held open for the whole block. An earlier version read
        the "before" workbook inside its own ``with`` and then used the object
        afterwards, which silently produced a closed workbook and made the
        before/after comparison meaningless.
        """
        found_anomalies: list[Anomaly] = []
        notes: list[str] = []

        have_before = before_path is not None and before_path.exists()
        with ExitStack() as stack:
            after = stack.enter_context(opened(output_path))
            before = (
                stack.enter_context(opened(before_path))
                if have_before and before_path is not None
                else None
            )

            before_sheets = list(before.sheetnames) if before is not None else []
            before_formulas: dict[tuple[str, str], str] = {}
            if before is not None:
                before_formulas = {
                    (sheet, coordinate): formula
                    for sheet, coordinate, formula in formula_checks.collect_formulas(before)
                }

            structural.extend(
                structural_checks.check_structure(
                    before_sheets, after, expected_sheets=expected_sheets
                )
            )
            formula.extend(
                formula_checks.static_formula_checks(
                    after,
                    before_formulas=before_formulas or None,
                    expected_sheets=expected_sheets,
                    explained_removals=_parse_removed_rows(removed_rows),
                )
            )

            if before is not None:
                for target in data_targets or []:
                    data.extend(
                        structural_checks.check_data(
                            before, after, sheet=target.sheet, target=target
                        )
                    )

                found_anomalies.extend(
                    anomaly_checks.detect(
                        before,
                        after,
                        planned_cells_changed=planned_cells_changed,
                        actual_cells_changed=actual_cells_changed,
                        config=self.anomaly_config,
                    )
                )

            # Static analysis cannot resolve dynamic references; say so rather
            # than implying full coverage.
            unresolvable = formula_checks.unresolvable_formulas(after)
            if unresolvable:
                notes.append(
                    f"{len(unresolvable)} formula(s) use functions that cannot be checked "
                    f"statically (INDIRECT, OFFSET, VLOOKUP and similar); those references "
                    f"are unverified"
                )

        # Recalculation, when it actually happened, adds real evidence: a formula
        # that evaluates to an error value is a defect static analysis cannot see.
        if recalc.recalculated:
            formula.extend(_recalculated_checks(recalc))
        else:
            notes.extend(recalc.notes)
            if recalc.error:
                notes.append(f"recalculation was attempted and did not complete: {recalc.error}")

        status = _overall_status(structural, data, formula, reconciliation)
        if status is VerificationStatus.PASSED and found_anomalies:
            errors = [a for a in found_anomalies if a.severity.value == "error"]
            if errors:
                status = VerificationStatus.FAILED
            elif any(a.severity.value == "warning" for a in found_anomalies):
                status = VerificationStatus.WARNING

        return VerificationResult(
            run_id=run_id,
            status=status,
            structural=structural,
            data=data,
            formula=formula,
            reconciliation=reconciliation,
            anomalies=found_anomalies,
            recalculated=recalc.recalculated,
            static_formula_checks=not recalc.recalculated,
            output_hash=output_hash,
            notes=notes,
        )


#: Excel error values. A formula that evaluates to one of these is broken in a
#: way that static analysis cannot detect — only evaluation reveals it.
_ERROR_PREFIXES = ("#REF!", "#NAME?", "#VALUE!", "#DIV/0!", "#N/A", "#NULL!", "#NUM!")


def _recalculated_checks(recalc: RecalcResult) -> list[CheckResult]:
    """Checks that are only possible once formulas have been evaluated."""
    values = recalc.value_map()
    errored: list[str] = []
    for coordinate, value in values.items():
        if isinstance(value, str) and value.startswith(_ERROR_PREFIXES):
            errored.append(f"{coordinate} = {value}")
    return [
        CheckResult(
            name="formula_recalculated",
            status=VerificationStatus.PASSED,
            message=(
                f"{len(values):,} formula value(s) were evaluated with "
                f"{recalc.library}; this is a real recalculation, not a static check"
            ),
            details={"library": recalc.library, "values": len(values)},
        ),
        CheckResult(
            name="formula_evaluation_errors",
            status=VerificationStatus.FAILED if errored else VerificationStatus.PASSED,
            message=(
                f"{len(errored)} formula(s) evaluate to an Excel error"
                if errored
                else "no formula evaluates to an Excel error"
            ),
            details={"errored": errored[:25], "count": len(errored)},
        ),
    ]


def _parse_removed_rows(removed_rows: list[str] | None) -> set[tuple[str, int]]:
    """Turn ``['Sales!42', ...]`` into ``{('Sales', 42), ...}``.

    These are the rows a run deliberately deleted, so the formula-preservation
    check can tell an intentional removal from an accidental loss.
    """
    parsed: set[tuple[str, int]] = set()
    for entry in removed_rows or []:
        sheet, _, row = entry.rpartition("!")
        if not sheet or not row.isdigit():
            continue
        parsed.add((sheet, int(row)))
    return parsed


def _overall_status(
    structural: list[CheckResult],
    data: list[CheckResult],
    formula: list[CheckResult],
    reconciliation: ReconciliationReport | None,
) -> VerificationStatus:
    """Combine the check results into one verdict.

    Any ``FAILED`` check fails the run. Reconciliation failure fails the run when
    the config says so. Otherwise any warning downgrades to ``WARNING`` — which
    still is not ``PASSED``, so a run with warnings never claims clean success.
    """
    checks = [*structural, *data, *formula]
    if any(check.failed for check in checks):
        return VerificationStatus.FAILED
    if reconciliation is not None and reconciliation.failed:
        return VerificationStatus.FAILED
    if any(check.status is VerificationStatus.WARNING for check in checks):
        return VerificationStatus.WARNING
    if reconciliation is not None and reconciliation.warning_count:
        return VerificationStatus.WARNING
    return VerificationStatus.PASSED


def describe(result: VerificationResult) -> str:
    """Human-readable verification report.

    Always states that formulas were checked statically and never recalculated,
    because that is the single most important caveat in the system.
    """
    lines: list[str] = []
    lines.append(f"Verification: {result.status.value}")
    lines.append("")
    lines.append(f"  recalculated:      {result.recalculated}")
    lines.append(f"  static formula checks: {result.static_formula_checks}")
    for title, checks in (
        ("Structural", result.structural),
        ("Data", result.data),
        ("Formula", result.formula),
    ):
        if not checks:
            continue
        lines.append("")
        lines.append(f"{title}:")
        for check in checks:
            marker = {"passed": "OK  ", "failed": "FAIL", "warning": "WARN"}.get(
                check.status.value, "?"
            )
            lines.append(f"  [{marker}] {check.name}: {check.message}")

    if result.reconciliation is not None:
        lines.append("")
        lines.append("Reconciliation:")
        for entry in result.reconciliation.results:
            lines.append(
                f"  [{entry.status.value:7}] {entry.name}: expected {entry.expected} "
                f"actual {entry.actual} variance {entry.variance}"
            )
        lines.append(f"  recalculated: {result.reconciliation.recalculated}")

    if result.anomalies:
        lines.append("")
        lines.append(f"Anomalies ({len(result.anomalies)}):")
        for anomaly in result.anomalies:
            lines.append(
                f"  [{anomaly.severity.value:7}] [{anomaly.source.value:12}] {anomaly.message}"
            )

    if result.notes:
        lines.append("")
        lines.append("Notes:")
        for note in result.notes:
            lines.append(f"  - {note}")

    if not result.passed:
        lines.append("")
        lines.append(
            "This run did not pass verification. A file was written, but the outcome "
            "was not established; do not treat the output as correct."
        )
    return "\n".join(lines)


__all__ = ["Verifier", "describe"]
