"""The run state machine — the sole orchestrator.

Composes the layers in the required order and is the **only** place that knows
about the whole pipeline:

    inspect -> understand -> plan -> decide (JEV) -> policy
            -> approval -> execute -> verify -> manifest -> audit

Everything below it is independently testable; everything above it is a thin
adapter. The CLI and the dashboard both call this, so neither can bypass policy
(ADR-0008).

Two invariants this module exists to enforce:

1. **JEV cannot widen authority.** Its result is passed to policy as an escalation
   input only, and the combination is the asymmetric OR documented in ADR-0005.
2. **Saved is not verified.** The run outcome requires ``verification.passed``;
   a file written without verification is a *failed* run with exit code 5.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from app.audit import AuditLog, EventType, FileRunStore
from app.audit.log import AuditLog as _AuditLog  # noqa: F401 - typing clarity
from app.contracts.base import UntrustedText, new_run_id, utc_now
from app.contracts.config import ExcelPilotConfig
from app.contracts.enums import (
    Actor,
    ApprovalStatus,
    PolicyOutcome,
    RunOutcome,
    RunState,
)
from app.contracts.errors import (
    ExcelPilotError,
    PaidCallBlocked,
)
from app.contracts.operations import Target
from app.contracts.pipeline import (
    ApprovalRequest,
    ApprovalResult,
    DecisionContext,
    ExecutionPlan,
    ExecutionState,
    JevDecisionSet,
    PolicyDecision,
    PolicyRequest,
    RunRecord,
    TaskUnderstanding,
)
from app.contracts.verification import (
    ChangeManifest,
    ReconciliationReport,
    VerificationResult,
)
from app.decisions import HttpJevAdapter, MockJevAdapter
from app.decisions.jev import JevAdapter
from app.diff import diff_workbooks
from app.executor import ExecutionError, Executor, preview_plan, verification_plan_for
from app.planner import DeterministicPlanner, Planner
from app.policy import PolicyEngine
from app.safety import versioned_output
from app.verification import Verifier, recalc
from app.verification.recalc import FormulaRecalculator, NullRecalculator
from app.workbook import file_sha256, inspect_workbook, opened, save_atomic


class Stage(str, Enum):
    """Pipeline stages, in order. Used in the audit trail and error messages."""

    INSPECT = "inspect"
    UNDERSTAND = "understand"
    PLAN = "plan"
    DECIDE = "decide"
    POLICY = "policy"
    APPROVAL = "approval"
    PREVIEW = "preview"
    EXECUTE = "execute"
    VERIFY = "verify"
    MANIFEST = "manifest"
    DONE = "done"


#: Legal state transitions. Enforced so a bug cannot make a run claim to have
#: executed before it was approved.
_TRANSITIONS: dict[RunState, frozenset[RunState]] = {
    RunState.CREATED: frozenset({RunState.INSPECTING, RunState.FAILED}),
    RunState.INSPECTING: frozenset({RunState.UNDERSTANDING, RunState.FAILED}),
    RunState.UNDERSTANDING: frozenset({RunState.PLANNING, RunState.FAILED}),
    RunState.PLANNING: frozenset({RunState.DECIDING, RunState.FAILED}),
    RunState.DECIDING: frozenset({RunState.POLICY_CHECK, RunState.FAILED}),
    RunState.POLICY_CHECK: frozenset(
        {RunState.AWAITING_APPROVAL, RunState.EXECUTING, RunState.FAILED, RunState.REJECTED}
    ),
    RunState.AWAITING_APPROVAL: frozenset({RunState.EXECUTING, RunState.REJECTED, RunState.FAILED}),
    RunState.EXECUTING: frozenset({RunState.VERIFYING, RunState.FAILED}),
    RunState.VERIFYING: frozenset({RunState.COMPLETED, RunState.FAILED}),
    RunState.COMPLETED: frozenset(),
    RunState.FAILED: frozenset(),
    RunState.REJECTED: frozenset(),
    RunState.ROLLED_BACK: frozenset(),
}


class RunStateError(ExcelPilotError):
    """An illegal state transition was attempted."""

    code = "invalid_transition"


@dataclass(slots=True)
class RunResult:
    """Everything a caller needs to know about a completed run."""

    run_id: str
    state: RunState
    outcome: RunOutcome
    record: RunRecord
    inspection: Any = None
    understanding: TaskUnderstanding | None = None
    plan: ExecutionPlan | None = None
    jev: JevDecisionSet | None = None
    policy: PolicyDecision | None = None
    approval_request: ApprovalRequest | None = None
    approval: ApprovalResult | None = None
    execution: ExecutionState | None = None
    verification: VerificationResult | None = None
    manifest: ChangeManifest | None = None
    report: str = ""
    error: str | None = None
    duration_seconds: float = 0.0
    warnings: list[str] = field(default_factory=list)

    @property
    def succeeded(self) -> bool:
        return self.outcome is RunOutcome.SUCCEEDED

    @property
    def output_path(self) -> str | None:
        return self.record.output_path

    def to_json_dict(self) -> dict[str, Any]:
        """Machine-readable summary for ``--json``."""
        return {
            "run_id": self.run_id,
            "state": self.state.value,
            "outcome": self.outcome.value,
            "duration_seconds": round(self.duration_seconds, 3),
            "source": {
                "path": self.record.source_path,
                "name": self.record.source_name,
                "hash": self.record.source_hash,
            },
            "output": {
                "path": self.record.output_path,
                "hash": self.record.output_hash,
            },
            "task": {
                "raw": self.record.raw_task.to_redacted(200),
                "intent": self.record.intent_summary,
            },
            "planner_source": self.record.planner_source,
            "jev": {
                "called": self.record.jev_called,
                "provider": self.record.jev_provider.value,
                "escalated": bool(self.policy and self.policy.jev_escalated),
                "error": self.jev.error if self.jev else None,
            },
            "policy": {
                "outcome": self.record.policy_outcome.value if self.record.policy_outcome else None,
                "rule_ids": self.record.policy_rule_ids,
                "reasons": self.policy.reasons if self.policy else [],
            },
            "approval": {
                "status": self.record.approval_status.value,
                "required": bool(self.approval_request),
            },
            "execution": self.execution.totals() if self.execution else None,
            "verification": (
                {
                    "status": self.verification.status.value,
                    "passed": self.verification.passed,
                    "recalculated": self.verification.recalculated,
                    "static_formula_checks": self.verification.static_formula_checks,
                    "failed_checks": [c.name for c in self.verification.failed_checks],
                    "anomalies": [a.to_summary() for a in self.verification.anomalies],
                }
                if self.verification
                else None
            ),
            "warnings": self.warnings,
            "error": self.error,
        }


class RunOrchestrator:
    """Executes a full run: the pipeline, end to end."""

    def __init__(
        self,
        config: ExcelPilotConfig | None = None,
        *,
        store: FileRunStore | None = None,
        planner: Planner | None = None,
        jev: JevAdapter | None = None,
    ) -> None:
        self.config = config or ExcelPilotConfig()
        self.store = store or FileRunStore(self.config)
        self.policy = PolicyEngine(self.config)
        self.executor = Executor(self.config)
        # Recalculation is added evidence, never a requirement: with the optional
        # library absent, verification falls back to static checks and says so.
        recalculator = (
            FormulaRecalculator(
                timeout_seconds=self.config.verification.recalculation_timeout_seconds
            )
            if self.config.verification.enable_recalculation
            else NullRecalculator()
        )
        self.verifier = Verifier(self.config.anomaly, recalculator=recalculator)
        # The deterministic planner is the default; no credential required.
        self.planner = planner or DeterministicPlanner()
        self.jev = jev or self._default_jev()

    def _default_jev(self) -> JevAdapter:
        """Choose a JEV adapter from the configuration.

        Mock when JEV is disabled entirely (so tests and offline runs still
        exercise the decisioning path), otherwise the real HTTP adapter.
        """
        if not self.config.jev.enabled or self.config.jev.provider.value == "mock":
            return MockJevAdapter()
        return HttpJevAdapter(self.config.jev, allow_paid_calls=self.config.allow_paid_calls)

    # -- inspection --------------------------------------------------------

    def inspect(self, path: Path) -> Any:
        """Inspect a workbook without creating a run.

        Read-only and free; the ``inspect`` CLI command uses this.
        """
        return inspect_workbook(path, limits=self.config.limits)

    # -- planning ----------------------------------------------------------

    def plan(
        self,
        path: Path,
        task: UntrustedText,
        *,
        run_id: str | None = None,
        jev: JevAdapter | None = None,
    ) -> tuple[ExecutionPlan, Any, JevDecisionSet, PolicyDecision]:
        """Inspect, plan, decide, and evaluate policy — without executing.

        This is what ``plan`` and ``run --dry-run`` use. Policy is always
        non-optional here: an advisory decision set and a verdict are both
        always produced, so a caller never has to handle "no opinion".
        """
        inspection = self.inspect(path)
        _understanding, plan = self._build_plan(task, inspection, run_id or new_run_id())
        jev_result = self._decide(plan, inspection, jev)
        self._last_jev = jev_result
        policy = self._evaluate_policy(plan, inspection)
        return plan, inspection, jev_result, policy

    def _build_plan(
        self, task: UntrustedText, inspection: Any, run_id: str
    ) -> tuple[TaskUnderstanding, ExecutionPlan]:
        plan = self.planner.plan(task, inspection)
        if plan.run_id in {"", "pending"}:
            plan = plan.model_copy(update={"run_id": run_id})
        return plan.understanding, plan

    def _decide(
        self, plan: ExecutionPlan, inspection: Any, override: JevAdapter | None = None
    ) -> JevDecisionSet:
        """Ask JEV, recording the fact that it was asked and what it said.

        Always returns a decision set, even when JEV is unavailable: the run then
        carries ``jev_called: false`` with the reason, so "JEV said yes" and "JEV
        was never asked" can never look the same in an audit trail.
        """
        adapter = override if override is not None else self.jev
        try:
            result = adapter.decide(_decision_context(plan, inspection))
        except PaidCallBlocked as error:
            # A paid call is blocked: record it and carry on. Policy still runs in
            # full, so a blocked call cannot authorise anything.
            return JevDecisionSet(
                jev_called=False,
                error=str(error),
                min_probability=self.config.jev.min_probability,
                min_margin=self.config.jev.min_margin,
            )
        return result

    def _evaluate_policy(self, plan: ExecutionPlan, inspection: Any) -> PolicyDecision:
        """Evaluate policy for the whole plan.

        JEV is passed as an escalation input only, and the combination is the
        asymmetric OR of ADR-0005.
        """
        preview = self._preview_operations(plan, inspection)
        request = PolicyRequest(
            operations=list(plan.operations),
            sheet_names=list(inspection.sheet_names),
            total_rows=inspection.total_rows,
            total_cells=inspection.total_non_empty_cells,
            sensitivity_level=inspection.sensitivity.level,
            has_hidden_sheets=inspection.hidden_sheet_count > 0,
            has_vba=inspection.metadata.has_vba,
            structural_change=preview.structural_change,
            ambiguity_signals=list(plan.understanding.missing_information),
            cells_affected=preview.cells_to_change,
            formulas_removed=preview.formulas_to_remove,
            config_fingerprint=self.config.fingerprint(),
        )
        jev = self._last_jev
        return self.policy.evaluate(request, jev=jev)

    _last_jev: JevDecisionSet | None = None

    def _preview_operations(self, plan: ExecutionPlan, inspection: Any) -> Any:
        """Measure the plan's effect without touching the source."""
        output = versioned_output(Path(inspection.path), plan.run_id)
        return preview_plan(
            list(plan.operations),
            inspection,
            run_id=plan.run_id,
            output_path=str(output),
            limits=self.config.limits,
        )

    # -- approval ----------------------------------------------------------

    def build_approval_request(
        self,
        plan: ExecutionPlan,
        inspection: Any,
        preview: Any,
        policy: PolicyDecision,
        jev: JevDecisionSet | None,
        run_id: str,
    ) -> ApprovalRequest:
        """Assemble everything an approver needs to decide."""
        risk = self.policy.risk_level(
            PolicyRequest(
                operations=list(plan.operations),
                cells_affected=preview.cells_to_change,
                formulas_removed=preview.formulas_to_remove,
                structural_change=preview.structural_change,
                sensitivity_level=inspection.sensitivity.level,
                has_vba=inspection.metadata.has_vba,
                has_hidden_sheets=inspection.hidden_sheet_count > 0,
                ambiguity_signals=list(plan.understanding.missing_information),
            )
        )
        ranges = sorted({op.target.describe() for op in plan.operations if hasattr(op, "target")})
        return ApprovalRequest(
            run_id=run_id,
            workbook_name=inspection.file_name,
            source_path=inspection.path,
            proposed_output_path=str(versioned_output(Path(inspection.path), run_id)),
            intent_summary=plan.understanding.intent_summary,
            operation_kinds=plan.operation_kinds,
            sheets_affected=preview.sheets_affected_names,
            ranges_affected=ranges,
            cells_to_change=preview.cells_to_change,
            formulas_to_add=preview.formulas_to_add,
            formulas_to_remove=preview.formulas_to_remove,
            records_removed=preview.records_to_remove,
            structural_change=preview.structural_change,
            risk=risk,
            risk_reasons=policy.reasons[:10],
            jev_summary=_jev_summary(jev),
            jev_escalated=bool(jev and jev.escalates),
            policy_explanation=policy.explain(),
            policy_rule_ids=list(policy.rule_ids),
            ambiguity=list(plan.understanding.missing_information),
            verification_plan=verification_plan_for(list(plan.operations)),
            warnings=list(preview.warnings),
            created_at=utc_now().isoformat(),
        )

    # -- the run -----------------------------------------------------------

    def run(
        self,
        path: Path,
        task: UntrustedText,
        *,
        dry_run: bool = False,
        output_path: Path | None = None,
        run_id: str | None = None,
        approve: bool = False,
        reject: bool = False,
        jev: JevAdapter | None = None,
    ) -> RunResult:
        """Execute a full run.

        ``approve``/``reject`` supply the human decision for a non-interactive
        invocation. Neither defaults to approval: a run that policy says needs
        approval and was not given one stops at the gate.
        """
        started = time.monotonic()
        identifier = run_id or new_run_id()
        self._last_jev = None

        record = RunRecord(
            run_id=identifier,
            created_at=utc_now().isoformat(),
            raw_task=task,
            source_path=str(path),
            source_name=path.name,
            source_hash="",
            source_size_bytes=0,
            dry_run=dry_run,
            planner_source=getattr(self.planner, "source", "unknown"),
            config_fingerprint=self.config.fingerprint(),
        )

        with self.store.lock(identifier) as run_dir, AuditLog(run_dir.audit_file) as audit:
            audit.emit(
                Actor.USER,
                EventType.RUN_CREATED,
                {
                    "run_id": identifier,
                    "task": task.to_redacted(),
                    "dry_run": dry_run,
                    "source": str(path),
                    "config_fingerprint": self.config.fingerprint(),
                },
            )
            try:
                result = self._run_stages(
                    identifier,
                    path,
                    task,
                    record,
                    audit,
                    dry_run=dry_run,
                    output_path=output_path,
                    approve=approve,
                    reject=reject,
                    jev=jev,
                )
            except ExcelPilotError as error:
                result = self._fail(identifier, record, audit, error, started)
            except Exception as error:  # noqa: BLE001 - unexpected, but must be recorded
                wrapped = ExcelPilotError(f"{type(error).__name__}: {error}")
                result = self._fail(identifier, record, audit, wrapped, started)

        self.store.write_run(result.record)
        result.duration_seconds = time.monotonic() - started
        result.record = result.record.model_copy(
            update={"duration_seconds": round(result.duration_seconds, 3)}
        )
        self.store.write_run(result.record)
        return result

    def _run_stages(
        self,
        identifier: str,
        path: Path,
        task: UntrustedText,
        record: RunRecord,
        audit: AuditLog,
        *,
        dry_run: bool,
        output_path: Path | None,
        approve: bool,
        reject: bool,
        jev: JevAdapter | None,
    ) -> RunResult:
        state = self._advance(RunState.CREATED, RunState.INSPECTING)

        # -- inspect -------------------------------------------------------
        inspection = self.inspect(path)
        record = record.model_copy(
            update={
                "source_hash": inspection.content_hash,
                "source_size_bytes": inspection.file_size_bytes,
                "state": state,
            }
        )
        audit.emit(
            Actor.SYSTEM,
            EventType.WORKBOOK_INSPECTED,
            {
                "run_id": identifier,
                "summary": inspection.summary(),
                "hash": inspection.content_hash,
            },
        )

        # Snapshot the source before anything else touches it (ADR-0010).
        snapshot = self.store.snapshot(identifier, path)
        audit.emit(
            Actor.SYSTEM,
            EventType.SOURCE_SNAPSHOT,
            {
                "run_id": identifier,
                "path": str(snapshot),
                "hash": file_sha256(snapshot),
                "note": "byte copy taken before any work; the source is never written",
            },
        )

        # -- understand and plan -------------------------------------------
        state = self._advance(state, RunState.UNDERSTANDING)
        state = self._advance(state, RunState.PLANNING)
        understanding, plan = self._build_plan(task, inspection, identifier)
        record = record.model_copy(
            update={"intent_summary": understanding.intent_summary, "state": state}
        )
        audit.emit(
            Actor.AI if plan.planner_source == "llm" else Actor.SYSTEM,
            EventType.PLAN_BUILT,
            {
                "run_id": identifier,
                "planner_source": plan.planner_source,
                "operations": plan.operation_kinds,
                "intent_summary": understanding.intent_summary,
                "interpretation": understanding.interpretation.value,
                "missing_information": understanding.missing_information,
            },
        )
        if understanding.missing_information:
            audit.emit(
                Actor.SYSTEM,
                EventType.PLAN_REJECTED,
                {
                    "run_id": identifier,
                    "reason": "the request could not be resolved to a specific operation set",
                    "missing_information": understanding.missing_information,
                },
            )

        # -- preview (measurement, not estimation) -------------------------
        preview = self._preview_operations(plan, inspection)

        # -- decide --------------------------------------------------------
        state = self._advance(state, RunState.DECIDING)
        jev_result = self._decide(plan, inspection, jev)
        self._last_jev = jev_result
        if jev_result is not None:
            audit.emit(
                Actor.JEV,
                EventType.JEV_DECIDED if jev_result.jev_called else EventType.JEV_UNAVAILABLE,
                {
                    "run_id": identifier,
                    "called": jev_result.jev_called,
                    "provider": jev_result.provider.value,
                    "model": jev_result.model,
                    "error": jev_result.error,
                    "decisions": [d.model_dump(mode="json") for d in jev_result.decisions],
                    "escalates": jev_result.escalates,
                },
            )
        record = record.model_copy(
            update={
                "jev_called": bool(jev_result and jev_result.jev_called),
                "jev_provider": jev_result.provider if jev_result else self.config.jev.provider,
            }
        )

        # -- policy --------------------------------------------------------
        state = self._advance(state, RunState.POLICY_CHECK)
        policy = self._evaluate_policy(plan, inspection)
        record = record.model_copy(
            update={
                "policy_outcome": policy.outcome,
                "policy_rule_ids": list(policy.rule_ids),
                "state": state,
            }
        )
        audit.emit(
            Actor.POLICY,
            EventType.POLICY_EVALUATED,
            {
                "run_id": identifier,
                "outcome": policy.outcome.value,
                "rule_ids": policy.rule_ids,
                "reasons": policy.reasons,
                "jev_escalated": policy.jev_escalated,
                "config_fingerprint": policy.config_fingerprint,
            },
        )

        warnings = list(preview.warnings)

        if policy.denied:
            state = self._advance(state, RunState.REJECTED)
            audit.emit(
                Actor.POLICY,
                EventType.RUN_FAILED,
                {
                    "run_id": identifier,
                    "reason": "policy denied the run",
                    "rule_ids": policy.rule_ids,
                },
            )
            return self._finish(
                identifier,
                RunState.REJECTED,
                RunOutcome.REJECTED_BY_POLICY,
                record,
                inspection=inspection,
                understanding=understanding,
                plan=plan,
                jev=jev_result,
                policy=policy,
                warnings=warnings,
                error=policy.explain(),
            )

        # -- approval gate -------------------------------------------------
        approval_request: ApprovalRequest | None = None
        approval: ApprovalResult | None = None
        if policy.requires_approval:
            approval_request = self.build_approval_request(
                plan, inspection, preview, policy, jev_result, identifier
            )
            state = self._advance(state, RunState.AWAITING_APPROVAL)
            audit.emit(
                Actor.SYSTEM,
                EventType.APPROVAL_REQUESTED,
                {
                    "run_id": identifier,
                    "risk": approval_request.risk.value,
                    "cells_to_change": approval_request.cells_to_change,
                    "policy_rule_ids": approval_request.policy_rule_ids,
                    "jev_escalated": approval_request.jev_escalated,
                },
            )

            if dry_run:
                record = record.model_copy(
                    update={"approval_status": ApprovalStatus.PENDING, "state": state}
                )
                return self._finish(
                    identifier,
                    RunState.AWAITING_APPROVAL,
                    RunOutcome.DRY_RUN,
                    record,
                    inspection=inspection,
                    understanding=understanding,
                    plan=plan,
                    jev=jev_result,
                    policy=policy,
                    approval_request=approval_request,
                    warnings=warnings,
                )

            if reject:
                approval = ApprovalResult(
                    status=ApprovalStatus.REJECTED,
                    run_id=identifier,
                    decided_at=utc_now().isoformat(),
                    reason="rejected by the operator",
                )
                audit.emit(
                    Actor.USER,
                    EventType.APPROVAL_REJECTED,
                    {"run_id": identifier, "reason": "rejected"},
                )
                return self._finish(
                    identifier,
                    RunState.REJECTED,
                    RunOutcome.REJECTED_BY_APPROVAL,
                    record,
                    inspection=inspection,
                    understanding=understanding,
                    plan=plan,
                    jev=jev_result,
                    policy=policy,
                    approval_request=approval_request,
                    approval=approval,
                    warnings=warnings,
                    error="rejected by the operator",
                )

            if not approve:
                # The safe default: no approval given means no execution.
                approval = ApprovalResult(
                    status=ApprovalStatus.PENDING,
                    run_id=identifier,
                    decided_at=utc_now().isoformat(),
                )
                audit.emit(
                    Actor.USER,
                    EventType.APPROVAL_REQUESTED,
                    {
                        "run_id": identifier,
                        "note": "no approval supplied; the run stopped at the gate",
                    },
                )
                record = record.model_copy(
                    update={"approval_status": ApprovalStatus.PENDING, "state": state}
                )
                return self._finish(
                    identifier,
                    RunState.AWAITING_APPROVAL,
                    RunOutcome.FAILED,
                    record,
                    inspection=inspection,
                    understanding=understanding,
                    plan=plan,
                    jev=jev_result,
                    policy=policy,
                    approval_request=approval_request,
                    approval=approval,
                    warnings=warnings,
                    error="approval is required; re-run with --approve once reviewed",
                )

            approval = ApprovalResult(
                status=ApprovalStatus.APPROVED,
                run_id=identifier,
                decided_at=utc_now().isoformat(),
            )
            audit.emit(
                Actor.USER,
                EventType.APPROVAL_GRANTED,
                {"run_id": identifier, "decided_by": "operator"},
            )
            record = record.model_copy(
                update={"approval_status": ApprovalStatus.APPROVED, "state": state}
            )
        else:
            approval = ApprovalResult(
                status=ApprovalStatus.NOT_REQUIRED,
                run_id=identifier,
                decided_at=utc_now().isoformat(),
            )
            record = record.model_copy(
                update={"approval_status": ApprovalStatus.NOT_REQUIRED, "state": state}
            )

        # -- dry run stops here --------------------------------------------
        if dry_run:
            audit.emit(
                Actor.SYSTEM,
                EventType.DRY_RUN_COMPLETED,
                {
                    "run_id": identifier,
                    "cells_to_change": preview.cells_to_change,
                    "sheets_affected": preview.sheets_affected,
                    "formulas_to_add": preview.formulas_to_add,
                    "formulas_to_remove": preview.formulas_to_remove,
                    "approval_required": policy.requires_approval,
                },
            )
            return self._finish(
                identifier,
                RunState.AWAITING_APPROVAL if policy.requires_approval else RunState.COMPLETED,
                RunOutcome.DRY_RUN,
                record,
                inspection=inspection,
                understanding=understanding,
                plan=plan,
                jev=jev_result,
                policy=policy,
                approval_request=approval_request,
                approval=approval,
                warnings=warnings,
            )

        # -- execute -------------------------------------------------------
        return self._execute(
            identifier,
            path,
            plan,
            inspection,
            record,
            audit,
            preview=preview,
            understanding=understanding,
            jev=jev_result,
            policy=policy,
            approval_request=approval_request,
            approval=approval,
            output_path=output_path,
            warnings=warnings,
            snapshot=snapshot,
        )

    def _execute(
        self,
        identifier: str,
        path: Path,
        plan: ExecutionPlan,
        inspection: Any,
        record: RunRecord,
        audit: AuditLog,
        *,
        preview: Any,
        understanding: TaskUnderstanding,
        jev: JevDecisionSet | None,
        policy: PolicyDecision,
        approval_request: ApprovalRequest | None,
        approval: ApprovalResult | None,
        output_path: Path | None,
        warnings: list[str],
        snapshot: Path,
    ) -> RunResult:
        state = self._advance(RunState.POLICY_CHECK, RunState.EXECUTING)
        destination = output_path or versioned_output(path, identifier)
        state = RunState.EXECUTING

        # The executor works on the snapshot, never on the user's file.
        audit.emit(
            Actor.SYSTEM,
            EventType.EXECUTION_STARTED,
            {
                "run_id": identifier,
                "operations": plan.operation_kinds,
                "working_copy": str(snapshot),
                "output": str(destination),
            },
        )

        # The Executor builds its own OperationContext, so the write-safety
        # configuration reaches the handlers from one place.
        with opened(snapshot) as workbook:
            try:
                execution = self.executor.execute(
                    workbook, plan, inspection, output_path=destination
                )
            except ExecutionError as error:
                execution = error.state
                # A guard denial happened at execution time, after the planning
                # pass had already recorded its own rule ids. Merge them in, so
                # the run record attributes the refusal to the control that
                # actually stopped it rather than leaving `policy_rule_ids`
                # empty for what was a hard-deny security event.
                denied_at_execution: list[str] = []
                for result in execution.operations:
                    for rule_id in result.details.get("denied_by_rules", []) or []:
                        if rule_id not in denied_at_execution:
                            denied_at_execution.append(rule_id)
                if denied_at_execution:
                    record = record.model_copy(
                        update={
                            "policy_rule_ids": [
                                *record.policy_rule_ids,
                                *[
                                    r
                                    for r in denied_at_execution
                                    if r not in record.policy_rule_ids
                                ],
                            ],
                            "policy_outcome": PolicyOutcome.DENY,
                        }
                    )
                audit.emit(
                    Actor.SYSTEM,
                    EventType.EXECUTION_FAILED,
                    {
                        "run_id": identifier,
                        "errors": execution.errors,
                        **({"denied_by_rules": denied_at_execution} if denied_at_execution else {}),
                    },
                )
                # The snapshot is not saved, so nothing partial is written.
                return self._finish(
                    identifier,
                    RunState.FAILED,
                    RunOutcome.FAILED,
                    record.model_copy(update={"state": state}),
                    inspection=inspection,
                    understanding=understanding,
                    plan=plan,
                    jev=jev,
                    policy=policy,
                    approval_request=approval_request,
                    approval=approval,
                    execution=execution,
                    warnings=warnings,
                    error="; ".join(execution.errors) or "execution failed",
                )

            # Save atomically, so no reader sees a partial workbook.
            save_atomic(workbook, destination)

        audit.emit(
            Actor.SYSTEM,
            EventType.EXECUTION_COMPLETED,
            {"run_id": identifier, "totals": execution.totals()},
        )
        for result in execution.operations:
            if result.cells_neutralised:
                audit.emit(
                    Actor.SYSTEM,
                    EventType.FORMULA_INJECTION_NEUTRALISED,
                    {
                        "run_id": identifier,
                        "operation": result.operation,
                        "count": result.cells_neutralised,
                    },
                )

        output_record = self.store.record_output(destination)
        record = record.model_copy(
            update={
                "output_path": str(destination),
                "output_hash": output_record["hash"],
                "state": RunState.EXECUTING,
            }
        )
        audit.emit(
            Actor.SYSTEM,
            EventType.OUTPUT_WRITTEN,
            {"run_id": identifier, **output_record},
        )

        # -- verify --------------------------------------------------------
        state = self._advance(state, RunState.VERIFYING)
        reconciliation = self._reconciliation(workbook_execution=execution)
        verification = self.verifier.verify(
            identifier,
            destination,
            before_path=snapshot,
            expected_sheets=list(inspection.sheet_names),
            data_targets=_data_targets(plan),
            planned_cells_changed=preview.cells_to_change,
            actual_cells_changed=execution.total_cells_written,
            reconciliation=reconciliation,
            output_hash=output_record["hash"],
            removed_rows=list(execution.removed_coordinates),
        )
        self.store.write_verification(verification)
        audit.emit(
            Actor.SYSTEM,
            EventType.VERIFICATION_COMPLETED,
            {
                "run_id": identifier,
                "status": verification.status.value,
                "passed": verification.passed,
                "recalculated": verification.recalculated,
                "static_formula_checks": verification.static_formula_checks,
                "failed_checks": [c.name for c in verification.failed_checks],
            },
        )
        if reconciliation is not None:
            audit.emit(
                Actor.SYSTEM,
                EventType.RECONCILIATION_COMPLETED,
                {
                    "run_id": identifier,
                    "summary": reconciliation.summary(),
                    "recalculated": reconciliation.recalculated,
                },
            )
        for anomaly in verification.anomalies:
            audit.emit(
                Actor.SYSTEM,
                EventType.ANOMALY_DETECTED,
                {"run_id": identifier, **anomaly.to_summary()},
            )

        # -- diff and manifest ---------------------------------------------
        with opened(snapshot) as before_wb, opened(destination) as after_wb:
            diff = diff_workbooks(
                before_wb, after_wb, before_name=str(snapshot), after_name=str(destination)
            )
        diff = diff.model_copy(
            update={"before_hash": file_sha256(snapshot), "after_hash": output_record["hash"]}
        )
        audit.emit(
            Actor.SYSTEM,
            EventType.DIFF_COMPUTED,
            {"run_id": identifier, "summary": diff.summary()},
        )

        manifest = ChangeManifest(
            run_id=identifier,
            workbook_name=inspection.file_name,
            source_path=str(path),
            source_hash=inspection.content_hash,
            output_path=str(destination),
            output_hash=output_record["hash"],
            created_at=utc_now().isoformat(),
            diff=diff,
            operations=[r.model_dump(mode="json") for r in execution.operations],
            verification={
                "status": verification.status.value,
                "recalculated": verification.recalculated,
                "static_formula_checks": verification.static_formula_checks,
            },
            reconciliation=(
                [r.model_dump(mode="json") for r in reconciliation.results]
                if reconciliation
                else []
            ),
            anomalies=[a.to_summary() for a in verification.anomalies],
            approval={
                "required": approval_request is not None,
                "status": approval.status.value if approval else ApprovalStatus.NOT_REQUIRED.value,
            },
            risk=self.policy.risk_level(
                PolicyRequest(
                    operations=list(plan.operations),
                    cells_affected=preview.cells_to_change,
                    structural_change=preview.structural_change,
                    sensitivity_level=inspection.sensitivity.level,
                )
            ),
        )
        self.store.write_manifest(manifest)

        # The outcome depends on verification, not on the save.
        if not verification.passed:
            record = record.model_copy(
                update={
                    "state": RunState.FAILED,
                    "verification_status": verification.status.value,
                    "verification_passed": False,
                }
            )
            audit.emit(
                Actor.SYSTEM,
                EventType.RUN_FAILED,
                {
                    "run_id": identifier,
                    "reason": "verification did not pass; a file was written but the outcome "
                    "was not established",
                    "failed_checks": [c.name for c in verification.failed_checks],
                },
            )
            return self._finish(
                identifier,
                RunState.FAILED,
                RunOutcome.FAILED,
                record,
                inspection=inspection,
                understanding=understanding,
                plan=plan,
                jev=jev,
                policy=policy,
                approval_request=approval_request,
                approval=approval,
                execution=execution,
                verification=verification,
                manifest=manifest,
                report=manifest.to_text(),
                warnings=warnings,
                error="verification failed; the output was written but must not be treated as correct",
            )

        state = self._advance(state, RunState.COMPLETED)
        record = record.model_copy(
            update={
                "state": RunState.COMPLETED,
                "verification_status": verification.status.value,
                "verification_passed": True,
            }
        )
        audit.emit(
            Actor.SYSTEM,
            EventType.RUN_COMPLETED,
            {
                "run_id": identifier,
                "output": str(destination),
                "output_hash": output_record["hash"],
                "duration_note": "see run.json",
            },
        )
        return self._finish(
            identifier,
            RunState.COMPLETED,
            RunOutcome.SUCCEEDED,
            record,
            inspection=inspection,
            understanding=understanding,
            plan=plan,
            jev=jev,
            policy=policy,
            approval_request=approval_request,
            approval=approval,
            execution=execution,
            verification=verification,
            manifest=manifest,
            report=manifest.to_text(),
            warnings=warnings,
        )

    def _reconciliation(self, *, workbook_execution: ExecutionState) -> ReconciliationReport | None:
        """Collect reconciliation results produced during execution.

        Built from what the executor recorded. Reconciliation happens *during*
        execution, against an in-memory workbook, so it reads whatever values
        openpyxl last cached. That is not a recalculation: a total over formula
        cells is reported as ``derived_from_formula_cells`` and downgraded to a
        warning rather than presented as a verified pass (ADR-0011).

        Genuine evaluation happens afterwards, in verification, via
        ``app.verification.recalc``. The two stay deliberately distinct — this
        report describes what execution observed, the verification result
        describes what evaluation proved.
        """
        results = list(workbook_execution.reconciliation_results)
        if not results:
            return None
        return ReconciliationReport(
            run_id=workbook_execution.run_id,
            results=results,
            passed_count=sum(1 for r in results if r.passed),
            failed_count=sum(1 for r in results if r.status.value == "failed"),
            warning_count=sum(1 for r in results if r.status.value == "warning"),
            recalculated=False,
        )

    def _fail(
        self,
        identifier: str,
        record: RunRecord,
        audit: AuditLog,
        error: ExcelPilotError,
        started: float,
    ) -> RunResult:
        record = record.model_copy(
            update={
                "state": RunState.FAILED,
                "error": error.message,
                "completed_at": utc_now().isoformat(),
            }
        )
        audit.emit(
            Actor.SYSTEM,
            EventType.RUN_FAILED,
            {
                "run_id": identifier,
                "error_code": error.code,
                "error": error.message,
                "details": error.details,
            },
        )
        return RunResult(
            run_id=identifier,
            state=RunState.FAILED,
            outcome=RunOutcome.FAILED,
            record=record,
            error=error.message,
            duration_seconds=time.monotonic() - started,
        )

    def _finish(
        self,
        identifier: str,
        state: RunState,
        outcome: RunOutcome,
        record: RunRecord,
        *,
        inspection: Any = None,
        understanding: TaskUnderstanding | None = None,
        plan: ExecutionPlan | None = None,
        jev: JevDecisionSet | None = None,
        policy: PolicyDecision | None = None,
        approval_request: ApprovalRequest | None = None,
        approval: ApprovalResult | None = None,
        execution: ExecutionState | None = None,
        verification: VerificationResult | None = None,
        manifest: ChangeManifest | None = None,
        report: str = "",
        warnings: list[str] | None = None,
        error: str | None = None,
    ) -> RunResult:
        record = record.model_copy(
            update={"state": state, "outcome": outcome, "completed_at": utc_now().isoformat()}
        )
        return RunResult(
            run_id=identifier,
            state=state,
            outcome=outcome,
            record=record,
            inspection=inspection,
            understanding=understanding,
            plan=plan,
            jev=jev,
            policy=policy,
            approval_request=approval_request,
            approval=approval,
            execution=execution,
            verification=verification,
            manifest=manifest,
            report=report,
            error=error,
            warnings=warnings or [],
        )

    @staticmethod
    def _advance(current: RunState, target: RunState) -> RunState:
        """Transition, validating the move.

        A bug that skipped approval would otherwise be invisible; here it is an
        error.
        """
        if target not in _TRANSITIONS.get(current, frozenset()):
            raise RunStateError(
                f"illegal run transition {current.value} -> {target.value}",
                details={"from": current.value, "to": target.value},
            )
        return target


