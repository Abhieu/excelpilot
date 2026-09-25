# ADR 0004 — JEV integration

- **Status:** Accepted
- **Date:** 2026-09-25

## Context

The specification requires JEV for structured decisioning, with the hard constraint
that **JEV must never mutate a workbook**. JEV's real API had to be investigated
before implementing; the example contracts in the specification are explicitly
described as conceptual and not the actual API.

### What the investigation established

Read from the authoritative source
`~/.local/share/uv/tools/jev-skill/lib/python3.13/site-packages/jev.py` (300 lines,
stdlib only), cross-checked against `github.com/wuyoscar/jev-skill`:

- Package `jev-skill` 0.2.0, MIT. Install: `uv tool install git+https://github.com/wuyoscar/jev-skill.git@v0.2.0`.
- **The locally installed CLI is broken.** `~/.local/bin/jev-decide` has a shebang
  pointing at `.../jev-skill/bin/python`, which does not exist:
  `bad interpreter: ... no such file or directory`. Running the module directly works.
- Request: `{model, state, questions}`. `questions` maps an id to
  `{type, instructions, criteria}` where type ∈ `choice` (2–255 criteria),
  `noul`, `score` (2–10 ordered).
- Response: `answers` keyed by id. `choice` → `{choice, probabilities, confidence}`;
  `noul` → `{noul}`; `score` → `{score, probabilities, legend}`.
- Confidence **is** available: per-label `probabilities`, `confidence`, and a
  derived `margin`.
- Interpretation: `status` ∈ `selected` | `needs_review` | `scored`.
- Exit codes: `0` selected, `2` some needs-review, `1` error. **No automatic retry.**
  Redirects refused. API keys never logged.
- Endpoints: OpenRouter `https://openrouter.ai/api/alpha/decisions`
  (model `typesafe/jev-1.13`); TypeSafe `https://api.typesafe.ai/v1/systemone`
  (model `jev-1.13.0`).
- Reserved review labels: `other, unknown, abstain, review, ask_user, wait, none,
  defer, insufficient_evidence`.
- Authority: the response carries `policy.executes_actions: false`. Upstream states
  *"None grants permission to execute an action."*

Verified without spending money: `jev.py setup` and `jev.py decide … --dry-run` make
zero network calls, and both were run.

Local credential state (presence only): **`TYPESAFE_API_KEY` is set,
`OPENROUTER_API_KEY` is not.** The CLI defaults to OpenRouter and never falls back,
so ExcelPilot must select `--provider typesafe` explicitly.

## Decision

### 1. ExcelPilot implements the HTTP contract directly

ExcelPilot does **not** shell out to `jev-decide`, and does not depend on `jev-skill`.

Reasons:
- The installed CLI is broken; depending on it would make the project non-functional.
- A subprocess boundary serialises untrusted workbook content through a temp file.
- The contract is small and stable: one POST, one JSON response.
- `jev-skill` is a collection of *skills and scenarios*, not a required runtime
  library — upstream itself says a separate CLI installation is optional.

### 2. `JevAdapter` protocol

```python
class JevAdapter(Protocol):
    def decide(self, ctx: DecisionContext) -> JevDecisionSet: ...
```

Implementations: `TypeSafeJevAdapter`, `OpenRouterJevAdapter` (both share
`HttpJevAdapter`), and `MockJevAdapter` for tests and offline runs.

### 3. JEV's vocabulary maps to ExcelPilot's decision questions

ExcelPilot asks four questions per run, mirroring the specification's conceptual
categories:

| Question id | Type | Criteria |
|---|---|---|
| `automation` | choice | `yes`, `approval_required`, `no` |
| `risk` | choice | `low`, `medium`, `high` |
| `interpretation` | choice | `sufficiently_clear`, `ambiguous`, `requires_user_input` |
| `verification` | choice | `structural_check`, `reconciliation`, `formula_validation`, `value_check`, `manual_review` |

These are ExcelPilot's *use* of the documented API, not a claim about Jev's schema.

### 4. The authority boundary is structural, not conventional

`JevDecision` is a frozen pydantic model whose fields are only
`question`, `value`, `probability`, `margin`, `status`. **There is no field that can
express a workbook mutation**, so a JEV response cannot be turned into an operation
even by a careless caller. `JevDecisionSet` is likewise incapable of being passed to
the executor, which accepts only `ExecutionPlan`.

JEV's output influences exactly two things:

1. **Policy input.** A `needs_review` or high-risk JEV result can *raise* scrutiny —
   it can make approval more likely. It can never lower a requirement. Policy computes
   `required = policy_requires OR jev_escalates`; the OR is deliberately asymmetric.
2. **Approval context.** The decision and its probability are shown to the human
   approver.

If JEV is unavailable, ExcelPilot proceeds with `jev_called: false` and records the
degradation. JEV is advisory, so its absence must not silently authorise anything —
policy still runs in full.

### 5. Provider selection is explicit, never automatic

`ExcelPilotConfig.jev_provider` defaults to `auto`, which resolves from *credential
presence* (`TYPESAFE_API_KEY` → `typesafe`, else `OPENROUTER_API_KEY` → `openrouter`,
else disabled). It never probes a network endpoint and never retries across providers,
matching JEV's own documented behaviour.

Live calls require `--allow-paid-calls`. Without it, any attempt to reach a real
provider raises `PaidCallBlocked` — the protection is in the code path, not a warning.

## Alternatives considered

| Option | Why rejected |
|---|---|
| Shell out to `jev-decide` | Installed CLI is broken; subprocess boundary; brittle path discovery |
| Depend on `jev-skill` as a library | It is a stdlib *script*, not an importable SDK with a stable API; couples ExcelPilot to a scenario-collection package |
| Vendor `jev.py` into the repo | Silently forks a third-party file; drifts from upstream; licensing/attribution burden |
| Treat JEV as optional and skip the layer | Loses the required decisioning stage and its audit value |

## Consequences

**Positive**
- Works regardless of the state of the local `jev-decide` install.
- Mock adapter makes every decisioning path testable offline and for free.
- The advisory boundary is enforced by the type system, not by discipline.
- Contract tests validate our request shape against the real `jev.py --dry-run`.

**Negative**
- If JEV changes its API, ExcelPilot's adapter must change. Mitigated by pinning the
  contract in `tests/test_jev_contract.py`, which fails loudly on drift.
- Reimplementing the contract means reimplementing its validation rules. Mitigated by
  validating responses strictly — a malformed JEV response is an error, never a
  silently-accepted partial result.

## Verification status

- `MockJevAdapter` and all decision-mapping logic: **tested**.
- Request/response contract: **tested**, including rejection of malformed responses
  and drift detection against real `jev.py` output.
- **Live JEV call: not yet made.** One is scheduled for Phase 8 with explicit user
  consent, using the TypeSafe provider (the only credential present). Its measured
  result will be recorded in `benchmarks/results.json`. No JEV result may be claimed
  before that.
