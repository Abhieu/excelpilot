# Architecture

How ExcelPilot is put together, and why.

## The one-sentence version

A natural-language request becomes a typed `ExecutionPlan`; the plan is checked
by a deterministic policy engine; only a validated plan reaches an executor that
can mutate cells; and an independent verifier decides whether the result is
actually correct.

## Components

| Package | Responsibility | May mutate a workbook? |
|---|---|---|
| `app/contracts/` | The typed vocabulary. Enums, errors, operations, plans, results. No logic. | no |
| `app/workbook/` | Reading, inspecting, hashing, table/target resolution, resource limits | **no** — read-only by construction |
| `app/planner/` | Natural language → `ExecutionPlan`. Deterministic by default; an LLM adapter exists. | no |
| `app/decisions/` | The JEV adapter. Structured question in, typed decision set out. | no |
| `app/policy/` | Deterministic rules. The only authority on permission. | no |
| `app/executor/` | Typed operation dispatch | **yes** — the only such component |
| `app/verification/` | Structural, data, formula, recalculation, and anomaly checks | no |
| `app/diff/` | Content-based diff and change manifest | no |
| `app/audit/` | Append-only event log with redaction | no |
| `app/storage/` | Run store, artefacts, atomic versioned writes | no |
| `app/safety/` | Path sandbox, injection firewall, formula-injection guard, limits | no |
| `app/app/` | The run state machine. The sole composer. | no — it delegates |
| `app/cli/` | Typer CLI | no |
| `benchmarks/` | The benchmark suite | no |
| `fixtures/` | Deterministic workbook builders for tests and demos | no |

## Dependency direction

Enforced statically by `tests/test_architecture.py`, which fails the build if a
forbidden edge appears.

```
cli ─┐
     ├─> app ──> policy ─────> contracts
     │          executor ────> workbook ──> contracts
     │          verification ─> diff ─────> workbook
     ├─> decisions ──────────> contracts
     └─> planner ────────────> contracts
audit ─> contracts        storage ─> contracts
```

Forbidden, and tested for:

- `executor` must not import `planner`, `decisions`, or any model provider
- `policy` must not import `planner`, `decisions`, or `executor`
- `workbook` must not import anything above `contracts`
- `contracts` must not import any internal module
- no module outside `planner` may construct a model request
- no `eval`, no `exec`, and no `print` in library code

The last two matter more than they look. `eval`/`exec` would let a crafted
workbook value become code. `print` in library code would corrupt `--json`
output that a caller is parsing.

## Data flow

```
                      ┌──────────────────────────────────────┐
  workbook ──────────▶│ workbook/inspector                  │  read-only
                      │  sheets, headers, types, formulas,   │
                      │  hidden sheets, sensitivity, hash    │
                      └──────────────────┬───────────────────┘
                                         │ WorkbookInspection
                      ┌──────────────────▼───────────────────┐
  request ────────────▶│ planner/                            │  UntrustedText in
                      │  intent → operations, or a refusal   │  ExecutionPlan out
                      └──────────────────┬───────────────────┘
                                         │ ExecutionPlan
                      ┌──────────────────▼───────────────────┐
                      │ decisions/  (JEV)                    │  advisory only
                      │  structured questions → decisions     │
                      └──────────────────┬───────────────────┘
                                         │ JevDecisionSet
                      ┌──────────────────▼───────────────────┐
                      │ policy/                             │  deterministic
                      │  6 hard denies + 7 escalations       │
                      └──────────────────┬───────────────────┘
                                         │ PolicyDecision
                      ┌──────────────────▼───────────────────┐
                      │ approval gate (human, if escalated)  │
                      └──────────────────┬───────────────────┘
                                         │
                      ┌──────────────────▼───────────────────┐
                      │ executor/                           │  THE ONLY MUTATOR
                      │  re-validates, re-checks policy      │
                      └──────────────────┬───────────────────┘
                                         │ ExecutionState
                      ┌──────────────────▼───────────────────┐
                      │ safety/versioned_output + save_atomic│
                      └──────────────────┬───────────────────┘
                                         │ output path
                      ┌──────────────────▼───────────────────┐
                      │ verification/  (independent)        │
                      │  structural, data, formula, recalc,  │
                      │  anomalies                          │
                      └──────────────────┬───────────────────┘
                                         │ VerificationResult
                      ┌──────────────────▼───────────────────┐
                      │ diff/ + audit/ + storage/           │
                      └──────────────────────────────────────┘
```

## The state machine

