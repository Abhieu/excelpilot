"""Planning, decisioning, policy, approval and run-lifecycle contracts."""

from __future__ import annotations

from typing import Any

from pydantic import Field, field_validator, model_validator

from app.contracts.base import ContractModel, MutableContractModel, UntrustedText
from app.contracts.enums import (
    ApprovalStatus,
    AutomationVerdict,
    InterpretationVerdict,
    JevProvider,
    PolicyOutcome,
    RiskLevel,
    RunOutcome,
    RunState,
)
from app.contracts.operations import ReconcileSpec, WorkbookOperation

# --------------------------------------------------------------------------
# Task understanding
# --------------------------------------------------------------------------


class TaskUnderstanding(ContractModel):
    """The parsed intent of a user's request.

    Produced by the planner. This is the *only* place free text is allowed to
    become structured intent, and it is always validated.
    """

    raw_task: UntrustedText
    intent_summary: str = Field(min_length=1, max_length=2_000)
    interpretation: InterpretationVerdict
    missing_information: list[str] = Field(default_factory=list)
    referenced_sheets: list[str] = Field(default_factory=list)
    referenced_columns: list[str] = Field(default_factory=list)
    planner_source: str = Field(default="deterministic", description="'deterministic' or 'llm'")

    @model_validator(mode="after")
    def _ambiguity_requires_reasons(self) -> TaskUnderstanding:
        if (
            self.interpretation is not InterpretationVerdict.SUFFICIENTLY_CLEAR
            and not self.missing_information
        ):
            raise ValueError(
                "an ambiguous or under-specified task must list what information is missing"
            )
        return self


class ExecutionPlan(ContractModel):
    """A validated, ordered set of operations.

    The *only* thing the executor accepts. Whatever produced it — a rule-based
    compiler or a language model — the plan is the same typed structure, and it
    still has to pass deterministic policy.
    """

    plan_id: str = Field(min_length=1, max_length=64)
    run_id: str = Field(min_length=1, max_length=64)
    understanding: TaskUnderstanding
    operations: list[WorkbookOperation] = Field(min_length=1)
    source_hash: str = Field(description="Hash of the workbook this plan was built against")
    planner_source: str = "deterministic"
    notes: list[str] = Field(default_factory=list)
    contract_version: str = "1"

    @field_validator("operations")
    @classmethod
    def _bounded_plan(cls, ops: list[Any]) -> list[Any]:
        if len(ops) > 200:
            raise ValueError("a plan may contain at most 200 operations")
        return ops

    @property
    def operation_kinds(self) -> list[str]:
        return [op.operation.value for op in self.operations]

    @property
    def is_mutating(self) -> bool:
        return any(op.operation.is_mutating for op in self.operations)

    def duplicate_operations(self) -> list[WorkbookOperation]:
        """Mutating operations that would be run more than once.

        The executor refuses to apply these. Idempotence is not assumed.
        """
        seen: dict[str, WorkbookOperation] = {}
        duplicated: list[WorkbookOperation] = []
        for op in self.operations:
            if not op.operation.is_mutating:
                continue
            key = op.model_dump_json()
            if key in seen:
                duplicated.append(op)
            else:
                seen[key] = op
        return duplicated


# --------------------------------------------------------------------------
# JEV decisioning
# --------------------------------------------------------------------------


class DecisionQuestion(ContractModel):
    """One question put to JEV, in the documented request shape.

    Mirrors ``jev.py``'s ``validate_request``: ``choice`` requires 2–255 criteria,
    ``score`` requires 2–10 ordered criteria.
    """

    id: str = Field(min_length=1, max_length=64)
    type: str = Field(pattern="^(choice|noul|score)$")
    instructions: str = Field(min_length=1, max_length=4_000)
    criteria: dict[str, str] | list[str] = Field(default_factory=dict)
    model: str | None = None

    @model_validator(mode="after")
    def _criteria_shape_matches_type(self) -> DecisionQuestion:
        if self.type == "choice":
            if not isinstance(self.criteria, dict):
                raise ValueError("choice questions need a dict of label -> description")
            if not 2 <= len(self.criteria) <= 255:
                raise ValueError("choice questions require 2-255 criteria")
        elif self.type == "score":
            if not isinstance(self.criteria, list):
                raise ValueError("score questions need a list of ordered criteria")
            if not 2 <= len(self.criteria) <= 10:
                raise ValueError("score questions require 2-10 criteria")
        return self


