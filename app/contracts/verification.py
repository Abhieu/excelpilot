"""Diff, manifest, verification, reconciliation, anomaly and audit contracts."""

from __future__ import annotations

from typing import Any

from pydantic import Field

from app.contracts.base import ContractModel
from app.contracts.enums import (
    Actor,
    AnomalyKind,
    AnomalySeverity,
    AnomalySource,
    ReconciliationStatus,
    RiskLevel,
    VerificationStatus,
)

# --------------------------------------------------------------------------
# Diff
# --------------------------------------------------------------------------


class CellChange(ContractModel):
    """One changed cell, with before and after."""

    sheet: str
    coordinate: str
    change: str = Field(description="added | removed | value | formula | formatting")
    before: str | None = None
    after: str | None = None

    def describe(self) -> str:
        return f"{self.sheet}!{self.coordinate}: {self.change} {self.before!r} -> {self.after!r}"


class SheetDiff(ContractModel):
    name: str
    state_before: str = "visible"
    state_after: str = "visible"
    rows_before: int = Field(default=0, ge=0)
    rows_after: int = Field(default=0, ge=0)
    columns_before: int = Field(default=0, ge=0)
    columns_after: int = Field(default=0, ge=0)
    cells_added: int = Field(default=0, ge=0)
    cells_removed: int = Field(default=0, ge=0)
    cells_changed: int = Field(default=0, ge=0)
    formulas_added: int = Field(default=0, ge=0)
    formulas_removed: int = Field(default=0, ge=0)
    formulas_changed: int = Field(default=0, ge=0)
    formatting_changed: int = Field(default=0, ge=0)

    @property
    def total_changes(self) -> int:
        return (
            self.cells_added
            + self.cells_removed
            + self.cells_changed
            + self.formulas_added
            + self.formulas_removed
            + self.formulas_changed
            + self.formatting_changed
        )


class WorkbookDiff(ContractModel):
    """Content-based diff (ADR-0009).

    Never derived from file bytes: a load/save cycle changes the bytes of an
    untouched workbook, so byte comparison would report a change on every run.
    """

    before_hash: str
    after_hash: str
    before_name: str
    after_name: str
    sheets_added: list[str] = Field(default_factory=list)
    sheets_removed: list[str] = Field(default_factory=list)
    sheets_renamed: list[dict[str, str]] = Field(default_factory=list)
    sheet_diffs: list[SheetDiff] = Field(default_factory=list)
    cell_changes: list[CellChange] = Field(default_factory=list)
    cell_changes_truncated: bool = Field(
        default=False,
        description="True when cell_changes was capped; per-sheet counts remain exact.",
    )
    tables_added: list[str] = Field(default_factory=list)
    tables_removed: list[str] = Field(default_factory=list)
    defined_names_added: list[str] = Field(default_factory=list)
    defined_names_removed: list[str] = Field(default_factory=list)
    structural_change: bool = False
    total_cell_changes: int = Field(default=0, ge=0)
    total_formula_changes: int = Field(default=0, ge=0)
    total_formatting_changes: int = Field(default=0, ge=0)
    notes: list[str] = Field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return (
            not self.sheets_added
            and not self.sheets_removed
            and not self.sheets_renamed
            and self.total_cell_changes == 0
            and self.total_formula_changes == 0
            and self.total_formatting_changes == 0
        )

    def summary(self) -> dict[str, Any]:
        return {
            "sheets_added": self.sheets_added,
            "sheets_removed": self.sheets_removed,
            "sheets_renamed": self.sheets_renamed,
            "cells_changed": self.total_cell_changes,
            "formulas_changed": self.total_formula_changes,
            "formatting_changed": self.total_formatting_changes,
            "structural_change": self.structural_change,
        }


class ChangeManifest(ContractModel):
    """Machine-readable record of what a run changed."""

    run_id: str
    workbook_name: str
    source_path: str
    source_hash: str
    output_path: str | None = None
    output_hash: str | None = None
    created_at: str
    diff: WorkbookDiff
    operations: list[dict[str, Any]] = Field(default_factory=list)
    verification: dict[str, Any] = Field(default_factory=dict)
    reconciliation: list[dict[str, Any]] = Field(default_factory=list)
    anomalies: list[dict[str, Any]] = Field(default_factory=list)
    approval: dict[str, Any] = Field(default_factory=dict)
    risk: RiskLevel = RiskLevel.LOW
    contract_version: str = "1"

    def to_text(self) -> str:
        """Human-readable report. Every number is the measured one from the diff."""
        lines: list[str] = []
        lines.append(f"Workbook: {self.workbook_name}")
        lines.append(f"Run:      {self.run_id}")
        lines.append("")
        diff = self.diff
        if diff.sheets_renamed:
            for rename in diff.sheets_renamed:
                lines.append(f"  renamed sheet {rename.get('from')} -> {rename.get('to')}")
        if diff.sheets_added:
            lines.append(f"  + {len(diff.sheets_added)} sheet(s): {', '.join(diff.sheets_added)}")
        if diff.sheets_removed:
            lines.append(
                f"  - {len(diff.sheets_removed)} sheet(s): {', '.join(diff.sheets_removed)}"
            )

        modified = [s.name for s in diff.sheet_diffs if s.total_changes > 0]
        if modified:
            lines.append("")
            lines.append("Sheets modified:")
            for name in modified:
                lines.append(f"  {name}")
            lines.append("")
            lines.append("Changes:")
            lines.append(f"  ~ {diff.total_cell_changes:,} cell value change(s)")
            lines.append(f"  ~ {diff.total_formula_changes:,} formula change(s)")
            lines.append(f"  ~ {diff.total_formatting_changes:,} formatting change(s)")
        else:
            lines.append("")
            lines.append("Changes: none")

        if diff.structural_change:
            lines.append("")
            lines.append("  ! structural change detected")

        if self.verification:
            lines.append("")
            lines.append("Validation:")
            for name, value in sorted(self.verification.items()):
                lines.append(f"  {str(value):8} {name}")

        if self.anomalies:
            lines.append("")
            lines.append(f"Anomalies ({len(self.anomalies)}):")
            for anomaly in self.anomalies:
                lines.append(
                    f"  [{anomaly.get('severity', '?')}] {anomaly.get('kind', '?')}: "
                    f"{anomaly.get('message', '')}"
                )

        approval = self.approval or {}
        if approval.get("required"):
            lines.append("")
            lines.append("Approval: required")
        return "\n".join(lines)


