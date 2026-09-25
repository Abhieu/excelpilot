"""JEV adapter tests.

Split into two halves:

1. **Offline** — the request shape, response parsing, escalation semantics, and
   the mock adapter. No network, no cost, runs in CI.
2. **Contract drift** — ExcelPilot's request is fed to the *real* ``jev.py``
   validator via ``--dry-run``, which makes no network call. If JEV's documented
   request shape ever changes, this test fails rather than the integration
   breaking silently in production.

The live-call test is marked ``live`` and excluded by default: it costs money and
requires explicit authorisation.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.contracts.config import JevConfig
from app.contracts.enums import JevProvider
from app.contracts.errors import InvalidDecisionError, PaidCallBlocked
from app.contracts.pipeline import DecisionContext, JevDecision, JevDecisionSet
from app.decisions import (
    ENDPOINTS,
    HttpJevAdapter,
    MockJevAdapter,
    build_questions,
    build_request,
    build_state,
    parse_response,
    resolve_provider,
)
from app.decisions.questions import REVIEW_LABELS

#: The installed jev-skill module, when present. Its presence is what makes the
#: contract-drift test meaningful rather than skipped.
JEV_MODULE = Path(
    "~/.local/share/uv/tools/jev-skill/lib/python3.13/site-packages/jev.py"
).expanduser()


@pytest.fixture
def context() -> DecisionContext:
    return DecisionContext(
        run_id="run-test",
        task_summary="Normalise customer names and remove duplicate invoices",
        sheet_count=3,
        sheets_affected=["Sales"],
        total_rows=1_000,
        cells_to_change=5_000,
        formulas_to_remove=2,
        structural_change=True,
        operation_kinds=["normalize_values", "remove_duplicates"],
    )


def _answer(probabilities: dict[str, float], choice: str, confidence: float = 0.9) -> dict:
    return {
        "type": "choice",
        "choice": choice,
        "probabilities": probabilities,
        "confidence": confidence,
    }


class TestRequestShape:
    def test_request_matches_documented_shape(self, context: DecisionContext) -> None:
        questions = build_questions(context)
        payload = build_request(questions, build_state(context), "jev-1.13.0")

        # Exactly the three documented top-level fields.
        assert set(payload) == {"model", "state", "questions"}
        assert payload["model"] == "jev-1.13.0"
        assert isinstance(payload["state"], dict)
        assert set(payload["questions"]) == {
            "automation",
            "risk",
            "interpretation",
            "verification",
        }
        for question in payload["questions"].values():
            # Exactly the three documented per-question fields.
            assert set(question) == {"type", "instructions", "criteria"}
            assert question["type"] == "choice"
            assert isinstance(question["criteria"], dict)
            assert 2 <= len(question["criteria"]) <= 255
            assert question["instructions"]

    def test_state_contains_facts_only_not_cell_data(self, context: DecisionContext) -> None:
        """Minimise what leaves the machine."""
        state = build_state(context)
        serialised = json.dumps(state)
        assert "cells" not in serialised.lower() or "cells_to_change" in serialised
        # No formulas, no cell values, no credentials.
        assert "=" not in serialised
        assert "key" not in serialised.lower()

    def test_criteria_counts_are_within_limits(self, context: DecisionContext) -> None:
        for question in build_questions(context):
            assert 2 <= len(question.criteria) <= 255

    def test_instructions_treat_state_as_evidence(self, context: DecisionContext) -> None:
        for question in build_questions(context):
            assert "not instructions" in question.instructions


class TestResponseParsing:
    def test_parses_a_well_formed_response(self, context: DecisionContext) -> None:
        questions = build_questions(context)
        answers = {
            "automation": _answer({"yes": 0.92, "approval_required": 0.06, "no": 0.02}, "yes"),
            "risk": _answer({"low": 0.9, "medium": 0.08, "high": 0.02}, "low"),
            "interpretation": _answer(
                {"sufficiently_clear": 0.88, "ambiguous": 0.1, "requires_user_input": 0.02},
                "sufficiently_clear",
            ),
            "verification": _answer(
                {
                    "structural_check": 0.85,
                    "reconciliation": 0.06,
                    "formula_validation": 0.04,
                    "value_check": 0.03,
                    "manual_review": 0.02,
                },
                "structural_check",
            ),
        }
        decisions = parse_response({"answers": answers}, questions)
        assert len(decisions) == 4
        assert all(d.status == "selected" for d in decisions)
        assert decisions[0].probability == pytest.approx(0.92)
        assert decisions[0].margin == pytest.approx(0.86)

    def test_low_probability_triggers_review(self, context: DecisionContext) -> None:
        questions = build_questions(context)
        answers = {
            q.id: _answer(dict.fromkeys(q.criteria, 1 / len(q.criteria)), next(iter(q.criteria)))
            for q in questions
        }
        decisions = parse_response({"answers": answers}, questions)
        assert all(d.status == "needs_review" for d in decisions)

    def test_narrow_margin_triggers_review(self, context: DecisionContext) -> None:
        questions = build_questions(context)
        answers = {
            "automation": _answer({"yes": 0.50, "approval_required": 0.49, "no": 0.01}, "yes"),
            "risk": _answer({"low": 0.9, "medium": 0.08, "high": 0.02}, "low"),
            "interpretation": _answer(
                {"sufficiently_clear": 0.9, "ambiguous": 0.08, "requires_user_input": 0.02},
                "sufficiently_clear",
            ),
            "verification": _answer(
                {
                    "structural_check": 0.9,
                    "reconciliation": 0.05,
                    "formula_validation": 0.02,
                    "value_check": 0.02,
                    "manual_review": 0.01,
                },
                "structural_check",
            ),
        }
        decisions = parse_response({"answers": answers}, questions)
        automation = next(d for d in decisions if d.question == "automation")
        assert automation.status == "needs_review"

    def test_reserved_uncertainty_label_triggers_review(self, context: DecisionContext) -> None:
        questions = build_questions(context)
        answers = {
            "automation": _answer(
                {"yes": 0.45, "approval_required": 0.53, "no": 0.02}, "approval_required"
            ),
            "risk": _answer({"low": 0.9, "medium": 0.08, "high": 0.02}, "low"),
            "interpretation": _answer(
                {"sufficiently_clear": 0.9, "ambiguous": 0.08, "requires_user_input": 0.02},
                "sufficiently_clear",
            ),
            "verification": _answer(
                {
                    "structural_check": 0.9,
                    "reconciliation": 0.05,
                    "formula_validation": 0.02,
                    "value_check": 0.02,
                    "manual_review": 0.01,
                },
                "structural_check",
            ),
        }
        # 'approval_required' is added to the review labels for this run.
        decisions = parse_response(
            {"answers": answers}, questions, review_labels=frozenset({"approval_required"})
        )
        automation = next(d for d in decisions if d.question == "automation")
        assert automation.value == "approval_required"
        assert automation.status == "needs_review"

    def test_confident_run_with_extra_review_label_still_selected(
        self, context: DecisionContext
    ) -> None:
        """A reserved label only forces review when it is actually chosen."""
        questions = build_questions(context)
        answers = {
            "automation": _answer({"yes": 0.92, "approval_required": 0.06, "no": 0.02}, "yes"),
            "risk": _answer({"low": 0.9, "medium": 0.08, "high": 0.02}, "low"),
            "interpretation": _answer(
                {"sufficiently_clear": 0.9, "ambiguous": 0.08, "requires_user_input": 0.02},
                "sufficiently_clear",
            ),
            "verification": _answer(
                {
                    "structural_check": 0.9,
                    "reconciliation": 0.05,
                    "formula_validation": 0.02,
                    "value_check": 0.02,
                    "manual_review": 0.01,
                },
                "structural_check",
            ),
        }
        decisions = parse_response(
            {"answers": answers}, questions, review_labels=frozenset({"approval_required"})
        )
        assert next(d for d in decisions if d.question == "automation").status == "selected"

    @pytest.mark.parametrize(
        "mutation",
        [
            pytest.param(lambda a: a.pop("risk"), id="missing-answer"),
            pytest.param(lambda a: a["automation"].pop("probabilities"), id="no-probabilities"),
            pytest.param(
                lambda a: a["automation"].update({"probabilities": {"yes": 1.0}}),
                id="wrong-labels",
            ),
            pytest.param(
                lambda a: a["automation"].update(
                    {"probabilities": {"yes": 0.5, "approval_required": 0.2, "no": 0.1}}
                ),
                id="does-not-sum-to-one",
            ),
            pytest.param(
                lambda a: a["automation"].update({"choice": "maybe"}), id="choice-not-a-label"
            ),
            pytest.param(
                lambda a: a["automation"].update({"choice": "no"}), id="choice-not-highest"
            ),
            pytest.param(
                lambda a: a["automation"].update({"confidence": 1.5}),
                id="confidence-out-of-range",
            ),
            pytest.param(
                lambda a: a["automation"].update({"confidence": "high"}),
                id="confidence-not-a-number",
            ),
            pytest.param(
                lambda a: a["automation"].update({"type": "noul"}), id="wrong-answer-type"
            ),
            pytest.param(
                lambda a: a["automation"].update(
                    {"probabilities": {"yes": float("nan"), "approval_required": 0.0, "no": 0.0}}
                ),
                id="nan-probability",
            ),
            pytest.param(lambda a: a["risk"].pop("choice"), id="missing-choice"),
        ],
    )
    def test_malformed_responses_are_rejected(
        self, context: DecisionContext, mutation: object
    ) -> None:
        """A response ExcelPilot cannot fully understand must be rejected.

        Never silently accepted as a partial result.
        """
        questions = build_questions(context)
        answers = {
            "automation": _answer({"yes": 0.92, "approval_required": 0.06, "no": 0.02}, "yes"),
            "risk": _answer({"low": 0.9, "medium": 0.08, "high": 0.02}, "low"),
            "interpretation": _answer(
                {"sufficiently_clear": 0.88, "ambiguous": 0.1, "requires_user_input": 0.02},
                "sufficiently_clear",
            ),
            "verification": _answer(
                {
                    "structural_check": 0.85,
                    "reconciliation": 0.06,
                    "formula_validation": 0.04,
                    "value_check": 0.03,
                    "manual_review": 0.02,
                },
                "structural_check",
            ),
        }
        mutation(answers)  # type: ignore[operator]
        with pytest.raises(InvalidDecisionError):
            parse_response({"answers": answers}, questions)


class TestProviderResolution:
    def test_auto_prefers_typesafe_when_only_its_key_is_present(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The CLI defaults to OpenRouter, so AUTO must not.

        With only TYPESAFE_API_KEY set, an OpenRouter default would fail with
        "Set OPENROUTER_API_KEY" on every call.
        """
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        monkeypatch.setenv("TYPESAFE_API_KEY", "present")
        assert resolve_provider(JevConfig(provider=JevProvider.AUTO)) is JevProvider.TYPESAFE

    def test_auto_uses_openrouter_when_only_its_key_is_present(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
        monkeypatch.setenv("OPENROUTER_API_KEY", "present")
        assert resolve_provider(JevConfig(provider=JevProvider.AUTO)) is JevProvider.OPENROUTER

    def test_auto_disables_when_no_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        assert resolve_provider(JevConfig(provider=JevProvider.AUTO)) is JevProvider.DISABLED

    def test_explicit_provider_is_respected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TYPESAFE_API_KEY", "present")
        assert (
            resolve_provider(JevConfig(provider=JevProvider.OPENROUTER)) is JevProvider.OPENROUTER
        )

    def test_disabled_stays_disabled(self) -> None:
        assert resolve_provider(JevConfig(provider=JevProvider.DISABLED)) is JevProvider.DISABLED

    def test_endpoints_and_models_match_the_contract(self) -> None:
        assert ENDPOINTS[JevProvider.OPENROUTER] == (
            "https://openrouter.ai/api/alpha/decisions",
            "typesafe/jev-1.13",
        )
        assert ENDPOINTS[JevProvider.TYPESAFE] == (
            "https://api.typesafe.ai/v1/systemone",
            "jev-1.13.0",
        )


class TestPaidCallGate:
    def test_live_call_blocked_without_authorisation(
        self, context: DecisionContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TYPESAFE_API_KEY", "a-key")
        adapter = HttpJevAdapter(JevConfig(provider=JevProvider.TYPESAFE), allow_paid_calls=False)
        assert adapter.available is True
        with pytest.raises(PaidCallBlocked) as info:
            adapter.decide(context)
        assert info.value.service == "jev"

    def test_disabled_provider_returns_degraded_result_without_raising(
        self, context: DecisionContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        adapter = HttpJevAdapter(JevConfig(provider=JevProvider.AUTO))
        result = adapter.decide(context)
        assert result.jev_called is False
        assert result.error
        assert result.decisions == []

    def test_credential_status_never_reveals_a_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TYPESAFE_API_KEY", "super-secret-key-value")
        adapter = HttpJevAdapter(JevConfig(provider=JevProvider.TYPESAFE))
        status = adapter.credential_status()
        assert status["configured"] is True
        assert "super-secret" not in json.dumps(status)
        assert status["key_env_var"] == "TYPESAFE_API_KEY"


class TestMockAdapter:
    @pytest.mark.parametrize("scenario", sorted(MockJevAdapter.SCENARIOS))
    def test_every_scenario_produces_four_decisions(
        self, context: DecisionContext, scenario: str
    ) -> None:
        result = MockJevAdapter(scenario).decide(context)
        assert result.jev_called is True
        assert len(result.decisions) == 4
        assert {d.question for d in result.decisions} == {
            "automation",
            "risk",
            "interpretation",
            "verification",
        }

    def test_confident_scenario_does_not_escalate(self, context: DecisionContext) -> None:
        assert MockJevAdapter("confident").decide(context).escalates is False

    def test_risky_scenario_escalates(self, context: DecisionContext) -> None:
        assert MockJevAdapter("risky").decide(context).escalates is True

    def test_veto_scenario_escalates(self, context: DecisionContext) -> None:
        assert MockJevAdapter("veto").decide(context).escalates is True

    def test_unsure_scenario_is_needs_review(self, context: DecisionContext) -> None:
        result = MockJevAdapter("unsure").decide(context)
        assert result.any_needs_review is True
        assert result.escalates is True

    def test_unknown_scenario_rejected(self) -> None:
        with pytest.raises(KeyError):
            MockJevAdapter("nonsense")

    def test_is_deterministic(self, context: DecisionContext) -> None:
        first = MockJevAdapter("approve").decide(context)
        second = MockJevAdapter("approve").decide(context)
        assert first.model_dump_json() == second.model_dump_json()


class TestJevCannotMutate:
    def test_decision_type_has_no_operation_field(self) -> None:
        """The structural guarantee behind 'JEV must not mutate workbooks'."""
        assert "operation" not in JevDecision.model_fields
        assert "operations" not in JevDecision.model_fields
        assert "target" not in JevDecision.model_fields
        assert "workbook" not in JevDecision.model_fields

    def test_decision_set_cannot_be_used_as_a_plan(self) -> None:
        """A JevDecisionSet is not an ExecutionPlan, so it cannot reach the executor."""
        from pydantic import ValidationError

        from app.contracts.pipeline import ExecutionPlan

        assert not isinstance(JevDecisionSet(), ExecutionPlan)
        with pytest.raises(ValidationError):
            ExecutionPlan.model_validate(JevDecisionSet().model_dump())

    def test_extra_fields_rejected(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            JevDecision.model_validate(
                {
                    "question": "automation",
                    "value": "yes",
                    "status": "selected",
                    "operation": "delete_sheets",
                }
            )


@pytest.mark.skipif(not JEV_MODULE.exists(), reason=f"jev.py not installed at {JEV_MODULE}")
class TestContractDriftAgainstRealJev:
    """Feed ExcelPilot's request to the real ``jev.py`` validator.

    ``--dry-run`` makes no network call and costs nothing, so this runs in CI on
    any machine with jev-skill installed. If JEV's request contract changes, this
    fails instead of the integration breaking in production.
    """

    def _run_validator(
        self, payload: dict[str, object], provider: str
    ) -> subprocess.CompletedProcess[str]:
        request_file = (
            Path(os.environ.get("TMPDIR", "/tmp")) / f"excelpilot-jev-contract-{provider}.json"
        )
        request_file.write_text(json.dumps(payload), encoding="utf-8")
        return subprocess.run(  # noqa: S603 - fixed argv, no shell
            [
                sys.executable,
                str(JEV_MODULE),
                "decide",
                str(request_file),
                "--provider",
                provider,
                "--dry-run",
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    @pytest.mark.parametrize("provider", ["typesafe", "openrouter"])
    def test_real_jev_accepts_our_request(self, context: DecisionContext, provider: str) -> None:
        url, default_model = (
            ENDPOINTS[JevProvider.TYPESAFE]
            if provider == "typesafe"
            else ENDPOINTS[JevProvider.OPENROUTER]
        )
        del url
        payload = build_request(build_questions(context), build_state(context), default_model)
        result = self._run_validator(payload, provider)
        assert result.returncode == 0, f"jev.py rejected our request: {result.stderr}"
        echoed = json.loads(result.stdout)
        assert set(echoed["questions"]) == set(payload["questions"])

    def test_jev_cli_setup_reports_no_credentials_in_output(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``setup`` checks presence only and must not print key values."""
        result = subprocess.run(  # noqa: S603
            [sys.executable, str(JEV_MODULE), "setup"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
            env={**os.environ, "TYPESAFE_API_KEY": "leak-canary-12345"},
        )
        assert result.returncode == 0
        assert "leak-canary-12345" not in result.stdout
        report = json.loads(result.stdout)
        assert report["jev_called"] is False
        assert report["requires_user_choice"] is True


@pytest.mark.live
class TestLiveJevCall:
    """A single live call, with explicit authorisation. Excluded by default.

    Run with: ``pytest -m live --allow-paid-calls`` after setting
    ``TYPESAFE_API_KEY``. The result is recorded in ``benchmarks/results.json``.
    """

    def test_live_typesafe_call(self, context: DecisionContext) -> None:
        adapter = HttpJevAdapter(JevConfig(provider=JevProvider.TYPESAFE), allow_paid_calls=True)
        result = adapter.decide(context)
        assert result.jev_called is True, result.error
        assert result.provider is JevProvider.TYPESAFE
        assert len(result.decisions) == 4
        for decision in result.decisions:
            assert decision.probability is not None
            assert 0 <= decision.probability <= 1
        assert not any(d.value in REVIEW_LABELS for d in result.decisions)
