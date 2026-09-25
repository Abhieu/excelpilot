"""JEV adapter: a typed client for the verified JEV HTTP contract.

## What the investigation established

Read from the authoritative ``jev.py`` (``jev-skill`` 0.2.0, stdlib only) and
cross-checked against ``github.com/wuyoscar/jev-skill``:

* Request: ``{"model": str, "state": <text|object|array>, "questions": {id: {...}}}``
* Question: ``{"type": "choice"|"noul"|"score", "instructions": ..., "criteria": ...}``
* Response: ``{"answers": {id: {...}}}`` where a ``choice`` answer carries
  ``choice``, ``probabilities`` (label -> 0..1) and ``confidence``;
  ``noul`` carries ``noul``; ``score`` carries ``score``, ``probabilities`` and
  ``legend``.
* Endpoints: OpenRouter ``https://openrouter.ai/api/alpha/decisions`` (model
  ``typesafe/jev-1.13``); TypeSafe ``https://api.typesafe.ai/v1/systemone``
  (model ``jev-1.13.0``).
* No automatic retry. Redirects refused. Keys never logged.
* The response carries ``policy.executes_actions: false``; upstream states
  *"None grants permission to execute an action."*

## Why an adapter rather than a subprocess

The locally installed ``jev-decide`` is **broken** — its shebang points at a
Python that no longer exists. Rather than depend on it, ExcelPilot implements the
documented contract directly. See ADR-0004.

## The authority boundary

:class:`JevDecision` has no field capable of expressing a workbook mutation, so a
JEV response cannot become an operation. Its only influence is to make a run
*more* cautious (ADR-0005).
"""

from __future__ import annotations

import os
from typing import Any, Protocol

from app.contracts.config import JevConfig
from app.contracts.enums import JevProvider
from app.contracts.errors import InvalidDecisionError, PaidCallBlocked
from app.contracts.pipeline import (
    DecisionContext,
    DecisionQuestion,
    JevDecision,
    JevDecisionSet,
)
from app.decisions.questions import REVIEW_LABELS, build_questions, build_state
from app.net.http import HttpError, is_finite_number, post_json

#: Endpoint and default model per provider, from the verified contract.
ENDPOINTS: dict[JevProvider, tuple[str, str]] = {
    JevProvider.OPENROUTER: ("https://openrouter.ai/api/alpha/decisions", "typesafe/jev-1.13"),
    JevProvider.TYPESAFE: ("https://api.typesafe.ai/v1/systemone", "jev-1.13.0"),
}

#: Environment variable holding each provider's key. Presence is checked; the
#: value is never logged, printed, or written to an audit record.
KEY_ENV_VARS: dict[JevProvider, str] = {
    JevProvider.OPENROUTER: "OPENROUTER_API_KEY",
    JevProvider.TYPESAFE: "TYPESAFE_API_KEY",
}


class JevAdapter(Protocol):
    """What ExcelPilot needs from a decision service."""

    def decide(self, context: DecisionContext) -> JevDecisionSet:
        """Return advisory decisions. Never mutates anything."""
        ...

    @property
    def available(self) -> bool:
        """Whether this adapter can be used right now."""
        ...


def resolve_provider(config: JevConfig) -> JevProvider:
    """Resolve ``auto`` from credential **presence** only.

    Never probes a network endpoint and never falls back between providers after
    an error, matching JEV's own documented behaviour. Checking presence is not
    authentication: a key can be present and still be invalid or out of credit.
    """
    if not config.enabled or config.provider is JevProvider.DISABLED:
        return JevProvider.DISABLED
    if config.provider in {JevProvider.TYPESAFE, JevProvider.OPENROUTER}:
        return config.provider
    # AUTO: TypeSafe first, because the official route works without an
    # OpenRouter account, and the CLI defaults to OpenRouter which would fail
    # when only the TypeSafe key is present.
    if os.environ.get("TYPESAFE_API_KEY", "").strip():
        return JevProvider.TYPESAFE
    if os.environ.get("OPENROUTER_API_KEY", "").strip():
        return JevProvider.OPENROUTER
    return JevProvider.DISABLED


def build_request(
    questions: list[DecisionQuestion],
    state: dict[str, Any],
    model: str,
) -> dict[str, Any]:
    """Assemble the request in the documented shape."""
    return {
        "model": model,
        "state": state,
        "questions": {
            question.id: {
                "type": question.type,
                "instructions": question.instructions,
                "criteria": question.criteria,
            }
            for question in questions
        },
    }


def _as_float(value: Any) -> float:
    """Narrow an already-validated number to float, for the type checker."""
    return float(value)


