# ADR 0005 — Deterministic policy engine

- **Status:** Accepted
- **Date:** 2026-09-25

## Context

The specification's central safety claim is that the *application*, not the LLM and
not JEV, owns authorisation. Several independent actors propose actions:

- the user (via natural language),
- the deterministic planner or the LLM (as an `ExecutionPlan`),
- JEV (as a `JevDecisionSet`).

Only one of them may grant permission, and that one must be reproducible and
inspectable. A prompt saying "you are authorised to proceed" must be worth exactly
nothing.

## Decision

`app/policy` is a **pure, deterministic function** of typed facts. No network, no
model, no clock-dependent branching, no randomness.

```python
evaluate(request: PolicyRequest) -> PolicyDecision
```

`PolicyRequest` carries only facts derivable from the workbook, the plan, and the
config: operation type, target sheet/range, sensitivity classification, workbook size,
cells affected, formulas added/removed, structural impact, ambiguity, and configured
thresholds.

`PolicyDecision` yields one of `allow`, `require_approval`, or `deny`, plus a
machine-readable list of the `RuleId`s that fired and human-readable reasons.

## Why pure and deterministic

A policy that can be influenced by a model's opinion is not a policy. Determinism
gives three properties the specification demands:

1. **Reproducibility** — the same workbook plus the same plan always yields the same
   decision, so tests assert exact outcomes and the audit trail is meaningful.
2. **Testability** — every rule is a pure predicate with table-driven tests.
3. **Explainability** — a decision cites the rule ids that produced it.

## Rule ordering

Rules evaluate in a fixed order and **deny wins over approve**. The first matching
rule in each class determines the outcome:

1. **Hard deny rules.** Structural destruction (`delete_sheet` on a protected sheet),
   writes outside the sandbox, operations exceeding absolute limits, write attempts
   against a workbook that failed inspection. These cannot be overridden.
2. **Escalation rules.** Bulk cell modification, formula removal, row-count change,
   cross-sheet reconciliation, sheet creation. These set `require_approval`.
3. **Default.** `allow`.

`require_approval` is the sticky state: once any escalation fires, approval is
required. A later `allow` cannot clear it.

## The JEV relationship is deliberately asymmetric

```
required_approval = policy_requires_approval OR jev_escalates
```

JEV can **raise** scrutiny. JEV can never lower it. There is deliberately no
`policy_allows_because_jev_said_so` path. This inverts the usual risk of an advisory
model in a control loop, where a confident wrong answer suppresses a human.

Concretely: if policy says `deny`, the run is denied even if JEV returns
`automation: yes` with probability 0.99. If policy says `allow` but JEV returns
`risk: high` or `status: needs_review`, approval is required anyway.

## Defence in depth

The executor re-checks policy at execution time rather than trusting the gate that
ran earlier. This catches a plan that was mutated between approval and execution,
and makes the executor safe to call directly from tests or the dashboard.

`tests/test_architecture.py` enforces by static import analysis that `app/policy`
imports neither `app/planner` nor `app/decisions` nor any provider — the rule engine
physically cannot consult a model.

## Configuration

Thresholds live in `ExcelPilotConfig` and are overridable via
`excelpilot.toml` or `--config`, but:

- `deny` rules are **not** configurable. A configuration file cannot grant
  permission that the hard-deny set forbids. Raising a threshold is possible;
  deleting a deny rule is not.
- Every effective threshold is recorded in the run's audit record, so a run is
  interpretable against the config that produced it.

## Alternatives considered

| Option | Why rejected |
|---|---|
| LLM-as-judge for authorisation | Unreproducible, prompt-injectable, and the exact failure mode the specification prohibits |
| JEV as the policy engine | JEV returns `executes_actions: false` by design; it is a classifier, not an authoriser |
| Permissions matrix in the workbook itself | Interesting, but workbook files are untrusted input; a malicious workbook could carry its own permissive rules |
| No policy engine; rely on the approval gate | Then a single mis-click authorises anything, and automation is impossible without a human |

## Consequences

**Positive**
- Authority is provably not model-influenceable.
- Every decision is explainable by rule id.
- Rules are cheap to test exhaustively.
- The same policy protects CLI, dashboard, and any future API surface.

**Negative**
- A pure function cannot learn. Thresholds are hand-tuned. This is deliberate for v1:
  a learned policy is unreproducible, which defeats the purpose.
- Rule set is necessarily incomplete. Unknown situations are handled by the default
  (`allow`) for read-only work, and by escalation for anything that mutates. The
  conservative default for *unrecognised mutating operations* is
  `require_approval`, not `allow`.