class DecisionContext(ContractModel):
    """Facts put to JEV. Contains no instructions and no mutation intent."""

    run_id: str
    task_summary: str = Field(max_length=2_000)
    sheet_count: int = Field(default=0, ge=0)
    sheets_affected: list[str] = Field(default_factory=list)
    total_rows: int = Field(default=0, ge=0)
    cells_to_change: int = Field(default=0, ge=0)
    formulas_to_add: int = Field(default=0, ge=0)
    formulas_to_remove: int = Field(default=0, ge=0)
    records_removed: int = Field(default=0, ge=0)
    structural_change: bool = False
    hidden_sheets_present: bool = False
    ambiguity_signals: list[str] = Field(default_factory=list)
    operation_kinds: list[str] = Field(default_factory=list)


class JevDecision(ContractModel):
    """One advisory decision from JEV.

    **This type has no field capable of expressing a workbook mutation.** That is
    the structural enforcement of "JEV must not mutate workbooks" (ADR-0004) — not
    a convention, an absence of capability. ``JevDecisionSet`` likewise cannot be
    passed to the executor, which accepts only ``ExecutionPlan``.
    """

    question: str
    value: str
    status: str = Field(description="selected | needs_review | scored")
    probability: float | None = Field(default=None, ge=0, le=1)
    margin: float | None = Field(default=None, ge=0, le=1)
    confidence: float | None = Field(default=None, ge=0, le=1)

    @property
    def needs_review(self) -> bool:
        return self.status == "needs_review"

    @property
    def is_usable(self) -> bool:
        """Advisory value is trustworthy enough to inform policy."""
        return self.status in {"selected", "scored"}


class JevDecisionSet(ContractModel):
    """All decisions from one JEV call, plus provenance for the audit trail."""

    decisions: list[JevDecision] = Field(default_factory=list)
    jev_called: bool = False
    provider: JevProvider = JevProvider.DISABLED
    model: str | None = None
    elapsed_seconds: float | None = Field(default=None, ge=0)
    min_probability: float = Field(default=0.8, ge=0.5, le=1)
    min_margin: float = Field(default=0.15, ge=0, le=1)
    error: str | None = None

    def get(self, question: str) -> JevDecision | None:
        for decision in self.decisions:
            if decision.question == question:
                return decision
        return None

    @property
    def automation(self) -> JevDecision | None:
        return self.get("automation")

    @property
    def risk(self) -> JevDecision | None:
        return self.get("risk")

    @property
    def interpretation(self) -> JevDecision | None:
        return self.get("interpretation")

    @property
    def any_needs_review(self) -> bool:
        return any(d.needs_review for d in self.decisions)

    @property
    def escalates(self) -> bool:
        """Whether this decision set should raise scrutiny.

        Only ever true. JEV can make a run *more* cautious, never less
        (ADR-0005). There is deliberately no de-escalation path.
        """
        if self.any_needs_review:
            return True
        automation = self.automation
        if automation and automation.value in {
            AutomationVerdict.NO.value,
            AutomationVerdict.APPROVAL_REQUIRED.value,
        }:
            return True
        risk = self.risk
        return bool(risk and risk.value == RiskLevel.HIGH.value)


# --------------------------------------------------------------------------
# Policy
# --------------------------------------------------------------------------