def parse_response(
    payload: dict[str, Any],
    questions: list[DecisionQuestion],
    *,
    min_probability: float = 0.8,
    min_margin: float = 0.15,
    review_labels: frozenset[str] = REVIEW_LABELS,
) -> list[JevDecision]:
    """Parse a JEV response into typed decisions.

    Strict by design. A malformed response is an :class:`InvalidDecisionError`,
    never a silently-accepted partial result — a decision ExcelPilot cannot fully
    understand is a decision it must not act on.

    The status logic mirrors the verified ``build_report``:
    ``needs_review`` when the top probability is below the threshold, the margin
    over the runner-up is too small, or the chosen label is a reserved
    uncertainty label.
    """
    answers = payload.get("answers")
    if not isinstance(answers, dict):
        raise InvalidDecisionError("JEV response is missing an 'answers' object")

    decisions: list[JevDecision] = []
    for question in questions:
        answer = answers.get(question.id)
        if not isinstance(answer, dict):
            raise InvalidDecisionError(f"JEV response has no answer for question {question.id!r}")
        if answer.get("type") != question.type:
            raise InvalidDecisionError(
                f"JEV answer for {question.id!r} has type {answer.get('type')!r}, "
                f"expected {question.type!r}"
            )
        if question.type != "choice":
            raise InvalidDecisionError(
                f"question {question.id!r} is {question.type!r}; ExcelPilot only issues "
                f"choice questions, so this is a contract mismatch"
            )

        labels: set[str] = set(question.criteria)  # a dict, per DecisionQuestion
        probabilities = answer.get("probabilities")
        if not isinstance(probabilities, dict) or set(probabilities) != labels:
            raise InvalidDecisionError(
                f"JEV probabilities for {question.id!r} must cover exactly the candidate labels"
            )
        for value in probabilities.values():
            if not is_finite_number(value) or not 0 <= float(value) <= 1:
                raise InvalidDecisionError(
                    f"JEV probability for {question.id!r} is not a number in [0, 1]"
                )
        # JEV rounds, so a small tolerance is allowed — but not arbitrary weights.
        total = sum(_as_float(value) for value in probabilities.values())
        if abs(total - 1.0) > max(0.05, 0.0051 * len(labels)):
            raise InvalidDecisionError(
                f"JEV probabilities for {question.id!r} sum to {total:.3f}, expected ~1"
            )

        confidence = answer.get("confidence")
        if not is_finite_number(confidence) or not 0 <= _as_float(confidence) <= 1:
            raise InvalidDecisionError(
                f"JEV confidence for {question.id!r} is not a number in [0, 1]"
            )

        choice = answer.get("choice")
        if not isinstance(choice, str) or choice not in probabilities:
            raise InvalidDecisionError(
                f"JEV choice for {question.id!r} is not one of the candidate labels"
            )

        ranked = sorted(probabilities, key=lambda key: _as_float(probabilities[key]), reverse=True)
        probability = _as_float(probabilities[choice])
        # A choice that is not the highest-probability label is incoherent.
        if probability != _as_float(probabilities[ranked[0]]):
            raise InvalidDecisionError(
                f"JEV choice {choice!r} for {question.id!r} is not the highest-probability label"
            )
        margin = probability - _as_float(probabilities[ranked[1]])

        needs_review = (
            probability < min_probability
            or margin < min_margin
            or margin == 0
            or choice in review_labels
        )
        decisions.append(
            JevDecision(
                question=question.id,
                value=choice,
                status="needs_review" if needs_review else "selected",
                probability=probability,
                margin=margin,
                confidence=_as_float(confidence),
            )
        )
    return decisions


