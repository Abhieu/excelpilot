# Testing

## Running

```bash
make check              # format-check + lint + typecheck + fast tests — the pre-commit gate
make test               # the full default suite
make test-fast          # unit + integration only
make test-slow          # long-running tests against very large real workbooks
make test-security      # security boundary tests
make test-e2e           # end-to-end scenario tests
make smoke              # end-to-end developer smoke test (add --keep to inspect)
make bench              # the benchmark suite; mocked, no paid calls
```

Raw pytest:

```bash
uv run pytest                          # default: not slow, not benchmark, not live
uv run pytest -m slow -v               # the large-workbook tests
uv run pytest -m security -v
uv run pytest -m benchmark
uv run pytest tests/test_jev_adapter.py::TestCapabilityHonestyInQuestions -v
```

The default selection **excludes** `slow`, `benchmark`, and `live`, so the
default suite needs no credential and never costs money. `--strict-markers` and
`--strict-config` are on: a typo in a marker is an error, not a silently
unselected test.

## Layers

| Layer | File(s) | Marker | What it establishes |
|---|---|---|---|
| Contracts | `test_contracts.py` | default | the typed vocabulary validates and rejects |
| Architecture | `test_architecture.py` | default | the dependency graph and trust boundaries hold |
| Workbook | `test_workbook.py` | default + `slow` | read, hash, limits, round-trip fidelity |
| Planner | `test_planner.py` | default | natural language → plan, and refusal |
| Policy | `test_policy.py` | default | every rule fires when it should |
| Executor | `test_executor.py` | default | every operation, and the registry's refusals |
| Verification | `test_verification.py` | default | every check family, including recalculation |
| JEV adapter | `test_jev_adapter.py` | default + `live` | the contract, parsing, gating, privacy |
| CLI | `test_cli.py` | default | every command, and every exit code, as a subprocess |
| End-to-end | `test_e2e.py` | `e2e` | the whole pipeline on real runs |
| Regressions | `test_regressions.py` | default | specific bugs that were found and fixed |
| VBA / macros | `test_vba.py` | default + `slow` | macro detection, byte-level preservation, the write refusal |
| Content loss | `test_content_loss.py` | default | snapshot extension, and detection of dropped OOXML parts |
| Audit attribution | `test_audit_attribution.py` | default | a security denial names the rule that fired |
| Benchmarks | `test_benchmarks.py` | `benchmark` | that the benchmark harness itself is sound |

## Architecture tests are not decoration

`test_architecture.py` enforces ten invariants statically, by reading the source
and the import graph rather than by exercising behaviour. If any is violated the
build fails:

1. `contracts` imports no internal module
2. `workbook` imports nothing above `contracts`
3. `executor` imports neither `planner` nor `decisions`
4. `policy` imports neither `planner` nor `decisions` nor `executor`
5. no module outside `planner` constructs a model request
6. no `eval`, `exec`, `compile`, or `__import__` anywhere
7. no `print` in library code
8. `JevDecision` has no field capable of expressing a workbook mutation
9. `JevDecisionSet` is not accepted by the executor
10. no unsafe CLI flag exists

Checks 6, 7, 9, and 10 are the ones that would otherwise erode quietly. A
`print` in library code corrupts `--json` for every caller. An `eval` would let
a crafted workbook value become code. Check 9 is the structural form of "JEV
cannot mutate workbooks". Check 10 exists because the natural next change is to
add `--force` "temporarily".

## Exit codes are tested as subprocesses

`test_cli.py` runs the real CLI as a subprocess and asserts the exit code, for
every outcome. This was not theoretical: an earlier implementation returned 0 for
everything, so a script could not tell a refusal from a verification failure.
Exit codes are now 0–7 and each is asserted.

## Regression tests

`test_regressions.py` collects every bug found while building the system, each
with a comment explaining what went wrong and why the test asserts what it does.
They are the most valuable tests in the suite, because each one corresponds to a
thing that was actually wrong.

Among them:

- a dedupe key of `[Region, Customer]` collapsing 38 of 42 rows
- a data-delimiter opening marker containing the closing marker
- the word "Sales" resolving to the Amount column
- "tidy it up a bit" normalising everything
- "trim whitespace from the notes" naming no column
- verification mutating the workbook it verified — indexing a worksheet by
  coordinate *creates* the cell
