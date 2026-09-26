# ExcelPilot

**An AI Excel operations engine that treats a spreadsheet as a system to be
changed safely, not a document to be edited.**

Give ExcelPilot a plain-language request. It inspects the workbook, builds a
structured plan, asks JEV whether the change is one that should proceed
unattended, evaluates deterministic policy, asks a human when policy says the
change needs one, executes against a copy, verifies what it actually did, and
writes a versioned output with a full audit trail.

The original workbook is never modified.

---

## What ExcelPilot is

A pipeline with a hard separation between the parts that understand intent and
the part that is allowed to touch cells:

- **Workbook understanding** — sheets, headers, data types, formulas, hidden
  sheets, tables, sensitivity
- **Structured planning** — a natural-language request becomes a typed,
  validated `ExecutionPlan`, or an explicit refusal
- **JEV decisioning** — an external decision service is asked whether the
  change is one that should proceed unattended
- **Deterministic policy** — thirteen rules, six of which cannot be disabled
- **Dry run** — measure a change without writing anything or spending anything
- **Human approval** — required whenever policy escalates, independent of JEV
- **Deterministic execution** — thirteen typed operations, no model involved
- **Reconciliation** — totals recomputed in Python, never asked of a model
- **Verification** — structural, data, formula, and recalculation checks
- **Change tracking** — a content-based diff and a change manifest
- **Versioned output** — a new file, written atomically, never over the source
- **Auditability** — append-only JSONL, one directory per run, replayable
  read-only

## What ExcelPilot is *not*

- **Not "ChatGPT for Excel."** Nothing in the pipeline asks a language model
  whether a number looks right. Every number is computed in Python from the
  workbook's own cells.
- **Not an autonomous agent.** There is no loop, no self-directed retry, no
  tool that grants itself permission. The pipeline is a fixed state machine with
  a transition table that raises on an illegal move.
- **Not a system where an LLM mutates workbooks.** A model cannot write a cell.
  Its output is parsed into a typed plan, and the executor accepts nothing but a
  validated `ExecutionPlan`. A model that emits a plan the policy denies is
  denied.
- **Not a replacement for Excel.** ExcelPilot is a library and a CLI. It is
  compatible with Excel's file format; it is not a spreadsheet application.
- **Not fully Excel-compatible.** Formula recalculation implements a *subset* of
  Excel, is an optional extra, and surfaces an unsupported function as an error
  rather than a wrong number. Charts, pivot tables, and slicers are not
  round-tripped.
- **Not a lossless workbook transformer.** openpyxl does not round-trip every
  OOXML part. Cell comments and drawings are lost from the **output**; ExcelPilot
  *detects* that and fails the run, but it does not prevent the loss. See
  [`docs/limitations.md`](docs/limitations.md) §3–4.
- **Not a VBA automation tool.** Macro-enabled workbooks can be read and
  preserved byte-for-byte. Writing to one is refused unconditionally, and
  authoring macros is out of scope entirely.
- **Not a statistically validated use of JEV.** Exactly one live JEV call has
  been made, to confirm the integration works. That is no evidence at all about
  accuracy, reliability, or calibration. See
  [`docs/jev.md`](docs/jev.md).

---

## Architecture

```
User request
      ↓
Workbook inspection          ←  read-only; the source is never opened for writing
      ↓
Task understanding           ←  intent, or an explicit "this is not specific enough"
      ↓
Plan                         ←  a validated ExecutionPlan, or a refusal
      ↓
JEV decision                 ←  advisory; can only raise scrutiny
      ↓
Deterministic policy         ←  the only authority on permission
      ↓
Approval if required         ←  a human, not a model
      ↓
Deterministic spreadsheet executor   ←  the only component that mutates cells
      ↓
Validation / reconciliation  ←  totals recomputed from the data
      ↓
Verification                 ←  independent of the executor
      ↓
Versioned output             ←  a new file, written atomically
      ↓
Change manifest / audit      ←  append-only JSONL, replayable
```

### The boundaries, and why they are structural

Each boundary below is enforced by types and by an enforced import graph, not
by convention. `tests/test_architecture.py` fails the build if one is crossed.