class PolicyRequest(ContractModel):
    """Everything the policy engine is allowed to consider.

    A pure function of these facts. No model, no network, no clock.
    """

    operations: list[WorkbookOperation]
    sheet_names: list[str] = Field(default_factory=list)
    total_rows: int = Field(default=0, ge=0)
    total_cells: int = Field(default=0, ge=0)
    sensitivity_level: str = "public"
    has_hidden_sheets: bool = False
    has_vba: bool = False
    structural_change: bool = False
    ambiguity_signals: list[str] = Field(default_factory=list)
    cells_affected: int = Field(default=0, ge=0)
    formulas_removed: int = Field(default=0, ge=0)
    output_overwrites_source: bool = False
    requested_output_path: str | None = None
    workspace_root: str | None = None
    config_fingerprint: str | None = None


class PolicyDecision(ContractModel):
    """Deterministic policy verdict. The sole authority on permission."""

    outcome: PolicyOutcome
    rule_ids: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)
    jev_escalated: bool = Field(
        default=False,
        description="True when a JEV decision raised scrutiny. Never lowers the outcome.",
    )
    evaluated_facts: dict[str, Any] = Field(default_factory=dict)
    config_fingerprint: str | None = None

    @property
    def requires_approval(self) -> bool:
        """Derived from the outcome, never stored.

        A stored flag could disagree with ``outcome``, and then two components
        would disagree about whether a human was required. Deriving it removes
        that entire class of bug.
        """
        return self.outcome is PolicyOutcome.REQUIRE_APPROVAL

    @property
    def denied(self) -> bool:
        return self.outcome is PolicyOutcome.DENY

    @property
    def allowed(self) -> bool:
        return self.outcome is PolicyOutcome.ALLOW

    def explain(self) -> str:
        if not self.rule_ids:
            return f"policy: {self.outcome.value} (no rule fired; default)"
        return f"policy: {self.outcome.value} via {', '.join(self.rule_ids)}"


# --------------------------------------------------------------------------
# Approval
# --------------------------------------------------------------------------


class ApprovalRequest(ContractModel):
    """What the human approver is shown. Everything needed to decide."""

    run_id: str
    workbook_name: str
    source_path: str
    proposed_output_path: str
    intent_summary: str
    operation_kinds: list[str] = Field(default_factory=list)
    sheets_affected: list[str] = Field(default_factory=list)
    ranges_affected: list[str] = Field(default_factory=list)
    cells_to_change: int = Field(default=0, ge=0)
    formulas_to_add: int = Field(default=0, ge=0)
    formulas_to_remove: int = Field(default=0, ge=0)
    records_removed: int = Field(default=0, ge=0)
    structural_change: bool = False
    risk: RiskLevel = RiskLevel.LOW
    risk_reasons: list[str] = Field(default_factory=list)
    jev_summary: str = "JEV not consulted"
    jev_escalated: bool = False
    policy_explanation: str = ""
    policy_rule_ids: list[str] = Field(default_factory=list)
    ambiguity: list[str] = Field(default_factory=list)
    verification_plan: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    reconciliation_checks: list[ReconcileSpec] = Field(default_factory=list)
    created_at: str


class ApprovalResult(ContractModel):
    status: ApprovalStatus
    run_id: str
    decided_at: str
    decided_by: str = "operator"
    reason: str | None = None

    @property
    def approved(self) -> bool:
        return self.status is ApprovalStatus.APPROVED


# --------------------------------------------------------------------------
# Execution results
# --------------------------------------------------------------------------


