"""Deterministic policy engine.

``evaluate(request) -> PolicyDecision`` is a pure function. Same request plus same
thresholds always yields the same decision, which is what makes a policy decision
reproducible, testable, and meaningful in an audit trail (ADR-0005).

The JEV relationship is deliberately asymmetric:

    requires_approval = policy_requires OR jev_escalates

JEV can raise scrutiny. It can never lower a requirement, and there is
deliberately no "allowed because JEV was confident" path. If policy says DENY, a
JEV verdict of ``automation: yes`` at probability 0.99 changes nothing.
"""

from __future__ import annotations

from typing import Any

from app.contracts.config import ExcelPilotConfig
from app.contracts.enums import PolicyOutcome, RiskLevel
from app.contracts.pipeline import JevDecisionSet, PolicyDecision, PolicyRequest
from app.policy.rules import (
    ALL_RULES,
    ESCALATION_RULES,
    HARD_DENY_RULES,
    READ_ONLY_KINDS,
    rule_catalog,
)


class PolicyEngine:
    """Evaluates policy deterministically.

    Holds no mutable state, so a single instance can be shared across a run
    without affecting reproducibility.
    """

    def __init__(self, config: ExcelPilotConfig | None = None) -> None:
        self.config = config or ExcelPilotConfig()

    def evaluate(
        self,
        request: PolicyRequest,
        *,
        jev: JevDecisionSet | None = None,
    ) -> PolicyDecision:
        """Decide whether a request is allowed, needs approval, or is denied.

        Order matters: hard denies are evaluated first and short-circuit, so a
        denial is never softened by an escalation that also fired.
        """
        thresholds = self.config.policy
        fired: list[str] = []
        reasons: list[str] = []
        facts: dict[str, object] = {
            "operations": len(request.operations),
            "cells_affected": request.cells_affected,
            "formulas_removed": request.formulas_removed,
            "sensitivity": request.sensitivity_level,
            "has_hidden_sheets": request.has_hidden_sheets,
            "has_vba": request.has_vba,
            "structural_change": request.structural_change,
        }

        for rule in HARD_DENY_RULES:
            outcome = rule(request, thresholds)
            if outcome is not None:
                fired.append(rule.rule_id)
                reasons.append(outcome.reason)
                facts.update(outcome.facts)
                return PolicyDecision(
                    outcome=PolicyOutcome.DENY,
                    rule_ids=fired,
                    reasons=reasons,
                    # A denial is not "un-escalated" by JEV; record whether JEV
                    # even had an opinion, for the audit trail.
                    jev_escalated=bool(jev and jev.escalates),
                    evaluated_facts=facts,
                    config_fingerprint=self.config.fingerprint(),
                )

        for rule in ESCALATION_RULES:
            outcome = rule(request, thresholds)
            if outcome is not None:
                fired.append(rule.rule_id)
                reasons.append(outcome.reason)
                facts.update(outcome.facts)

        jev_escalated = bool(jev and jev.escalates)
        if jev_escalated and jev is not None:
            reasons.append("JEV raised scrutiny: " + _jev_summary(jev))

        policy_requires_approval = bool(fired)
        final = (
            PolicyOutcome.REQUIRE_APPROVAL
            if policy_requires_approval or jev_escalated
            else PolicyOutcome.ALLOW
        )

        return PolicyDecision(
            outcome=final,
            rule_ids=fired,
            reasons=reasons,
            jev_escalated=jev_escalated,
            evaluated_facts=facts,
            config_fingerprint=self.config.fingerprint(),
        )

    def risk_level(self, request: PolicyRequest) -> RiskLevel:
        """Classify risk from deterministic facts alone.

        Used to populate the approval request. Distinct from the allow/deny
        decision: a low-risk run can still require approval, and a high-risk one
        can still be allowed after approval is granted.
        """
        score = 0
        thresholds = self.config.policy

        # Scale the cell-change contribution with magnitude rather than treating
        # every change above the threshold alike: 2,000 changed cells and 500,000
        # changed cells are very different things to approve.
        if request.cells_affected > thresholds.cell_change_approval_threshold:
            score += 2
            if request.cells_affected > thresholds.cell_change_approval_threshold * 10:
                score += 2
        elif request.cells_affected > 100:
            score += 1
        if request.formulas_removed > 0:
            score += 2
            if request.formulas_removed > 100:
                score += 1
        if request.structural_change:
            score += 1
        if request.sensitivity_level == "restricted":
            score += 3
        elif request.sensitivity_level == "confidential":
            score += 2
        if request.has_vba:
            score += 2
        if request.has_hidden_sheets:
            score += 1
        if request.ambiguity_signals:
            score += 2

        # A read-only run cannot damage data, so its risk is LOW by definition.
        # Note this is risk, not permission: a read of a restricted workbook is
        # still escalated by the ``restricted_data`` rule, because the concern
        # there is who is allowed to see the data, not what the run will change.
        read_only = all(op.operation.value in READ_ONLY_KINDS for op in request.operations)
        if read_only:
            return RiskLevel.LOW

        if score >= 6:
            return RiskLevel.HIGH
        if score >= 3:
            return RiskLevel.MEDIUM
        return RiskLevel.LOW


def _jev_summary(jev: JevDecisionSet) -> str:
    """One-line, human-readable summary of a JEV decision set."""
    if not jev.jev_called:
        return "JEV was not consulted"
    parts = []
    for decision in jev.decisions:
        confidence = f" p={decision.probability:.2f}" if decision.probability is not None else ""
        parts.append(f"{decision.question}={decision.value} ({decision.status}{confidence})")
    return "; ".join(parts) or "no decisions returned"


def explain(config: ExcelPilotConfig | None = None) -> dict[str, Any]:
    """Describe the effective policy, for ``excelpilot policy explain``."""
    effective = config or ExcelPilotConfig()
    return {
        "rules": rule_catalog(),
        "hard_deny_rules": [rule.rule_id for rule in HARD_DENY_RULES],
        "escalation_rules": [rule.rule_id for rule in ESCALATION_RULES],
        "thresholds": effective.policy.model_dump(mode="json"),
        "config_fingerprint": effective.fingerprint(),
        "notes": [
            "Hard deny rules cannot be disabled by configuration.",
            "JEV may raise the requirement for approval but can never lower it.",
            "requires_approval = policy_requires OR jev_escalates",
        ],
    }


__all__ = ["ALL_RULES", "PolicyEngine", "explain"]