`app/app/orchestrator.py` holds a `_TRANSITIONS` table mapping each `RunState`
to the states reachable from it. Advancing to a state not in that set raises
`RunStateError` — a bug that skipped approval would otherwise be invisible, and
this is where it becomes an error rather than a log line.

```
created ─▶ inspecting ─▶ understanding ─▶ planning ─▶ deciding ─▶ policy_check
                                                                       │
                                    ┌──────────────────────────────────┤
                                    ▼                                  ▼
                            awaiting_approval                     executing
                                    │                                  │
                          executing / rejected                      verifying
                                                                        │
                                                          completed / failed
```

The table as implemented:

| From | Reachable |
|---|---|
| `created` | `inspecting`, `failed` |
| `inspecting` | `understanding`, `failed` |
| `understanding` | `planning`, `failed` |
| `planning` | `deciding`, `failed` |
| `deciding` | `policy_check`, `failed` |
| `policy_check` | `awaiting_approval`, `executing`, `failed`, `rejected` |
| `awaiting_approval` | `executing`, `rejected`, `failed` |
| `executing` | `verifying`, `failed` |
| `verifying` | `completed`, `failed` |
| `completed`, `failed`, `rejected`, `rolled_back` | — terminal |

The approval gate is structural: `executing` is reachable from `policy_check`
only when policy permits, and from `awaiting_approval` only after a decision.
There is no transition that reaches `executing` while an approval is pending, so
skipping the gate is a `RunStateError` rather than a silent pass. `failed` is
reachable from every non-terminal state, so an error can never be swallowed.

## The AI boundary

There are exactly two places a model can be involved, and both are adapters
behind a protocol.

**The planner.** `Planner.plan(task, inspection) -> ExecutionPlan`. The
deterministic implementation is the default and needs no credentials. The LLM
adapter is implemented and contract-tested but was never run against a live
provider here — see [limitations.md](limitations.md).

Whatever produces the plan, the output is parsed into typed contracts. A model
that emits a malformed plan, an unknown operation, or a target that does not
exist gets a **refusal**, not a best guess. The `no_guessing` hard-deny rule
exists for exactly this.

**The JEV adapter.** `JevAdapter.decide(context) -> JevDecisionSet`. See
[jev.md](jev.md).

## The JEV boundary

`JevDecision` is:

```python
class JevDecision(ContractModel):
    question: str
    value: str
    status: str          # selected | needs_review | scored
    probability: float | None
    margin: float | None
    confidence: float | None
```

There is no field capable of expressing a workbook mutation. That is the
structural enforcement of "JEV must not mutate" (ADR-0004) — not a convention,
an absence of capability. `JevDecisionSet` cannot be passed to the executor,
which accepts only an `ExecutionPlan`. Passing one is a type error, not a
runtime check someone might forget.

JEV is combined with policy by an **asymmetric OR**:

```
requires_approval = policy_requires OR jev_escalates
```

JEV can make a run more cautious. It can never make one less cautious. There is
deliberately no de-escalation path — a model returning "yes, this is fine" must
not be able to override a policy denial. See ADR-0005.

## The policy boundary

Thirteen rules in `app/policy/rules.py`:

**Hard denies** — not configurable; no config file can grant a permission these
forbid:

| Rule | Forbids |
|---|---|
| `source_never_overwritten` | writing to the source workbook |
| `output_within_workspace` | an output path outside the workspace root |
| `cell_ceiling` | more cells than `deny_cells_affected_above` |
| `operation_ceiling` | more operations than `max_operations_per_plan` |
| `vba_read_only` | writing to a macro-enabled workbook |

`vba_read_only` is worth singling out. It is a **deny**, not an escalation,
because the risk cannot be assessed by the person approving it: openpyxl
preserves a VBA project but cannot reason about it, so a change could leave
macros referencing sheets or ranges that no longer exist. A human would be
approving something the tool cannot show them. Measured behaviour: the macro
project survives a round trip byte-identically, every mutating operation is
refused, and read-only operations proceed. See
[limitations.md](limitations.md) §3.
| `no_guessing` | proceeding on an ambiguous request |

**Escalations** — set `require_approval` when they fire, and configurable:

| Rule | Escalates on |
|---|---|
| `bulk_change` | more cells or rows than the thresholds |
| `formula_removal` | a formula removed or overwritten |
| `structural_change` | sheets added, removed, renamed, or reordered |
| `destructive_operation` | a destructive operation present |
| `hidden_sheet_change` | a hidden sheet written |
| `restricted_data` | the workbook looks sensitive |
| `ambiguous_task` | the plan carries missing information |