- a metrics column producing a false positive in column consistency
- `sk-ant-` keys mislabelled as `openai_key` in redaction
- a corrupt output raising out of the verifier instead of failing it
- **underscore-prefixed sheet names being unreachable, which bypassed the
  hidden-sheet escalation rule** — found by the benchmark
- **the synthetic VBA fixture proving only that the detector fired**, never that
  the bytes survive — replaced by a genuinely macro-enabled package plus
  byte-level preservation tests
- the standalone `verify` command ignoring the configured recalculation setting,
  so it under-reported against the run it was checking

## The slow tests

Opt-in, because they are genuinely slow:

```bash
make test-slow      # 13 passed in 275s
```

They run against a real 37,883 × 120 workbook with roughly 69,000 formulas. Two
facts they establish, and that nothing else can:

- **inspection scales acceptably** — ~70 s for that sheet
- **a round trip is lossless** — ~226 s, with all 69,221 formulas, 219 merged
  ranges, 18 data validations, 36 conditional formats, 16 defined names, and
  3 tables preserved

That round-trip result is the evidence behind
[ADR-0001](adr/0001-workbook-engine.md), the decision to use openpyxl as the sole
engine. It is also why the diff is content-based: bytes differ after every save
even when nothing changed.

## The benchmark's own tests

`test_benchmarks.py` — 35 tests, `benchmark` marker. A benchmark that cannot fail
is decoration, so the harness is tested like any other component: that every
scenario runs in every mode, that the four expectation kinds are scored as
documented, that the source-safety measurement is a real comparison rather than a
constant, that a single repeat cannot produce a timing claim, that the
paid-call gate is reachable through exactly one explicit check, and that a
recorded live call is never discarded by a routine re-run.

## Current results

| Selection | Result | Command |
|---|---:|---|
| Default | **581 passed**, 1 skipped, 49 deselected | `make test` |
| Pre-commit gate | 547 passed, 1 skipped, 83 deselected | `make check` |
| Security boundary | 100 passed | `make test-security` |
| End-to-end | 34 passed | `make test-e2e` |
| Benchmark harness | 36 passed | `uv run pytest -m benchmark` |
| Slow, real workbooks | 13 passed in 275 s | `make test-slow` |
| Ruff | clean, 78 files | `make lint` |
| mypy (strict) | clean, 58 source files | `make typecheck` |

The skipped test requires an optional capability not present in this
environment. The one warning is a CPython `ZipFile.__del__` GC-timing artefact
that appears only when the whole suite runs in one process; it is present at the
previous commit, no `ZipFile` leaks in the core read paths, and openpyxl alone
does not reproduce it. It is cosmetic.

## Real-workbook tests

Some tests run against **real** workbooks, because synthetic fixtures cannot
establish that openpyxl round-trips a file the size and complexity Excel actually
produces. Those workbooks are not in this repository — they are business data —
and no filesystem location is assumed.

Supply them by pointing `EXCELPILOT_REAL_WORKBOOKS` at a directory containing a
`manifest.json` that maps a **role** to a workbook path:

```json
{
  "large":           "path/to/a-big-workbook.xlsx",
  "wide":            "path/to/a-wide-workbook.xlsx",
  "macro_extension": "path/to/a-macro-enabled.xlsm",
  "macro_project":   "path/to/a-macro-enabled.xlsm"
}
```

```bash
export EXCELPILOT_REAL_WORKBOOKS=~/my-workbooks
uv run pytest -m slow
```

Every role is optional and every real-workbook test **skips** when its role is not
configured, so the suite passes on a machine with no such collection — which is
the case in CI. The roles name a *property* the workbook must have (large, wide,
macro-enabled, containing a real VBA project) rather than a particular file, so
the same tests work with any collection. See `fixtures/real.py`.

`make test-slow` runs them when configured. It is excluded from `make check` and
from CI because a very large sheet takes minutes to inspect and round-trip.

## Writing a test

- Prefer asserting the **behaviour that matters**, not an implementation detail.
  `test_mentions_word` tests the matcher directly, but the regression tests
  assert what the planner *plans* — which is what a bug would actually look like
  to a user.
- A test that passes for the wrong reason is worse than no test. Where a value
  depends on the environment, assert against the environment's real capability
  rather than hard-coding it — the bug in `test_passes_for_an_untouched_workbook`,
  which asserted `recalculated is False` and so encoded a defect as expected
  behaviour, is the example to avoid.
- If you find a bug, add it to `test_regressions.py` with the reasoning. A
  regression test without the reasoning is a test nobody will dare to delete when
  it next fails.
