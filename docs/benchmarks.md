# Benchmarks

What was measured, how, and — more importantly — what the numbers do not
establish.

**Measured result** and **interpretation** are labelled separately throughout.
The first is what a command produced. The second is what someone concluded from
it, and is qualified where the measurement cannot carry the conclusion.

---

## Purpose

The specification asked for a comparison across three configurations. The point
is to measure what each layer *contributes*, and to catch correctness defects
that a test suite written alongside the code would not find.

It found three. That turned out to be the most valuable thing the benchmark did.

## The three modes

| Mode | Planner | JEV | What it isolates |
|---|---|---|---|
| `baseline` | none — the plan is truncated to one operation | disabled | the cost of the machinery around a fixed plan |
| `no_jev` | deterministic | disabled | the value of the planning layer |
| `with_jev` | deterministic | mocked | the full pipeline |

`baseline` is **not a strawman**. It is the *floor*: if the full pipeline cost
substantially more than a hand-built plan and bought nothing measurable, that
would be a finding worth reporting, not a reason to hide the numbers.

Its exact construction matters for honesty: the baseline is the same
`DeterministicPlanner` with its plan truncated to the first operation. It
isolates the surrounding machinery, **not planning quality** — which this
benchmark cannot measure, because there is no reference answer to plan against.

---

## Methodology

### Scenarios

11 scenarios, defined in `benchmarks/scenarios.py`, each with a stated rationale
for why it is in the suite. They deliberately include cases where **refusing is
the correct answer**, because a benchmark that only rewards writing files would
be measuring the wrong thing.

| Scenario | Expectation | Why it is here |
|---|---|---|
| `normalise_only` | succeed | the ordinary case |
| `clean_and_summarise` | succeed | the central example end to end: normalise, dedupe, summarise. Check: duplicates removed exactly |
| `table_targeted` | succeed | a workbook using real Excel tables and defined names |
| `formulas_only_input` | succeed | a formula-dense workbook |
| `large_workbook` | succeed | volume |
| `hidden_sheet_target` | escalate | writing to a hidden internal sheet. Check: the hidden-sheet rule fired |
| `ambiguous_request` | refuse | not specific enough to act on |
| `unsupported_capability` | refuse | a capability that does not exist |
| `destructive_without_key` | refuse | a destructive request naming no key |
| `injection_in_request` | refuse | an injection attempt in the task text |
| `formula_damage_present` | `verify_fails` | the input already contains `#REF!`, a hard-coded replacement, a deleted formula, and an external reference. Check: damage detected |

### Four expectation kinds

A scenario expecting a refusal counts as met when the run was refused. A
scenario that *should* be caught by verification is a distinct case:

| Expectation | Met when |
|---|---|
| `succeed` | the run succeeded |
| `escalate` | policy returned `require_approval` or `deny` |
| `refuse` | the run was denied by policy or approval |
| `verify_fails` | a file **was written**, verification **rejected** it |

`verify_fails` is not a softer `refuse`. Policy let the run proceed, and
verification caught something the input already contained. Scoring both as
"refuse" would hide the difference between *ExcelPilot stopped it* and
*ExcelPilot checked it*. It also requires that a file was actually written, so a
run that produced no output and failed for an unrelated reason cannot be scored
as though verification had done its job.

### Repetitions

`--repeats 5` by default: 11 scenarios × 3 modes × 5 repeats = **165 runs**.

A single repeat gives no spread to compare against, so the timing comparison is
then reported as *not measurable* rather than printing a number that would be
quoted as evidence.

### Warmup

One scenario per mode runs first and is **discarded**.

Without it, the first mode measured absorbs the entire cold-start cost — lazy
imports, pydantic's first model compilation, the first recalculation-library
import. That made an early draft of this benchmark report the *baseline* as
slower than the full pipeline, which is not a result; it is an artefact of which
mode happened to run first.

Warmup runs are not written to the results.

---

## What is measured

Only things that can be measured honestly:

- wall-clock time per scenario, with spread
- the outcome, and whether it matched the scenario's expectation
- the correctness of the change: cells written, rows affected, formulas removed
- whether the source workbook was byte-identical afterwards
- policy decisions and the rule ids that fired
- JEV decisions and their stated confidence
- audit events written

**No quality score.** No model-judged ranking. A benchmark that assigns a number
to "how good" a workbook is, without a ground truth to compare against, is a
decoration. Where a scenario has a checkable property — exactly N duplicates
removed, damage detected — that property is asserted and recorded as a boolean.

Seven of the eleven scenarios carry a checkable property; the other four are
scored on their expectation alone. Hence 35 checks across 55 runs per mode, not
55. A run with no check to perform is not counted as a check passed.

### The source-safety measurement

Recorded on **every** run, not only on the scenario's own check: the source
file's SHA-256 is compared before and after. This is the property the entire
output-safety model rests on, so it is measured continuously rather than trusted
to a test.

A modification would be reported as a `CRITICAL` finding, on the grounds that the
output-safety model would be broken and no other number in the report would mean
anything.

---

## Measured results

From `benchmarks/results.json`. Python 3.13.15, Darwin arm64, recalculation
library available.

| | baseline | no_jev | with_jev |
|---|---:|---:|---:|
| Scenarios | 11 | 11 | 11 |
| Runs | 55 | 55 | 55 |
| **Expectations met** | **55/55** | **55/55** | **55/55** |
| **Checks passed** | **35/35** | **35/35** | **35/35** |
| **Source never modified** | **yes** | **yes** | **yes** |
| Outputs written | 35 | 35 | 35 |
| Mean duration (s) | 0.1343 | 0.1360 | 0.1328 |
| Spread (s) | 0.2345 | 0.2405 | 0.2381 |