# --------------------------------------------------------------------------
# Reconciliation
# --------------------------------------------------------------------------


class ReconciliationResult(ContractModel):
    """Outcome of one reconciliation check. Values are computed, not judged."""

    name: str
    status: ReconciliationStatus
    expected: float | None = None
    actual: float | None = None
    variance: float | None = None
    tolerance: float = Field(default=0.0, ge=0)
    explanation: str
    derived_from_formula_cells: bool = Field(
        default=False,
        description=(
            "True when the aggregate was computed over formula cells whose values "
            "could not be recalculated. Reported, never presented as verified."
        ),
    )

    @property
    def passed(self) -> bool:
        return self.status is ReconciliationStatus.PASSED


class ReconciliationReport(ContractModel):
    run_id: str
    results: list[ReconciliationResult] = Field(default_factory=list)
    passed_count: int = Field(default=0, ge=0)
    failed_count: int = Field(default=0, ge=0)
    warning_count: int = Field(default=0, ge=0)
    recalculated: bool = Field(
        default=False,
        description=(
            "Always False. ExcelPilot cannot recalculate Excel formulas; see "
            "docs/limitations.md. Present so no consumer can assume otherwise."
        ),
    )

    @property
    def failed(self) -> bool:
        return self.failed_count > 0

    def summary(self) -> str:
        return (
            f"{self.passed_count} passed, {self.failed_count} failed, {self.warning_count} warnings"
        )


# --------------------------------------------------------------------------
# Anomalies
# --------------------------------------------------------------------------


class Anomaly(ContractModel):
    """A detected anomaly, with explicit provenance.

    ``source`` records whether this came from a deterministic check, a model, JEV,
    or a human. A probabilistic finding is never presented as a verified fact.
    """

    kind: AnomalyKind
    severity: AnomalySeverity
    source: AnomalySource
    message: str
    sheet: str | None = None
    coordinate: str | None = None
    evidence: dict[str, Any] = Field(default_factory=dict)
    detector: str = Field(description="Name of the check that produced this")

    def to_summary(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "severity": self.severity.value,
            "source": self.source.value,
            "message": self.message,
            "sheet": self.sheet,
            "coordinate": self.coordinate,
            "detector": self.detector,
        }


# --------------------------------------------------------------------------
# Verification
# --------------------------------------------------------------------------


class CheckResult(ContractModel):
    name: str
    status: VerificationStatus
    message: str = ""
    details: dict[str, Any] = Field(default_factory=dict)

    @property
    def failed(self) -> bool:
        return self.status is VerificationStatus.FAILED


class VerificationResult(ContractModel):
    """The complete verification verdict for a run.

    ``recalculated`` is always ``False``. ExcelPilot cannot evaluate Excel
    formulas (ADR-0001/0011); formula checks are static, and this field exists so
    no consumer can mistake one for the other.
    """

    run_id: str
    status: VerificationStatus = VerificationStatus.PASSED
    structural: list[CheckResult] = Field(default_factory=list)
    data: list[CheckResult] = Field(default_factory=list)
    formula: list[CheckResult] = Field(default_factory=list)
    reconciliation: ReconciliationReport | None = None
    anomalies: list[Anomaly] = Field(default_factory=list)
    recalculated: bool = False
    static_formula_checks: bool = True
    output_hash: str | None = None
    notes: list[str] = Field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.status is VerificationStatus.PASSED

    @property
    def all_checks(self) -> list[CheckResult]:
        return [*self.structural, *self.data, *self.formula]

    @property
    def failed_checks(self) -> list[CheckResult]:
        return [check for check in self.all_checks if check.failed]


# --------------------------------------------------------------------------
# Audit
# --------------------------------------------------------------------------


class AuditEvent(ContractModel):
    """One immutable line of the audit log.

    Payloads are redacted *before* serialisation, so secrets never reach disk.
    """

    seq: int = Field(ge=0)
    run_id: str
    timestamp: str
    actor: Actor
    event_type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    contract_version: str = "1"


__all__ = [
    "Anomaly",
    "AuditEvent",
    "CellChange",
    "ChangeManifest",
    "CheckResult",
    "ReconciliationReport",
    "ReconciliationResult",
    "SheetDiff",
    "VerificationResult",
    "WorkbookDiff",
]
