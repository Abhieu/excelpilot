# ADR 0003 — Model provider abstraction and the deterministic planner

- **Status:** Accepted
- **Date:** 2026-09-25

## Context

The specification requires that ExcelPilot turn a natural-language operational
request into a structured `ExecutionPlan`, and that it not be hard-coded to one LLM
vendor.

Measured constraint: **no LLM credential exists in this environment.**
`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, and `OPENROUTER_API_KEY` are all unset. Only
`TYPESAFE_API_KEY` is present, and that serves JEV, not general chat.

A system whose headline capability ("ask for a workbook change in English") is
unrunnable without a purchased key is not demonstrable, not testable in CI, and not
honest about what it does.

## Decision

Two distinct layers, with the deterministic one as the default.

### 1. `Planner` protocol

```python
class Planner(Protocol):
    def plan(self, task: UntrustedText, inspection: WorkbookInspection) -> ExecutionPlan: ...
```

Implementations:

| Implementation | Role | Default |
|---|---|---|
| `DeterministicPlanner` | Compiles natural language into a typed plan using workbook facts and explicit grammar rules. No network. | **Yes** |
| `LLMPlanner` | Wraps any `ModelProvider`, requests strict JSON, and validates the reply into `ExecutionPlan`. | No |

### 2. `ModelProvider` protocol

```python
class ModelProvider(Protocol):
    def complete(self, request: ModelRequest) -> ModelResponse: ...
```

Implementations: `OpenAICompatibleProvider` (works with OpenAI, OpenRouter,
vLLM, Ollama's OpenAI-compatible surface, LM Studio) and
`AnthropicCompatibleProvider`. Selected by URL, not by a hard-coded vendor switch.

## The trust rule

`LLMPlanner` output is **untrusted input**. It is:

1. Requested as strict JSON.
2. Parsed with `json.loads` under a size cap.
3. Validated by pydantic into `ExecutionPlan`.
4. **Rejected** — not repaired, not guessed — if validation fails.
5. Additionally constrained: every referenced sheet and range must exist in the
   inspection the model was given. A hallucinated sheet name fails validation.
6. Passed through policy before anything executes.

The `UntrustedText` wrapper makes the boundary visible in the type system: workbook
content and user text are not `str`, and no function accepts `str` where untrusted
content is expected.

## Why a deterministic planner is not a cop-out

For the operation set ExcelPilot supports — normalize, dedupe, sort, filter, validate,
summarise, reconcile, create/rename sheet, write formulas — the mapping from request
to plan is genuinely finite. A rule-based compiler is:

- **Correct** — no hallucinated sheet names or ranges.
- **Deterministic** — the same request always yields the same plan, so tests assert
  exact plans.
- **Free and offline** — CI needs no key.
- **Auditable** — the rules are readable source, not a prompt.

The LLM earns its place on genuinely ambiguous requests. When `DeterministicPlanner`
finds the request under-specified it does **not** guess; it returns
`InterpretationStatus.REQUIRES_USER_INPUT` with the specific missing facts, which is
also the honest input to a human approval gate.

## Consequences

**Positive**
- The project clones, installs, tests, and demos with zero credentials.
- The headline workflow is exercisable in CI and in the benchmark suite.
- LLM output can never widen authority: it produces a *plan*, and a plan still has to
  pass deterministic policy.

**Negative — stated plainly**
- The deterministic planner covers a bounded grammar. Novel phrasings are rejected
  rather than guessed. This is a deliberate precision/recall trade.
- **The LLM path is implemented and contract-tested but has never been run against a
  live provider here.** It must not be described as working. Tracked in
  `docs/limitations.md`.

## Verification status

`LLMPlanner` is covered by `tests/test_planner.py` using recorded provider responses —
including malformed JSON, schema-violating JSON, and a hallucinated sheet name. The
provider HTTP layer is covered by `tests/test_net_http.py` against a local stub server.
Live-provider execution is **unverified**.