def _decision_context(plan: ExecutionPlan, inspection: Any) -> DecisionContext:
    """Facts for JEV. No cell contents, no formulas, no credentials."""
    sheets = sorted({op.target.sheet for op in plan.operations if hasattr(op, "target")})
    return DecisionContext(
        run_id=plan.run_id,
        task_summary=plan.understanding.intent_summary,
        sheet_count=len(inspection.sheet_names),
        sheets_affected=sheets,
        total_rows=inspection.total_rows,
        cells_to_change=0,
        formulas_to_add=0,
        formulas_to_remove=0,
        structural_change=any(
            "structural_change" in (op.model_extra or {}) for op in plan.operations
        ),
        hidden_sheets_present=inspection.hidden_sheet_count > 0,
        ambiguity_signals=list(plan.understanding.missing_information),
        operation_kinds=plan.operation_kinds,
        # What this deployment can actually do, measured at ask time rather than
        # assumed. The JEV verification question is worded from this.
        recalculation_available=recalc.library_available(),
    )


def _data_targets(plan: ExecutionPlan) -> list[Target]:
    """Targets whose data should be checked for loss."""
    seen: set[str] = set()
    targets: list[Target] = []
    for op in plan.operations:
        target = getattr(op, "target", None)
        if isinstance(target, Target) and target.sheet not in seen:
            seen.add(target.sheet)
            targets.append(target)
    return targets


def _jev_summary(jev: JevDecisionSet | None) -> str:
    if jev is None or not jev.jev_called:
        reason = jev.error if jev and jev.error else "JEV was not consulted"
        return reason
    return "; ".join(
        f"{d.question}={d.value} ({d.status}"
        + (f", p={d.probability:.2f}" if d.probability is not None else "")
        + ")"
        for d in jev.decisions
    )


__all__ = ["RunOrchestrator", "RunResult", "RunStateError", "Stage"]