| Boundary | How it is enforced |
|---|---|
| **Cell content is data, never instructions** | Carried as `UntrustedText`, structurally separated from prompts, size-capped per provenance, and scanned before any model call |
| **Model output is untrusted input** | Parsed into typed contracts; rejected on validation failure; a malformed plan is a refusal, not a best guess |
| **JEV cannot mutate** | `JevDecision` has no field capable of expressing a mutation, and `JevDecisionSet` cannot be passed to the executor, which accepts only an `ExecutionPlan` |
| **JEV cannot authorise** | `requires_approval = policy_requires OR jev_escalates`. There is deliberately no de-escalation path |
| **Policy is deterministic** | No model, no network, no clock, no randomness. Same inputs, same decision, every time |
| **The executor re-checks** | Policy is re-evaluated at execution time, and every operation is re-validated, so a plan that was approved cannot smuggle an operation past the engine |
| **Verification is independent** | A run cannot be marked successful because a file was written. The verifier reads the output; the executor cannot influence its verdict |

---

## Quickstart

```bash
git clone <your-fork-or-clone-url> ExcelPilot
cd ExcelPilot
make install          # uv sync --extra dev
```

Recalculation is an optional extra. Without it, verification still runs and says
plainly that it was static:

```bash
uv sync --extra recalc        # adds `formulas`, enabling real evaluation
```

Everything below works with **no credentials of any kind**. JEV and the LLM
planner are both optional.

### The full workflow, as run

```bash
D=/tmp/excelpilot-demo && mkdir -p $D

# A workbook to work on. Or use any .xlsx / .xlsm of your own.
uv run python -c "
import sys; sys.path.insert(0, '.')
from pathlib import Path; from fixtures.workbooks import build
build('monthly_sales', Path('$D/sales.xlsx'), rows=60)"

TASK="normalise the Customer column and remove duplicate invoices"

# 1. Inspect — read-only, costs nothing
uv run excelpilot inspect $D/sales.xlsx

# 2. Plan — reads, decides, writes nothing, costs nothing
uv run excelpilot plan $D/sales.xlsx -t "$TASK"

# 3. Dry run — measure exactly what would change
uv run excelpilot run $D/sales.xlsx -t "$TASK" --dry-run -w $D

# 4. Without approval, nothing happens (exit code 4)
uv run excelpilot run $D/sales.xlsx -t "$TASK" -w $D; echo "exit=$?"

# 5. Approve and run — versioned output, atomic write
uv run excelpilot run $D/sales.xlsx -t "$TASK" --approve -w $D

# 6. The output is a new file; the source is untouched
ls $D/*.xlsx
#   sales.xlsx                     the original, unchanged
#   sales__run-<run-id>.xlsx       the versioned output

# 7. Diff, by content rather than by bytes
uv run excelpilot diff $D/sales.xlsx $D/sales__run-<run-id>.xlsx

# 8. Re-verify independently
uv run excelpilot verify $D/sales__run-<run-id>.xlsx -w $D

# 9. The audit trail
ls $D/.excelpilot/runs/<run-id>/
head $D/.excelpilot/runs/<run-id>/audit.jsonl

# 10. Replay — reconstructs what happened, re-executes nothing
uv run excelpilot replay <run-id> -w $D
```

The run id is printed by every command, and `excelpilot runs -w $D` lists recent
runs with their ids. Note that the run id already begins with `run-`, and it
appears verbatim in the output filename.

### What a run directory contains

```
.excelpilot/runs/run-<16 hex>/
├── audit.jsonl           append-only event log, one JSON object per line
├── manifest.json         the change manifest
├── report.txt            the human-readable report
├── run.json              the run record
├── source.snapshot.xlsx  a byte copy of the source, taken before any change
├── verification.json     every check and its verdict
└── output/               the run's artefact directory
```

The **versioned workbook itself is written next to its source**
(`sales__run-<run-id>.xlsx`), not into the run directory — so it sits beside the
file it came from and can be opened, diffed, or replaced directly. The run
directory holds the record *about* the change: what was requested, what was
decided, what was written, and whether it verified.