class HttpJevAdapter:
    """Calls a real JEV endpoint.

    Live calls cost money, so they require explicit authorisation. The gate is in
    the code path — ``allow_paid_calls=False`` raises rather than warns.
    """

    def __init__(
        self,
        config: JevConfig | None = None,
        *,
        allow_paid_calls: bool = False,
    ) -> None:
        self.config = config or JevConfig()
        self.allow_paid_calls = allow_paid_calls
        self.provider = resolve_provider(self.config)

    @property
    def available(self) -> bool:
        if self.provider is JevProvider.DISABLED:
            return False
        return bool(os.environ.get(KEY_ENV_VARS[self.provider], "").strip())

    def credential_status(self) -> dict[str, Any]:
        """Presence-only credential report. Never includes a key value."""
        return {
            "provider": self.provider.value,
            "configured": self.available,
            "key_env_var": KEY_ENV_VARS.get(self.provider),
            "note": "presence is not authentication or credit validation",
        }

    def decide(self, context: DecisionContext) -> JevDecisionSet:
        """Ask JEV and return typed decisions.

        Any failure returns a ``JevDecisionSet`` with ``jev_called: False`` and an
        ``error`` set, rather than raising. JEV is advisory, so its absence must
        not abort a run — but it must be recorded, because "JEV said yes" and
        "JEV was never asked" must never look the same in an audit trail.
        """
        if self.provider is JevProvider.DISABLED:
            return JevDecisionSet(
                jev_called=False,
                provider=JevProvider.DISABLED,
                error="no JEV provider configured or no credential present",
                min_probability=self.config.min_probability,
                min_margin=self.config.min_margin,
            )

        key_env = KEY_ENV_VARS[self.provider]
        api_key = os.environ.get(key_env, "").strip()
        if not api_key:
            return JevDecisionSet(
                jev_called=False,
                provider=self.provider,
                error=f"{key_env} is not set in the process environment",
                min_probability=self.config.min_probability,
                min_margin=self.config.min_margin,
            )

        if not self.allow_paid_calls:
            raise PaidCallBlocked(
                f"a live {self.provider.value} JEV call costs money and requires explicit "
                f"authorisation; pass --allow-paid-calls (or set allow_paid_calls) to permit it",
                service="jev",
                details={"provider": self.provider.value},
            )

        url, default_model = ENDPOINTS[self.provider]
        questions = build_questions(context)
        model = self.config.model or default_model
        payload = build_request(questions, build_state(context), model)

        try:
            response = post_json(
                url,
                payload,
                api_key=api_key,
                timeout=self.config.timeout_seconds,
                provider_name=self.provider.value,
                extra_headers={"X-OpenRouter-Title": "ExcelPilot"},
            )
        except HttpError as error:
            return JevDecisionSet(
                jev_called=False,
                provider=self.provider,
                model=model,
                error=str(error),
                min_probability=self.config.min_probability,
                min_margin=self.config.min_margin,
            )

        try:
            decisions = parse_response(
                response.data,
                questions,
                min_probability=self.config.min_probability,
                min_margin=self.config.min_margin,
                review_labels=frozenset(self.config.review_labels) | REVIEW_LABELS,
            )
        except InvalidDecisionError as error:
            return JevDecisionSet(
                jev_called=False,
                provider=self.provider,
                model=model,
                error=f"malformed JEV response: {error}",
                min_probability=self.config.min_probability,
                min_margin=self.config.min_margin,
            )

        return JevDecisionSet(
            decisions=decisions,
            jev_called=True,
            provider=self.provider,
            model=model,
            elapsed_seconds=round(response.elapsed_seconds, 6),
            min_probability=self.config.min_probability,
            min_margin=self.config.min_margin,
        )


class MockJevAdapter:
    """Deterministic adapter for tests, CI, and offline runs.

    Produces a fixed, plausible decision set with no network and no cost, so
    every decisioning path — including the escalation interaction with policy —
    is testable for free.

    ``scenario`` selects the shape of the answer, which is how the "JEV escalates"
    and "JEV does not escalate" branches are both exercised.
    """

    #: scenario -> (automation, risk, interpretation, verification, probability, margin)
    SCENARIOS: dict[str, tuple[str, str, str, str, float, float]] = {
        "confident": ("yes", "low", "sufficiently_clear", "structural_check", 0.93, 0.71),
        "approve": (
            "approval_required",
            "medium",
            "sufficiently_clear",
            "reconciliation",
            0.88,
            0.54,
        ),
        "risky": ("approval_required", "high", "ambiguous", "manual_review", 0.84, 0.42),
        "unsure": ("approval_required", "medium", "requires_user_input", "value_check", 0.61, 0.09),
        "veto": ("no", "high", "ambiguous", "manual_review", 0.91, 0.66),
    }

    def __init__(self, scenario: str = "approve") -> None:
        if scenario not in self.SCENARIOS:
            raise KeyError(f"unknown scenario {scenario!r}; available: {sorted(self.SCENARIOS)}")
        self.scenario = scenario

    @property
    def available(self) -> bool:
        return True

    def decide(self, context: DecisionContext) -> JevDecisionSet:
        automation, risk, interpretation, verification, probability, margin = self.SCENARIOS[
            self.scenario
        ]
        # A low margin produces needs_review, mirroring the real parser.
        status = "selected" if margin >= 0.15 and probability >= 0.8 else "needs_review"
        decisions = [
            JevDecision(
                question="automation",
                value=automation,
                status=status,
                probability=probability,
                margin=margin,
                confidence=probability,
            ),
            JevDecision(
                question="risk",
                value=risk,
                status=status,
                probability=probability,
                margin=margin,
                confidence=probability,
            ),
            JevDecision(
                question="interpretation",
                value=interpretation,
                status=status,
                probability=probability,
                margin=margin,
                confidence=probability,
            ),
            JevDecision(
                question="verification",
                value=verification,
                status=status,
                probability=probability,
                margin=margin,
                confidence=probability,
            ),
        ]
        return JevDecisionSet(
            decisions=decisions,
            jev_called=True,
            provider=JevProvider.MOCK,
            model="mock",
            elapsed_seconds=0.0,
        )


__all__ = [
    "ENDPOINTS",
    "KEY_ENV_VARS",
    "HttpJevAdapter",
    "JevAdapter",
    "MockJevAdapter",
    "build_request",
    "parse_response",
    "resolve_provider",
]