class OperationResult(ContractModel):
    """Structured outcome of one operation. Feeds diff, manifest, audit."""

    operation: str
    status: str = Field(description="applied | skipped | failed | preview")
    cells_read: int = Field(default=0, ge=0)
    cells_written: int = Field(default=0, ge=0)
    formulas_added: int = Field(default=0, ge=0)
    formulas_removed: int = Field(default=0, ge=0)
    rows_affected: int = Field(default=0, ge=0)
    rows_removed: int = Field(default=0, ge=0)
    rows_written: int = Field(default=0, ge=0)
    rows_read: int = Field(default=0, ge=0)
    sheets_created: list[str] = Field(default_factory=list)
    sheets_renamed: list[str] = Field(default_factory=list)
    cells_neutralised: int = Field(
        default=0,
        ge=0,
        description="Values written as literal text because they looked like formulas.",
    )
    warnings: list[str] = Field(default_factory=list)
    error: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class ExecutionState(MutableContractModel):
    """Aggregate outcome of a whole execution.

    Mutable because it accumulates operation by operation: a run that fails on
    operation 3 of 5 must be able to report operations 1 and 2 accurately. It is
    still validated on every assignment.
    """

    run_id: str
    operations: list[OperationResult] = Field(default_factory=list)
    status: str = Field(default="pending", description="pending | executing | applied | failed")
    total_cells_written: int = Field(default=0, ge=0)
    total_rows_affected: int = Field(default=0, ge=0)
    total_formulas_added: int = Field(default=0, ge=0)
    total_formulas_removed: int = Field(default=0, ge=0)
    errors: list[str] = Field(default_factory=list)

    @property
    def applied(self) -> bool:
        return self.status == "applied"

    def totals(self) -> dict[str, int]:
        return {
            "operations": len(self.operations),
            "cells_written": self.total_cells_written,
            "rows_affected": self.total_rows_affected,
            "formulas_added": self.total_formulas_added,
            "formulas_removed": self.total_formulas_removed,
        }


# --------------------------------------------------------------------------
# Dry run
# --------------------------------------------------------------------------


class DryRunPreview(ContractModel):
    """Calculated preview of a run. Every number here is measured, never estimated."""

    run_id: str
    workbook_name: str
    source_path: str
    proposed_output_path: str
    cells_to_change: int = Field(default=0, ge=0)
    sheets_affected: int = Field(default=0, ge=0)
    sheets_affected_names: list[str] = Field(default_factory=list)
    formulas_to_add: int = Field(default=0, ge=0)
    formulas_to_remove: int = Field(default=0, ge=0)
    records_to_normalize: int = Field(default=0, ge=0)
    records_to_remove: int = Field(default=0, ge=0)
    records_requiring_review: int = Field(default=0, ge=0)
    structural_change: bool = False
    risk: RiskLevel = RiskLevel.LOW
    risk_reasons: list[str] = Field(default_factory=list)
    approval_required: bool = False
    verification_plan: list[str] = Field(default_factory=list)
    operation_breakdown: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------
# Run lifecycle
# --------------------------------------------------------------------------


class RunRecord(ContractModel):
    """Durable record of one run. Written to ``run.json``."""

    run_id: str
    state: RunState = RunState.CREATED
    outcome: RunOutcome | None = None
    created_at: str
    completed_at: str | None = None
    duration_seconds: float | None = Field(default=None, ge=0)
    raw_task: UntrustedText
    intent_summary: str | None = None
    source_path: str
    source_name: str
    source_hash: str
    source_size_bytes: int = Field(default=0, ge=0)
    output_path: str | None = None
    output_hash: str | None = None
    dry_run: bool = False
    planner_source: str = "deterministic"
    jev_called: bool = False
    jev_provider: JevProvider = JevProvider.DISABLED
    policy_outcome: PolicyOutcome | None = None
    policy_rule_ids: list[str] = Field(default_factory=list)
    approval_status: ApprovalStatus = ApprovalStatus.NOT_REQUIRED
    verification_status: str | None = None
    verification_passed: bool | None = None
    config_fingerprint: str | None = None
    error: str | None = None
    replay_of: str | None = None
    contract_version: str = "1"


__all__ = [
    "ApprovalRequest",
    "ApprovalResult",
    "DecisionContext",
    "DecisionQuestion",
    "DryRunPreview",
    "ExecutionPlan",
    "ExecutionState",
    "JevDecision",
    "JevDecisionSet",
    "OperationResult",
    "PolicyDecision",
    "PolicyRequest",
    "RunRecord",
    "TaskUnderstanding",
]