### Development

```bash
make check              # format-check, lint, typecheck, fast tests — the pre-commit gate
make test               # the full suite
make test-slow          # long-running tests against very large real workbooks
make test-security      # security boundary tests
make test-e2e           # end-to-end scenario tests
make smoke              # end-to-end developer smoke test (add --keep to inspect)
make bench              # the benchmark suite; mocked, makes no paid calls
```

---

## Verified results

Four kinds of evidence appear below, and they are **not** the same kind of claim:

| Section | What it is | Reproducible? |
|---|---|---|
| [Test suite](#test-suite) | Automated tests in this repository | Yes — `make test` |
| [Benchmark](#benchmark) | A recorded measurement, kept in `benchmarks/results.json` | Yes, but minutes long; deliberately not run per-PR |
| [Live JEV](#live-jev-integration) | **One** authorised call to a live service | **No** — it cost money and is not repeated |
| [Status](#status) and [limitations](docs/limitations.md) | What is *not* established | — |

The full record is in [`docs/benchmarks.md`](docs/benchmarks.md) and
[`docs/verification-report.md`](docs/verification-report.md). Future work is in
[`docs/roadmap.md`](docs/roadmap.md).

### Test suite

| Measure | Result | Command |
|---|---:|---|
| Tests passing | **581** | `make test` |
| Skipped | 1 | |
| Ruff | clean, 78 files | `make lint` |
| mypy (strict) | clean, 58 source files | `make typecheck` |
| Security boundary tests | 100 | `make test-security` |
| End-to-end tests | 34 | `make test-e2e` |
| Slow real-workbook tests | 13, in 275 s | `make test-slow` |
| Benchmark harness tests | 36 | `uv run pytest -m benchmark` |
| Pre-commit gate | 547 passed | `make check` |

### Benchmark

The benchmark runs **11 scenarios** across **3 modes**, **5 repeats** each —
**165 runs**. It compares:

1. **baseline** — a hand-built plan, no planner, no JEV
2. **no_jev** — the deterministic planner, JEV disabled
3. **with_jev** — the full pipeline

| Result | baseline | no_jev | with_jev |
|---|---:|---:|---:|
| Scenarios | 11 | 11 | 11 |
| Runs | 55 | 55 | 55 |
| Expectations met | 55/55 | 55/55 | 55/55 |
| Checkable properties held | 35/35 | 35/35 | 35/35 |
| **Source workbook unmodified** | **yes** | **yes** | **yes** |

What this establishes: on these scenarios, each mode did what the scenario said
it should, every checkable property held, and **in all 165 runs the source
workbook was byte-identical afterwards**. The last row is the safety property
the whole output model rests on, and it is measured on every run rather than
inferred from a test.

What this does **not** establish: anything about accuracy, usefulness, or
production readiness. Eleven synthetic scenarios are not a workload. The
benchmark reports no quality score, because there is no ground truth against
which to score one.

### Timing

**Timing differences between modes were not measurable under this benchmark
configuration.** Observed run-to-run spread (approximately 0.8–1.1 s) exceeded
the differences between modes, so **no performance advantage or disadvantage is
claimed** for any mode, including JEV.

The benchmark reports this as unmeasurable rather than printing a number that
would be quoted as if it were evidence. JEV was mocked in all 165 runs, so no
network latency, provider reliability, or cost is represented in those figures.

### The benchmark found real bugs

The benchmark was used as a correctness instrument, not a scoreboard. It found
three defects that the test suite had not:

**Hidden and internal sheet resolution.** Excel's own convention for internal
sheets is a leading underscore (`_Lookup`, `_Lists`, `_AuditState`). The planner
required a capitalised whole-word match, and `_` is not an uppercase letter — so
a request naming a hidden internal sheet never matched it and silently fell
through to the largest visible sheet. The consequence was not a wrong error: the
**hidden-sheet escalation rule never fired**, because the plan no longer
referenced the hidden sheet. A safety check was bypassed by a name matcher. Fixed
and covered by regression tests.

**Verification failure semantics.** One scenario expected a successful run on a
workbook already containing `#REF!`. The system was right and the expectation
was wrong: verification failed the run, which is the correct behaviour. A
distinct `verify_fails` outcome was added so that *ExcelPilot stopped it* and
*ExcelPilot performed verification and detected failure* are never conflated,
and so a verification failure is not scored as a mere refusal.

**Cold-start ordering.** The baseline initially appeared *slower* than the full
pipeline, which is impossible given it does strictly less work. The baseline ran
first and absorbed all cold-start cost. A discarded warmup pass now runs before
any measurement.

### Live JEV integration

One live JEV call was made, with explicit authorisation, requiring
`--allow-paid-calls`. It is an **integration validation artifact, not a
benchmark sample**: it is excluded from every aggregate, comparison, success
rate, and timing figure above, and is recorded separately under `live_jev` in
[`benchmarks/results.json`](benchmarks/results.json).

| | |
|---|---|
| Provider | TypeSafe (`https://api.typesafe.ai/v1/systemone`) |
| Model | `jev-1.13.0` (the endpoint's documented default) |
| Latency | 1.404 s |
| Result | Four decisions returned, no error |
| Selected | `automation=approval_required` (p=0.97), `risk=medium` (p=0.97) |
| Needs review | `interpretation=requires_user_input` (p=0.50), `verification=value_check` (p=0.50) |

Two of the four came back `needs_review` at 0.5 probability. That is the
interesting result, not the interesting-looking one: any `needs_review` raises
scrutiny, so this answer would **escalate rather than authorise**.

A single call establishes that the integration works end to end against a live
provider. It establishes nothing about accuracy, reliability, or latency
distribution, and no such claim is made.

### Live-call privacy

Only aggregate metadata was transmitted. Before sending, the payload was
rendered and checked; it contains counts, sheet names, booleans, operation
kinds, and the runtime's capabilities. It does **not** contain cell values,
formulas, cell addresses, row data, file paths, or credentials. See
[`docs/jev.md`](docs/jev.md) for the exact field list.

---

## Privacy and security

Described by behaviour, not by aspiration. Full detail in
[`docs/security.md`](docs/security.md).

- **Your workbook stays local.** Cell values, formulas, and row data are never
  sent to any model or service. The JEV payload is built by an explicit
  allowlist of fields, not by redacting a full workbook dump.
- **Cell content is untrusted data.** Cell text, sheet names, and comments are
  carried as `UntrustedText` — a distinct type, structurally separated from
  prompts, capped per provenance, and scanned for instruction-like content
  before any model call. A sheet named `=cmd|'/c calc'!A1` is data.
- **Model output is untrusted input.** It is parsed into typed contracts and
  rejected on validation failure. A model cannot widen its own permissions; the
  policy engine reads the plan, not the prose.
- **JEV receives no mutation authority.** Structurally: `JevDecision` has no
  field that could express a mutation, and the executor will not accept a
  `JevDecisionSet`.
- **Policy is deterministic.** No model, no network, no clock, no randomness.
- **Originals are protected by default.** The source is never opened for
  writing. Output is a new versioned file, written atomically. Rollback is
  "discard the output", because nothing was overwritten.
- **Secrets are redacted at write time**, in logs, audit records, manifests, and
  error messages — not on read.
- **Path traversal is prevented.** Every output path is resolved through a
  sandbox and rejected if it leaves the workspace root, including via symlink
  or `..`.
- **Formula injection is addressed.** A string beginning `=`, `+`, `-`, or `@`
  written into a cell is neutralised so it cannot execute on open, and
  user-supplied sheet names are sanitised before becoming sheet names.
- **Unsafe operations are blocked or escalated** — a run that would remove
  formulas, change structure, touch a hidden sheet, or affect a very large
  number of cells requires a human.
- **Macro-enabled workbooks are read, never written.** ExcelPilot opens a
  `.xlsm`, reports `has_vba` truthfully, and preserves `vbaProject.bin`
  **byte-for-byte** through a read/save round trip — measured against a real
  152 KB VBA project. Every mutating operation is refused by the `vba_read_only`
  hard deny, which `--approve` does not override and no configuration can
  disable. Read-only operations are permitted. See
  [`docs/limitations.md`](docs/limitations.md) §3.

Six policy rules are **hard denies** that no configuration file can disable:
`source_never_overwritten`, `output_within_workspace`, `cell_ceiling`,
`operation_ceiling`, `vba_read_only`, `no_guessing`.

There are **no unsafe override flags**. There is no `--force`, no `--no-verify`,
no `--skip-policy`, no `--overwrite`, no `--in-place` — and a test asserts they
do not exist, because the absence of an escape hatch is only a real property if
something checks it.

---

## Documentation

| Document | What it covers |
|---|---|
| [docs/architecture.md](docs/architecture.md) | Components, dependency boundaries, state machine, data flow |
| [docs/security.md](docs/security.md) | Threat model, injection, traversal, secrets, authorisation |
| [SECURITY.md](SECURITY.md) | What to report, what is already known, what is out of scope |
| [docs/jev.md](docs/jev.md) | The JEV integration, adapter design, live-call opt-in, privacy boundary |
| [docs/cli.md](docs/cli.md) | Every command, with real output |
| [docs/configuration.md](docs/configuration.md) | Every setting, with defaults |
| [docs/benchmarks.md](docs/benchmarks.md) | Methodology, measured results, what the numbers do not show |
| [docs/testing.md](docs/testing.md) | Test layers and how to run them |
| [docs/limitations.md](docs/limitations.md) | **What ExcelPilot cannot do** — read this before trusting it |
| [docs/roadmap.md](docs/roadmap.md) | Post-baseline work, with the evidence each item would need |
| [RELEASE_NOTES.md](RELEASE_NOTES.md) | What this release does, and what it does not |
| [CHANGELOG.md](CHANGELOG.md) | Version history, including every defect found and fixed |
| [docs/implementation-plan.md](docs/implementation-plan.md) | The living build record, phase by phase |
| [docs/verification-report.md](docs/verification-report.md) | Every claim, traced to the command that produced it |
| [docs/adr/](docs/adr/) | Twelve architecture decision records, with the evidence behind each |

## Contributing and CI

`make check` is the pre-commit gate: format, lint, strict types, fast tests. CI
(`.github/workflows/ci.yml`) runs that plus the security tests, the E2E tests,
the benchmark **harness** tests, the smoke test, and a packaging/import/fixture
check on a single Python version.

CI deliberately does **not** run the 165-run benchmark or any live JEV call. The
benchmark's result is a recorded measurement rather than a pass/fail gate, and
running it per-PR would invite someone to "fix" a timing difference that was
already shown to be noise.

## Reporting a security problem

See [SECURITY.md](SECURITY.md). It records what is worth reporting, what is
already a documented limitation, and the fact that **no private reporting
channel exists yet**.

## Licence

MIT — see [LICENSE](LICENSE).

## Status

Working and tested. **Not production-hardened, and with one lossy transformation
you must know about before using it on a workbook with comments or drawings.**

- **Comments and drawings are lost in the output — but you are now told.**
  `openpyxl` does not round-trip every OOXML part. On real workbooks, cell
  **comments** (seven parts on one file) and **drawings** — including shapes
  bound to a macro, i.e. the button that runs it — are dropped on save. This is
  not macro-specific; an ordinary `.xlsx` is affected.

  Verification now compares the set of workbook parts before and after and
  **fails the run** if content disappeared, so a silent loss can no longer be
  reported as success. The content is still missing from the output file — that
  is an openpyxl limitation and is not fixed. What is fixed is that you are
  told, the run fails rather than passing, and the untouched source survives, so
  discarding the output loses nothing. Full detail in
  [`docs/limitations.md`](docs/limitations.md) §3–4.
- Formula recalculation implements a **subset** of Excel, and is optional.
- The LLM planner has never been run against a live provider.
- There is no authentication, and rollback is manual by design.

Read [`docs/limitations.md`](docs/limitations.md) before relying on any of this,
and the "Remaining risks" section of
[`docs/verification-report.md`](docs/verification-report.md) if you are deciding
whether to.