### What this establishes

On these 11 scenarios, every mode did what the scenario said it should, every
checkable property held, and **in all 165 runs the source workbook was
byte-identical afterwards.**

### What this does not establish

Anything about accuracy, usefulness, reliability, or production readiness.
Eleven synthetic scenarios on small generated workbooks are not a workload. The
benchmark reports no quality score because there is no ground truth against
which to score one.

---

## Timing

### Measured result

Mean per-scenario duration differed between modes by at most 0.0032 s
(`baseline` 0.1343 s against `with_jev` 0.1328 s — a difference in the direction
opposite to the "each layer costs more" assumption), while run-to-run spread was
0.2345–0.2405 s per mode.

### Interpretation

**Timing differences between modes were not measurable under this benchmark
configuration.** The largest gap between any two modes' means is 0.0032 s,
roughly seventy times smaller than the run-to-run spread. **No performance
advantage or disadvantage is claimed** for any mode, including JEV.

The benchmark enforces this rather than asserting it. A timing finding is
reported only when the difference exceeds the combined spread of the two samples
being compared; otherwise the comparison is emitted as a caveat saying no
measurable difference was found. With a single repeat there is no spread at all,
and the comparison is reported as unmeasurable.

JEV was **mocked in all 165 runs**, so these figures represent orchestration
overhead only. No network latency, provider reliability, or cost is represented.
The one live call (1.404 s) is recorded separately under `live_jev` and is never
blended into these numbers.

### Other timing caveats

- Timings come from small synthetic workbooks on one machine. They are not a
  production performance claim.
- The planner is fully deterministic, so planner variance is zero and the
  planner is not a source of the spread.
- Real workbooks are far larger. A 37,883 × 120 sheet takes ~70 s to inspect and
  ~226 s to round-trip; see [testing.md](testing.md).

---

## Regression findings

The benchmark found three real defects that the test suite had not. This is the
part that justifies its existence — a benchmark that only reports timings would
not have earned its keep.

### 1. Underscore-prefixed sheet names were unreachable

Excel's own convention for internal sheets is a leading underscore: `_Lookup`,
`_Lists`, `_AuditState`. The planner required a capitalised whole-word match, and
`_` is not an uppercase letter — so a request naming a hidden internal sheet
never matched it and silently fell through to the largest visible sheet.

**The consequence was not a wrong error.** The request was applied to the *main
data sheet* instead, and the `hidden_sheet_change` policy rule **never fired**,
because the plan no longer referenced the hidden sheet. A safety check was
bypassed by a name matcher.

Fixed in `app/planner/deterministic.py` by accepting a non-capitalised
whole-word match for names that cannot plausibly be an operation word, guarded
by a stoplist of ordinary words that also name operations (`summary`, `data`,
`list`, …) so the original false positive — "create a summary by Region" matching
a sheet called `Summary` — stays prevented. Covered by
`tests/test_regressions.py::TestUnderscorePrefixedSheetNames`, which asserts both
that the hidden sheet is now reachable *and* that the escalation rule fires.

### 2. Verification failure semantics were conflated

One scenario expected a successful run on a workbook already containing `#REF!`,
a hard-coded replacement, a deleted formula, and an external reference. The
system correctly failed the run; the **expectation** was wrong.

A distinct `verify_fails` outcome was added, with a `_run_check` that verifies
damage was actually found, and an expectation check that requires a file to have
been written. The point is not bookkeeping: conflating "stopped it" with
"checked it" would let a system that refuses to verify score the same as one that
verifies and catches damage.

### 3. Cold-start ordering produced a false result

The baseline initially appeared *slower* than the full pipeline — impossible,
given it does strictly less work. It ran first and absorbed all cold-start cost.

Fixed with a discarded warmup pass, plus the spread-based comparison described
above, which now makes an unmeasurable difference report itself as
unmeasurable.

A fourth defect was found by the same discipline, while inspecting the outbound
payload before the live call: the JEV `verification` question told the model
ExcelPilot could not recalculate formulas, which had been false since
recalculation was integrated. See [jev.md](jev.md).

---

## Running it

```bash
make bench                                   # mocked; no paid calls
uv run python -m benchmarks.run --repeats 5  # explicit
uv run python -m benchmarks.run --only duplicate  # one scenario
uv run pytest -m benchmark                    # the harness's own tests
```

`make bench` makes **no paid calls**. A live call requires
`--allow-paid-calls` and is recorded under `live_jev`.

The harness has 30 tests of its own (`tests/test_benchmarks.py`), because a
benchmark that cannot fail is decoration. They assert that every scenario runs in
every mode, that the four expectations are scored as documented, that the
source-safety measurement is real rather than a constant, that a single repeat
cannot produce a timing claim, and that the paid-call gate is reachable through
exactly one explicit check.

## Reading the output

`benchmarks/results.json`:

```
generated_at                        when the run happened
python, platform                    the environment
recalculation_library_available      whether evaluation was possible
jev_scenario                        which mock scenario was used
repeats, warmup                     the methodology, recorded with the numbers
modes.<name>.summary                aggregates per mode
modes.<name>.results                every individual run
interpretation.findings             what the measurements support
interpretation.caveats              what they do not
live_jev                            the single authorised live call, isolated
```
