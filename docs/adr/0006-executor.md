# ADR 0006 — Typed operations and the executor

- **Status:** Accepted
- **Date:** 2026-09-25

## Context

An LLM asked to "normalise the Customer column and remove duplicate records" will
happily emit prose, JSON with plausible-but-wrong field names, ranges that do not
exist, or a request to run arbitrary code. The executor is the component that actually
mutates user workbooks, so its input contract is the last line of defence.

## Decision

### 1. Operations are a closed, typed discriminated union

`WorkbookOperation` is a pydantic discriminated union keyed on `operation`. There is
no free-form `dict` parameter bag, no `**kwargs` escape hatch, and no
`operation: str` with dynamic dispatch.

```python
WriteCells(operation="write_cells", target=Target(...), values=CellValues(...))
NormalizeValues(operation="normalize_values", target=Target(...), rules=NormalizeRules(...))
RemoveDuplicates(operation="remove_duplicates", target=Target(...), keys=[...])
```

Pydantic's `extra="forbid"` on every model means an unknown field is a **validation
error**, not a silently ignored key. A hallucinated `colours: ["red"]` on
`normalize_values` fails to parse and the plan is rejected whole.

### 2. The operation set is deliberately small

v1 implements: read range, write range, set formula, create sheet, rename sheet,
sort, filter, remove duplicates, normalise values, apply validation, create summary,
compare, reconcile.

Charts and pivots are **not** implemented. A typed extension point exists, but there
is no half-working implementation. `docs/limitations.md` states this plainly.

### 3. Targets are resolved, never trusted

`Target` names a sheet and optionally a range or a table. The executor resolves it
against the live workbook before doing anything. A target that does not resolve is a
hard error — never a best-effort guess. `resolve_target()` returns actual coordinates
or raises `TargetResolutionError` listing what *was* found, which is far more useful
to an operator than a partial write.

### 4. The executor re-validates and re-checks policy

Even though the pipeline gated the plan, `execute_operation()` independently:

1. re-validates the operation against its pydantic model,
2. re-resolves the target against the workbook as it now exists,
3. re-evaluates policy,
4. only then mutates.

This is defence in depth against a plan mutated between approval and execution, and it
makes the executor safe to invoke directly from tests or the dashboard.

### 5. No dynamic code execution

There is no `eval`, no `exec`, no `compile`, and no shell-out from the executor. If a
request genuinely needs logic ExcelPilot does not model, the correct outcome is a
`UnsupportedOperationError` naming the missing capability. Shipping an interpreter
here would be the single largest hole in the threat model (spec §24).

### 6. Every mutation is recorded

Each executed operation returns a structured `OperationResult` — cells read, cells
written, formulas added/removed, rows affected, before/after values for the affected
range. These results feed the diff, the manifest, the verification plan, and the audit
trail. The executor does not itself decide whether the run succeeded.

## Why a union and not a plugin registry

A registry keyed by string is a plugin system, and the specification explicitly warns
against speculative plugin systems. The union gives the same extensibility with
compile-time exhaustiveness: adding a variant makes every `match`/`if` on the union
fail to type-check until handled. `tests/test_executor_registry.py` additionally
asserts that every variant has a handler *and* a policy rule, so a new operation
cannot be added with no authorisation path.

## Alternatives considered

| Option | Why rejected |
|---|---|
| `operation: str` + `params: dict` | Exactly the shape the specification warns against; no validation of parameters |
| Per-operation Python function calling from the model | Arbitrary code execution |
| Send the whole plan to an LLM to "apply" it | No verification, no determinism, no audit |
| pandas transformations | Destroys formulas, styles, and unrelated sheets (ADR-0001) |

## Consequences

**Positive**
- Malformed or hallucinated operations fail at parse time, before any I/O.
- Adding an operation is a compile error until fully handled.
- The executor is a pure, testable function of `(workbook, operation, config)`.

**Negative**
- Every new operation needs a new model variant, a handler, a policy rule, a dry-run
  preview, and tests. That friction is the feature.
- Closed union means ExcelPilot cannot do something it was not designed for. For an
  operations tool, that is the correct default.