Policy is a pure function of the plan, the inspection, and the configuration. No
model, no network, no clock, no randomness. The same inputs give the same
decision every time, which is what makes it auditable.

The registry **refuses to register an operation with no policy rule**, so a new
operation cannot be added without deciding what it should be subject to.

## The executor

`app/executor/registry.py` maps each of the thirteen `WorkbookOperation` variants
to a handler. The handler receives an open workbook and mutates it in place;
saving is the orchestrator's decision, which keeps "executed" and "saved" as
separate, separately-reported facts.

The executor re-validates every operation and **re-evaluates policy at execution
time**. This is defence in depth: a plan approved under one inspection cannot
smuggle an operation past the engine if the workbook changed underneath it. That
is not theoretical — it is exactly what happened during development, when a run
was correctly denied at execution for an output path that had passed the earlier
check.

Thirteen operations:

`ReadRange`, `WriteRange`, `SetFormula`, `CreateWorksheet`, `RenameWorksheet`,
`SortRange`, `FilterRows`, `RemoveDuplicates`, `NormalizeValues`,
`ApplyValidation`, `CreateSummary`, `CompareWorkbooks`, `Reconcile`.

## Verification

Independent of the executor by construction. The verifier reads the output file
fresh; it is not handed the executor's claims.

Check families:

- **Structural** — the file opens, expected sheets present, nothing removed
  unexpectedly, visibility and dimensions intact, tables valid
- **Data** — the range is readable; row counts, null rates, duplicate rates
  compared against the source
- **Formula** — presence, column-pattern consistency, reference resolution,
  broken references, hard-coded replacements, deletions
- **Recalculation** — when the optional `recalc` extra is installed, the output
  is actually evaluated. This catches what static analysis cannot: a formula
  that evaluates to `#VALUE!` looks perfectly fine as text.
- **Anomalies** — row-count change, null-rate increase, duplicate-rate increase,
  large value changes, outliers, and planned-vs-actual divergence

The result carries two mutually exclusive flags:

| `recalculated` | `static_formula_checks` | Meaning |
|---|---|---|
| `True` | `False` | Formulas were evaluated. A stronger claim. |
| `False` | `True` | Static checks only. Real, but strictly weaker. |

A consumer can never read a static result as though it had been evaluated.

## Persistence

Append-only JSONL, one directory per run — inspectable with `cat` and `jq`, with
no database dependency (ADR-0007).

```
.excelpilot/runs/run-<16 hex>/
├── audit.jsonl           one JSON object per event
├── manifest.json         the change manifest
├── report.txt            the human-readable report
├── run.json              the run record
├── source.snapshot.xlsx  a byte copy of the source
├── verification.json     every check and its verdict
└── output/               the run's artefact directory
```

The snapshot is a **byte copy**, not a re-save. That distinction is load-bearing:
openpyxl drops `xl/sharedStrings.xml` and relocates comment parts on every save,
so a re-saved "snapshot" would differ from the original for reasons that have
nothing to do with the change. See [ADR-0009](adr/0009-diff-strategy.md).

Redaction happens at **write time**, not on read. A secret that reaches a log is
never written in the first place, so there is no window in which a secret sits on
disk in the clear.

## The UI

There is no dashboard. The planned `app/dashboard/` was deliberately not built:
it would need to re-implement the approval and policy gate to be trustworthy, and
a UI that bypasses either is worse than no UI. The CLI is the interface, and it
cannot skip a stage.

`docs/implementation-plan.md` describes the sequencing this decision came from.

## Why these choices

Each is recorded as an ADR with the evidence that motivated it. The ones most
worth reading before changing anything:

| ADR | Decision |
|---|---|
| [0001](adr/0001-workbook-engine.md) | openpyxl as the sole engine — verified lossless on a 69k-formula workbook |
| [0002](adr/0002-minimal-dependencies.md) | minimal dependencies; no automatic `.env` loading |
| [0004](adr/0004-jev-integration.md) | JEV behind an adapter calling the HTTP contract directly |
| [0005](adr/0005-policy-engine.md) | deterministic policy is the sole authority; JEV is advisory |
| [0009](adr/0009-diff-strategy.md) | content hashing, never byte equality |
| [0010](adr/0010-rollback.md) | safe-output versioning, not in-place rollback |
| [0011](adr/0011-verification.md) | verification independent of the executor |
| [0012](adr/0012-trust-boundaries.md) | the trust-boundary model |
