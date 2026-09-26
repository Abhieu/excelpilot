"""Deterministic policy engine tests.

Policy is the sole authority on permission (ADR-0005). These tests cover:

* each hard-deny rule fires on the condition it exists for
* each escalation rule fires on its threshold
* deny wins over escalate
* escalation is sticky
* JEV can only ever *raise* scrutiny, never lower it
* the rules are pure: the same request always yields the same decision
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.contracts.config import ExcelPilotConfig
from app.contracts.enums import PolicyOutcome
from app.contracts.operations import (
    Aggregate,
    CreateWorksheet,
    NormalizeRules,
    NormalizeValues,
    ReadRange,
    Reconcile,
    ReconcileSpec,
    RemoveDuplicates,
    SetFormula,
    Target,
)
from app.contracts.pipeline import JevDecision, JevDecisionSet, PolicyRequest
from app.policy import PolicyEngine, explain, rule_catalog


def _request(**overrides: object) -> PolicyRequest:
    """A benign read-only request, overridable per test."""
    base: dict[str, object] = {
        "operations": [ReadRange(target=Target(sheet="Sales"))],
        "sheet_names": ["Sales"],
        "total_rows": 100,
        "total_cells": 1_000,
    }
    base.update(overrides)
    return PolicyRequest(**base)  # type: ignore[arg-type]


@pytest.fixture
def engine() -> PolicyEngine:
    return PolicyEngine()


class TestReadOnlyIsAllowed:
    def test_read_is_allowed(self, engine: PolicyEngine) -> None:
        decision = engine.evaluate(_request())
        assert decision.outcome is PolicyOutcome.ALLOW
        assert decision.allowed is True
        assert decision.requires_approval is False

    def test_reconcile_is_allowed(self, engine: PolicyEngine) -> None:
        spec = ReconcileSpec(
            name="total",
            actual=Aggregate(sheet="Sales", column="Amount", aggregation="sum"),
            tolerance=0.01,
        )
        decision = engine.evaluate(
            _request(operations=[Reconcile(target=Target(sheet="Sales"), checks=[spec])])
        )
        assert decision.outcome is PolicyOutcome.ALLOW


@pytest.mark.security
class TestHardDenyRules:
    def test_source_never_overwritten(self, engine: PolicyEngine) -> None:
        decision = engine.evaluate(
            _request(output_overwrites_source=True, requested_output_path="/tmp/a.xlsx")
        )
        assert decision.outcome is PolicyOutcome.DENY
        assert "source_never_overwritten" in decision.rule_ids

    def test_output_outside_workspace_denied(self, engine: PolicyEngine) -> None:
        decision = engine.evaluate(
            _request(
                requested_output_path="/etc/passwd",
                workspace_root=str(Path.cwd()),
            )
        )
        assert decision.outcome is PolicyOutcome.DENY
        assert "output_within_workspace" in decision.rule_ids

    def test_output_inside_workspace_allowed(self, engine: PolicyEngine) -> None:
        decision = engine.evaluate(
            _request(
                requested_output_path=str(Path.cwd() / "out.xlsx"),
                workspace_root=str(Path.cwd()),
            )
        )
        assert decision.outcome is PolicyOutcome.ALLOW

    def test_cell_ceiling_is_not_configurable_away(self) -> None:
        """A config that raises every soft threshold must not defeat the hard limit."""
        permissive = ExcelPilotConfig(
            policy={
                "cell_change_approval_threshold": 0,
                "deny_cells_affected_above": 20_000_000,
            }
        )
        engine = PolicyEngine(permissive)
        decision = engine.evaluate(
            _request(
                operations=[NormalizeValues(target=Target(sheet="Sales"), rules=NormalizeRules())],
                cells_affected=20_000_001,
            )
        )
        assert decision.outcome is PolicyOutcome.DENY
        assert "cell_ceiling" in decision.rule_ids

    def test_operation_ceiling(self, engine: PolicyEngine) -> None:
        operations = [ReadRange(target=Target(sheet=f"S{i}")) for i in range(60)]
        decision = engine.evaluate(_request(operations=operations))
        assert decision.outcome is PolicyOutcome.DENY
        assert "operation_ceiling" in decision.rule_ids

    def test_vba_workbook_write_denied(self, engine: PolicyEngine) -> None:
        decision = engine.evaluate(
            _request(
                operations=[NormalizeValues(target=Target(sheet="Sales"), rules=NormalizeRules())],
                has_vba=True,
            )
        )
        assert decision.outcome is PolicyOutcome.DENY
        assert "vba_read_only" in decision.rule_ids

    def test_vba_workbook_read_allowed(self, engine: PolicyEngine) -> None:
        decision = engine.evaluate(_request(has_vba=True))
        assert decision.outcome is PolicyOutcome.ALLOW

    def test_ambiguity_denied_rather_than_guessed(self, engine: PolicyEngine) -> None:
        decision = engine.evaluate(
            _request(ambiguity_signals=["no sheet named", "no columns named"])
        )
        assert decision.outcome is PolicyOutcome.DENY
        assert "no_guessing" in decision.rule_ids

    def test_deny_reasons_are_actionable(self, engine: PolicyEngine) -> None:
        decision = engine.evaluate(
            _request(
                operations=[NormalizeValues(target=Target(sheet="Sales"), rules=NormalizeRules())],
                has_vba=True,
            )
        )
        assert "macro" in decision.reasons[0].lower()


class TestEscalationRules:
    def test_bulk_change_requires_approval(self, engine: PolicyEngine) -> None:
        decision = engine.evaluate(
            _request(
                operations=[NormalizeValues(target=Target(sheet="Sales"), rules=NormalizeRules())],
                cells_affected=5_000,
            )
        )
        assert decision.outcome is PolicyOutcome.REQUIRE_APPROVAL
        assert "bulk_change" in decision.rule_ids
        assert decision.requires_approval is True

    def test_small_change_does_not_escalate(self, engine: PolicyEngine) -> None:
        decision = engine.evaluate(
            _request(
                operations=[NormalizeValues(target=Target(sheet="Sales"), rules=NormalizeRules())],
                cells_affected=5,
            )
        )
        assert decision.outcome is PolicyOutcome.ALLOW

    def test_formula_removal_requires_approval(self, engine: PolicyEngine) -> None:
        decision = engine.evaluate(
            _request(
                operations=[SetFormula(target=Target(sheet="Sales"), formulas=["=A1"])],
                formulas_removed=3,
            )
        )
        assert decision.outcome is PolicyOutcome.REQUIRE_APPROVAL
        assert "formula_removal" in decision.rule_ids

    def test_structural_change_requires_approval(self, engine: PolicyEngine) -> None:
        decision = engine.evaluate(
            _request(
                operations=[CreateWorksheet(name="Summary")],
                structural_change=True,
            )
        )
        assert decision.outcome is PolicyOutcome.REQUIRE_APPROVAL
        assert "structural_change" in decision.rule_ids

    def test_destructive_operation_requires_approval(self, engine: PolicyEngine) -> None:
        decision = engine.evaluate(
            _request(operations=[RemoveDuplicates(target=Target(sheet="Sales"))])
        )
        assert decision.outcome is PolicyOutcome.REQUIRE_APPROVAL
        assert "destructive_operation" in decision.rule_ids

    def test_hidden_sheet_change_requires_approval(self, engine: PolicyEngine) -> None:
        decision = engine.evaluate(
            _request(
                operations=[
                    NormalizeValues(target=Target(sheet="_Lookup"), rules=NormalizeRules())
                ],
                has_hidden_sheets=True,
            )
        )
        assert decision.outcome is PolicyOutcome.REQUIRE_APPROVAL
        assert "hidden_sheet_change" in decision.rule_ids

    def test_restricted_data_requires_approval(self, engine: PolicyEngine) -> None:
        decision = engine.evaluate(_request(sensitivity_level="restricted"))
        assert decision.outcome is PolicyOutcome.REQUIRE_APPROVAL
        assert "restricted_data" in decision.rule_ids

    def test_escalations_accumulate(self, engine: PolicyEngine) -> None:
        decision = engine.evaluate(
            _request(
                operations=[RemoveDuplicates(target=Target(sheet="_Lookup"))],
                has_hidden_sheets=True,
                sensitivity_level="confidential",
                formulas_removed=2,
                cells_affected=99_999,
            )
        )
        assert decision.outcome is PolicyOutcome.REQUIRE_APPROVAL
        assert len(decision.rule_ids) >= 4
        assert "bulk_change" in decision.rule_ids
        assert "formula_removal" in decision.rule_ids
        assert "destructive_operation" in decision.rule_ids
        assert "hidden_sheet_change" in decision.rule_ids
        assert "restricted_data" in decision.rule_ids


@pytest.mark.security
class TestDenyWinsOverEscalate:
    def test_deny_short_circuits(self, engine: PolicyEngine) -> None:
        """A deny must not be softened because an escalation also fired."""
        decision = engine.evaluate(
            _request(
                operations=[RemoveDuplicates(target=Target(sheet="Sales"))],
                has_vba=True,  # would escalate
                output_overwrites_source=True,  # hard deny
            )
        )
        assert decision.outcome is PolicyOutcome.DENY
        assert "source_never_overwritten" in decision.rule_ids
        assert "destructive_operation" not in decision.rule_ids


@pytest.mark.security
class TestJevIsAdvisoryOnly:
    def test_jev_cannot_override_a_deny(self, engine: PolicyEngine) -> None:
        confident = JevDecisionSet(
            decisions=[
                JevDecision(
                    question="automation",
                    value="yes",
                    status="selected",
                    probability=0.999,
                    margin=0.99,
                ),
                JevDecision(
                    question="risk", value="low", status="selected", probability=0.999, margin=0.99
                ),
            ],
            jev_called=True,
        )
        decision = engine.evaluate(_request(output_overwrites_source=True), jev=confident)
        assert decision.outcome is PolicyOutcome.DENY
        assert decision.rule_ids == ["source_never_overwritten"]

    def test_jev_can_escalate_an_otherwise_allowed_run(self, engine: PolicyEngine) -> None:
        worried = JevDecisionSet(
            decisions=[
                JevDecision(question="risk", value="high", status="selected", probability=0.8)
            ],
            jev_called=True,
        )
        decision = engine.evaluate(_request(), jev=worried)
        assert decision.outcome is PolicyOutcome.REQUIRE_APPROVAL
        assert decision.jev_escalated is True

    def test_jev_needs_review_escalates(self, engine: PolicyEngine) -> None:
        unsure = JevDecisionSet(
            decisions=[JevDecision(question="automation", value="yes", status="needs_review")],
            jev_called=True,
        )
        decision = engine.evaluate(_request(), jev=unsure)
        assert decision.outcome is PolicyOutcome.REQUIRE_APPROVAL

    def test_jev_absent_does_not_authorise_anything_extra(self, engine: PolicyEngine) -> None:
        decision = engine.evaluate(_request(), jev=None)
        assert decision.outcome is PolicyOutcome.ALLOW
        assert decision.jev_escalated is False

    def test_jev_denial_still_denied(self, engine: PolicyEngine) -> None:
        veto = JevDecisionSet(
            decisions=[
                JevDecision(question="automation", value="no", status="selected", probability=0.9)
            ],
            jev_called=True,
        )
        decision = engine.evaluate(
            _request(
                operations=[NormalizeValues(target=Target(sheet="Sales"), rules=NormalizeRules())],
                cells_affected=5,
            ),
            jev=veto,
        )
        assert decision.outcome is PolicyOutcome.REQUIRE_APPROVAL


@pytest.mark.security
class TestPurity:
    def test_same_request_same_decision(self, engine: PolicyEngine) -> None:
        request = _request(
            operations=[RemoveDuplicates(target=Target(sheet="Sales"))],
            cells_affected=50,
        )
        first = engine.evaluate(request)
        second = engine.evaluate(request)
        assert first.outcome == second.outcome
        assert first.rule_ids == second.rule_ids
        assert first.reasons == second.reasons

    def test_decision_records_config_fingerprint(self, engine: PolicyEngine) -> None:
        decision = engine.evaluate(_request())
        assert decision.config_fingerprint == engine.config.fingerprint()

    def test_evaluated_facts_are_recorded(self, engine: PolicyEngine) -> None:
        decision = engine.evaluate(_request(cells_affected=42))
        assert decision.evaluated_facts["cells_affected"] == 42


class TestRiskClassification:
    def test_read_only_is_low_risk(self, engine: PolicyEngine) -> None:
        """A read cannot damage data, so its risk is LOW by definition."""
        assert engine.risk_level(_request(cells_affected=50_000)) == "low"

    def test_bulk_change_is_higher_risk(self, engine: PolicyEngine) -> None:
        """50,000 changed cells on public data is MEDIUM, not LOW."""
        risk = engine.risk_level(
            _request(
                operations=[NormalizeValues(target=Target(sheet="Sales"), rules=NormalizeRules())],
                cells_affected=50_000,
            )
        )
        assert risk == "medium"

    def test_risk_is_monotonic_in_magnitude(self, engine: PolicyEngine) -> None:
        """More cells changed must never be *less* risky than fewer.

        Asserted as non-decreasing rather than strictly increasing: several
        magnitudes legitimately share a risk band, and the property that matters
        is that risk never goes *down* as the blast radius grows.
        """

        def risk_for(cells: int) -> int:
            return engine.risk_level(
                _request(
                    operations=[
                        NormalizeValues(target=Target(sheet="Sales"), rules=NormalizeRules())
                    ],
                    cells_affected=cells,
                )
            ).rank

        ranks = [risk_for(c) for c in (1, 100, 2_000, 20_000, 200_000)]
        assert ranks == sorted(ranks)
        assert ranks[0] < ranks[-1]

    def test_restricted_data_is_high_risk(self, engine: PolicyEngine) -> None:
        risk = engine.risk_level(
            _request(
                operations=[RemoveDuplicates(target=Target(sheet="Sales"))],
                sensitivity_level="restricted",
                formulas_removed=10,
                structural_change=True,
            )
        )
        assert risk == "high"

    def test_small_change_to_public_data_is_low_risk(self, engine: PolicyEngine) -> None:
        risk = engine.risk_level(
            _request(
                operations=[NormalizeValues(target=Target(sheet="Sales"), rules=NormalizeRules())],
                cells_affected=3,
            )
        )
        assert risk == "low"


class TestExplain:
    def test_explain_lists_rules_and_thresholds(self) -> None:
        payload = explain()
        assert payload["rules"]
        assert payload["hard_deny_rules"]
        assert payload["thresholds"]
        assert "config_fingerprint" in payload

    def test_hard_deny_rules_are_documented_as_fixed(self) -> None:
        payload = explain()
        assert any("cannot be disabled" in note for note in payload["notes"])

    def test_catalog_covers_every_rule(self) -> None:
        catalog = rule_catalog()
        ids = {entry["id"] for entry in catalog}
        assert {"source_never_overwritten", "cell_ceiling", "bulk_change"} <= ids
        assert all(entry["kind"] in {"deny", "escalate"} for entry in catalog)
