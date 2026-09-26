# Verification report

Every claim in the README and the other documents, traced to the command that
produced it. Recorded 2026-09-26 on Python 3.13.15, Darwin arm64.

Where a claim is **not** supported by a measurement here, it says so. That is the
point of this document.

## Final verification run

Every command below was executed. Results are quoted, not summarised from memory.

| Command | Result |
|---|---|
| `make lint` | `All checks passed!` · `78 files already formatted` |
| `make typecheck` | `Success: no issues found in 58 source files` |
| `make test` | **581 passed**, 1 skipped, 49 deselected (31.75 s) |
| `make check` | 547 passed, 1 skipped, 83 deselected (38.25 s) |
| `make test-security` | **100 passed** |
| `make test-e2e` | **34 passed** (4.65 s) |
| `uv run pytest -m benchmark` | **36 passed** |
| `make test-slow` | **13 passed** (274.88 s) |
| `make smoke` | exit 0 |

### ### The warnings

The default suite reports 9 warnings rather than 1. The 8 new ones are the same
pre-existing `ZipFile.__del__` artefact, now triggered more often because
`tests/test_vba.py` opens packages repeatedly. It is the same cosmetic GC-timing
issue described below, not a new defect, and it was not suppressed with a filter
— suppressing it would hide a real signal if one ever appeared.

The one original warning

A CPython `ZipFile.__del__` GC-timing artefact, which appears only when the whole
suite runs in one process. Investigated rather than ignored:

- It is **present at the previous commit** (`aabb25e`), so it was not introduced
  by this work.
- No `ZipFile` leaks in the core read paths — verified by tracking live handles
  after `build`, `inspect_workbook`, and `opened()`: zero in every case.
- openpyxl 3.1.5 alone does not reproduce it.
- All three `ZipFile` uses in `app/` are inside `with` blocks.

Cosmetic. Left in place rather than papered over with a warning filter.

## The single skipped test

Requires an optional capability not present in this environment. Skipped rather
than faked, so the count is honest.

## Benchmark

**Not re-run for this report.** The 165-run results in
[`benchmarks/results.json`](../benchmarks/results.json) are the authoritative
record and are reproduced here unchanged.

| | baseline | no_jev | with_jev |
|---|---:|---:|---:|
| Scenarios | 11 | 11 | 11 |
| Runs | 55 | 55 | 55 |
| Expectations met | 55/55 | 55/55 | 55/55 |
| Checks passed | 35/35 | 35/35 | 35/35 |
| Source never modified | yes | yes | yes |
| Mean duration (s) | 0.1343 | 0.1360 | 0.1328 |
| Spread (s) | 0.2345 | 0.2405 | 0.2381 |

**165 runs total. Timing differences were not measurable** — the largest gap
between any two modes' means is 0.0032 s, roughly seventy times smaller than the
run-to-run spread. No performance claim is made for any mode.

## Live JEV

One call, explicitly authorised, behind `--allow-paid-calls`.

| | |
|---|---|
| Executed | **yes**, once |
| Provider | TypeSafe |
| Endpoint | `https://api.typesafe.ai/v1/systemone` |
| Model sent | `jev-1.13.0` — the endpoint's documented default, since the config `model` is `null`. **No model value was invented.** |
| Latency | 1.404 s (adapter-reported 1.401205 s) |
| Result | four decisions, no error |
| Selected | `automation=approval_required` p=0.97 · `risk=medium` p=0.97 |
| Needs review | `interpretation=requires_user_input` p=0.50 · `verification=value_check` p=0.50 |

### Privacy boundary: confirmed before sending

The payload was rendered and checked mechanically before the call:

| Check | Result |
|---|---|
| no `=` (no formula) | pass |
| no cell-address pattern (`A1`, `B12`) | pass |
| no currency symbol | pass |
| no digit run longer than 6 | pass |
| no API-key material | pass |
| no environment variable names | pass |
| no cell values or header names | pass |
| no long free text | pass |

Payload: 4,045 bytes. `task_summary` was the literal string
`"ExcelPilot benchmark capability probe"`. Transmitted fields are an explicit
allowlist — counts, sheet names, booleans, operation kinds, and
`capabilities.recalculation_available`.

### Isolation: asserted, not assumed

The recording step asserted that `modes`, `interpretation`, and every top-level
metadata field were **byte-identical** before and after writing `live_jev`. A
regression test additionally asserts no run record contains `live_jev` or
`endpoint`, and that the live record states it was excluded from aggregates,
timing, and expectation counts.

### A defect this exposed, now fixed

`make bench` overwrote `live_jev` with `{"called": false}`, destroying the only
evidence a paid call had happened. Since such a call cannot be repeated without
fresh authorisation, that record is not reproducible — losing it loses the result
permanently. Fixed by carrying a recorded call forward verbatim on any run
without `--allow-paid-calls`, labelled so it cannot masquerade as a fresh
measurement. Covered by five regression tests.

## Documentation: command verification

Every command in `README.md` and `docs/cli.md` was executed before being
documented.

| Command | Verified |
|---|---|
| `make install`, `uv sync --extra recalc` | target exists; extras declared in `pyproject.toml` |
| fixture build one-liner | runs, writes the workbook |
| `excelpilot inspect` | runs; output quoted verbatim |
| `excelpilot plan` | runs; output quoted verbatim |
| `excelpilot run --dry-run` | runs; exit 0, nothing written |
| `excelpilot run` without `--approve` | **exit 4** |
| `excelpilot run --approve` | **exit 0**; 584 formulas recalculated |
| `excelpilot run --approve` with a workspace mismatch | **exit 1** — the executor re-checked the path and denied it |
| `excelpilot diff` | runs; output quoted verbatim |
| `excelpilot verify` | runs; output quoted verbatim |
| `excelpilot replay` | runs; output quoted verbatim |
| `excelpilot runs` | runs |
| `excelpilot policy` | runs; output quoted verbatim |
| `excelpilot gc` | runs |
| `excelpilot version` | `excelpilot 0.1.0` |
| `ls`/`cat` of a run directory | runs; layout documented as observed |

One documentation error was found and fixed by running the commands: the README
described the output as `sales__run-<run-id>.xlsx`, but the actual pattern is
`<stem>__<run-id>.xlsx`, so the documented form was wrong.

## Documentation: consistency audit

Searched all documentation and code for statements the implementation no longer
supports.

### Stale recalculation claims — all found and corrected

| Location | Was | Now |
|---|---|---|
| `excelpilot verify --help` | "ExcelPilot cannot evaluate Excel formulas, and never claims to" | states that formulas are evaluated when the `recalc` extra is installed, and that the result says which it did |
| `app/verification/formulas.py` | module docstring: "**static only**… cannot recalculate" | describes static checks as always-run, and the recalculation that is additional |
| `app/contracts/verification.py` | `recalculated` described as "Always False" | described as whether evaluation actually happened, and why the default is the weaker claim |
| `app/app/orchestrator.py` | reconciliation docstring: "``recalculated`` is always False: ExcelPilot cannot evaluate Excel formulas" | distinguishes in-execution reconciliation (reads cached values) from verification-time evaluation |
| `app/verification/verifier.py` | "always states… never recalculated" | "always states whether formulas were evaluated or only checked statically" |
| `app/executor/operations.py` | "ExcelPilot cannot recalculate" | states that execution does not evaluate, and that verification does |
| `docs/adr/0011-verification.md` | "**Neither** successfully recalculates… `recalc` extra is documented as not providing working recalculation" | marked **Superseded in part (Phase 6)**, original reasoning preserved, current behaviour documented |
| `docs/implementation-plan.md` §7.2 item 1 | "recalculation status is **not yet determined**" | **SUPERSEDED (Phase 6)** with the measured result |
| `tests/test_contracts.py` | `test_recalculated_is_always_false`, asserting a constraint that no longer exists | replaced with three tests of what is actually true: static is the default and is the weaker claim, the flags are mutually exclusive, and a result cannot be edited after construction |

### A stale claim with a real consequence

The JEV `verification` question told the model *"ExcelPilot cannot recalculate
Excel formulas"* — false since Phase 6. Found by rendering the exact outbound
payload before the live call. Fixed by deriving the wording from the run's
measured capability, with `state.capabilities` carrying the fact as evidence, and
tests asserting both wordings.

### Live-call claims

`docs/implementation-plan.md` §7.2 item 5 said no live call had been made.
Updated to **RESOLVED (Phase 8)** with the measured result, the authorisation,
and the explicit statement that it supports no accuracy or reliability claim.

### Other consistency checks

- No claim of JEV superiority anywhere.
- No timing claim beyond "not measurable".
- No privacy claim stronger than the payload allowlist supports.
- No absolute privacy claim: the README says "workbook data stays local" in the
  context of cell values, formulas, and row data, and the JEV section lists
  exactly what is transmitted.
- Rollback described consistently as "discard the output" in all five places it
  appears.
- Thirteen operations, six hard denies, seven escalations — each verified
  against the code, not counted by hand.

## Defects found and fixed during this phase

Six in total. Each has a regression test.

| Defect | Consequence | Fix |
|---|---|---|
| `excelpilot verify` ignored `verification.enable_recalculation` | a standalone check reported `recalculated: false` on a workbook the run had just recalculated — weaker than the run it was checking | honours the setting, as `run` does; added `--config`, the only command that lacked it |
| `test_passes_for_an_untouched_workbook` asserted `recalculated is False` | the test **encoded the defect as expected behaviour**, which is why it went unnoticed | asserts against the real capability |
| `make test-security` selected **zero** tests | the `security` marker was declared but applied to nothing; the target exited 5, which reads as "security failed" rather than "security never ran" | marked 19 security-relevant test classes → **81 tests**; added a test asserting every marker selects a non-trivial number |
| `make bench` discarded the live JEV record | destroyed the only evidence a paid call happened, unrecoverably | carries a recorded call forward verbatim, labelled; 5 regression tests |
| `make smoke` called `input()` | failed with `EOFError` on every non-tty shell and CI run | non-interactive by default; `--keep` to inspect |
| `excelpilot policy` and plan output rendered list items via `field("", x)` | produced a line beginning with a bare colon and 26 spaces — reads as broken output | added an `item()` helper |

## Remaining risks

Only actual unresolved risks.

| Risk | Likelihood | Impact | Containment |
|---|---|---|---|
| **Macro-bound drawing parts are lost, unreported** | **confirmed** — measured on a real workbook | medium | The macro *project* survives byte-identically and writes are denied outright, so the code cannot be damaged. But a shape bound to a macro is dropped, and diff/verification do not report it. See [limitations.md](limitations.md) §3 |
| **LLM planner never run live** | certain | medium | `model.enabled` defaults to `false`; the deterministic planner is the default and is what everything measured used; plan output is untrusted and policy-checked either way |
| **One live JEV call** | certain | low | establishes integration only; no accuracy or reliability claim is made |
| **Injection scanning is heuristic** | medium | high | structural mitigation: a model cannot mutate a workbook. Detection is defence in depth, not the boundary |
| **JEV confidence thresholds uncalibrated** | observed — 2 of 4 questions came back at p=0.50 | low | an uncertain answer **escalates**, which is the safe direction |
| **openpyxl loses some OOXML parts** | certain | medium | measured and documented; workbooks with charts or pivots should not be round-tripped through ExcelPilot |
| **No authentication or multi-tenancy** | certain | high in a shared environment | out of scope; anyone who can run it has its authority |
| **Rollback is manual** | certain | low | deliberate: nothing is overwritten, so a bad run leaves a bad file until someone removes it |
| **Recalculation is a subset of Excel** | medium | medium | failures surface as errors, never as wrong numbers; `formulas` is not Excel |

## Macro-enabled workbook measurement

Performed 2026-09-26 against a real workbook found during this pass. This closes
what was previously the largest measurement gap in the project.

### The fixture

The original survey searched one directory and concluded that no fixture
contained a `vbaProject.bin`. That was wrong — a broader search found one in a
sibling automation folder, outside the directory originally searched. It is
referenced by the skipped-by-default `TestRealMacroWorkbook` tests, whose path
resolves it relative to the repository's parent; the file itself is never
committed, because it embeds a Windows username and absolute business paths.

| Property | Value |
|---|---|
| `vbaProject.bin` | 152,576 bytes, valid OLE2/CFB (`D0CF11E0A1B11AE1`) |
| SHA-256 | `171e1a806690bbecacf79b79d8a3a4c3bdfb0aa96815a115dc543e436a505d17` |
| CFB streams | 15 — `ThisWorkbook`, `Module1`, `Sheet2`–`Sheet4`, `dir`, `PROJECT`, `_VBA_PROJECT`, `__SRP_0`–`__SRP_7`, `PROJECTwm` |
| Content types | declares `application/vnd.ms-office.vbaProject` |

**Nothing derived from it is committed.** The workbook embeds a Windows username
and absolute business paths in its defined names, so it is measured in place
only. The committed fixture is generated from reviewable source
(`fixtures/vba.py`) and is documented as a structural stand-in, not a real VBA
project.

### Measured results

| # | Question | Result |
|---|---|---|
| 1 | Can ExcelPilot inspect it? | **Yes** — 3 sheets, 59 rows read |
| 2 | Does it identify as macro-enabled? | **Yes** — `has_vba: true` from the package, not the extension |
| 3 | Does inspection preserve the macro payload? | **Yes** — source file SHA-256 unchanged after repeated inspection |
| 4 | Does a supported transformation preserve `vbaProject.bin`? | **Yes, byte-identically** — SHA-256 unchanged through a full read/save round trip |
| 5 | Is the output a structurally valid `.xlsm`? | **Yes** — extension kept, `[Content_Types].xml` and `workbook.xml.rels` both still declare the part |
| 6 | Does the hard rule prevent unsafe writes? | **Yes** — `rejected_by_policy`, `vba_read_only`, no output written, source and VBA both unchanged |
| 7 | Does any operation strip the macro payload? | **No** — the payload survives intact |
| 8 | Does verification detect a relevant structural change? | **No** — see finding below |
| 9 | Does diff report accurately? | **It reports what it can see** (cell content) and is silent about a dropped part |

### The finding

**openpyxl drops drawing parts, and neither diff nor verification reports it.**

A read/save round trip of the real workbook:

| Part | In | Out | |
|---|---:|---:|---|
| `xl/vbaProject.bin` | 152,576 | 152,576 | **byte-identical** |
| `xl/theme/theme1.xml` | 6,995 | 6,995 | byte-identical |
| `xl/drawings/drawing1.xml` | 2,989 | — | **DROPPED** |
| `xl/worksheets/_rels/sheet3.xml.rels` | 299 | — | **DROPPED** |
| `xl/sharedStrings.xml` | 3,610 | — | dropped (known, benign) |
| 9 other parts | — | — | rewritten |

The dropped drawing is a shape bound to the macro —
`<xdr:sp macro="[0]!SomeMacro">`, the button a user clicks to run it.

Measured reporting for that round trip:

- `diff` → `structural_change: false`, 0 cell changes
- `verification` → `status: passed`, 0 anomalies

So the macro **code** is perfectly preserved, and the **affordance** that invokes
it is silently lost with no indication. This is the most significant open honesty
gap found in this pass. It is documented in
[limitations.md](limitations.md) §3–4 rather than fixed, because closing it
requires part-level comparison — a feature change, not a repair to the existing
safety contract, and out of scope for a measurement task.

## Release-hardening audit — Phase findings (2026-09-26)

A focused audit of the repository as a release candidate. Three concrete defects
were found, two of them security- or data-integrity relevant. All are fixed, with
regression tests.

### 1. Silent loss of workbook content on the core path — FIXED

**The most serious finding.** A plain `.xlsx` carrying a drawing — no macros, so
the `vba_read_only` hard rule does not apply — went through the ordinary approved
run path and reported:

```
run outcome        : succeeded
verification.status: passed
verification.passed: True
anomalies          : 0
diff.structural    : False
```

while the drawing was destroyed. Not an edge case: an ordinary workbook, an
ordinary approved change.

openpyxl's own warning corroborates it: *"DrawingML support is incomplete…
Shapes and drawings will be lost."* Measured across real workbooks, the loss
surface is wider than drawings — **seven comment parts** were dropped from one
real workbook, along with the VML anchor for legacy comments.

**Fix:** verification now compares the set of OOXML part names before and after
and **fails the run** when any part disappears other than two known-benign
caches (`xl/sharedStrings.xml`, `xl/calcChain.xml` — both rebuilt by Excel, with
values verified exact by the data checks). A name-set comparison, deliberately
the smallest mechanism that makes the verdict honest; it does not merge, repair,
or understand the parts it finds. Covered by `tests/test_content_loss.py`.

**What is not fixed:** the content is still absent from the output file. What
changed is that the run fails and says why, and the untouched source survives, so
discarding the output loses nothing.

### 2. A read-only run silently stripped the macro project — FIXED

Found *by* the check above, which is the argument for having added it.

The source snapshot was named `source.snapshot.xlsx` regardless of the source's
real extension. The executor opens the snapshot rather than the user's file, and
the reader decides `keep_vba` from the extension it sees — so a `.xlsm`
snapshotted as `.xlsx` loaded with `keep_vba=False`, and openpyxl dropped
`xl/vbaProject.bin` on save.

A **read-only** run — which policy deliberately permits on a macro workbook —
therefore produced an `.xlsm` output containing no macros, while the source
stayed intact and every other check passed. The `vba_read_only` rule was working
exactly as designed and was simply not the only path that could touch the file.

**Fix:** the snapshot keeps the source's real extension
(`store.snapshot_file_name`), and the CLI resolves a run's snapshot without
assuming a suffix (`store.find_snapshot`). A read-only run on a macro workbook
now succeeds with the project byte-identical.

### 3. Security denials were not attributable in the audit trail — FIXED

Attempting to direct a run's output back onto its own source with `--approve`
correctly failed and left the source untouched. But `run.json` recorded
`policy_outcome: require_approval` with an **empty** `policy_rule_ids`, and
`audit.jsonl` carried the readable reason with **no rule attached**. An auditor
could see that something stopped the run and what it said, but not which control
stopped it — so a hard-deny security refusal was indistinguishable from an
incidental failure.

`PolicyDenied` carried `rule_ids` the whole time; they were dropped between the
guard and the run record. Now propagated into `run.json`, the audit trail, and
the `--json` payload. Covered by `tests/test_audit_attribution.py`.

### 4. A false positive avoided: `defusedxml` is load-bearing

During the dependency review, `grep -r defusedxml app/` returns nothing, which
makes the dependency look unused and invites its removal. It is **not** unused:
`openpyxl.xml.functions` imports it, so every OOXML parse goes through hardened
XML. Verified by instrumenting `defusedxml.ElementTree.fromstring` during a real
`inspect_workbook` call — **55 of 55** parses used it.

Removing it would have silently downgraded every parse to stdlib
`xml.etree.ElementTree` with entity expansion enabled. The dependency is kept and
the mechanism is now documented, including that `OPENPYXL_DEFUSEDXML=False`
disables it without any change to ExcelPilot.

### 5. Documentation and packaging corrections

| Finding | Action |
|---|---|
| No `LICENSE` file, though `pyproject.toml` and the README both declared MIT | Added the MIT text — without it the grant of rights does not exist |
| A `dashboard` extra installing `fastapi`/`uvicorn`/`jinja2` for a module that was deliberately never built | Extra removed; ADR-0002 amended |
| `pytest-cov` and `hypothesis` declared, never used | Removed; ADR-0002 records why and when to reinstate |
| `docs/adr/0002-minimal-dependencies.md` still listed `pycel` as the recalc backend | Corrected to `formulas`, with `pycel` marked rejected |
| `docs/cli.md` presented exit 7 as general "not found" | Corrected: 7 is for an unknown **run id**; a bad workbook path is a usage error (2). Both now tested |
| `docs/cli.md` did not mention that an execution-time policy denial is exit 1, not 3 | Documented, with the two-distinction note, and tested |
| The drawing-loss limitation was absent from the README | Added to the README status, where a user will actually read it |

### 6. CI

No CI existed. A single-job workflow was added at `.github/workflows/ci.yml`
running lint, strict mypy, `make check`, the security tests, the E2E tests, the
benchmark **harness** tests, the smoke test, and a packaging/import/fixture smoke
step.

Deliberately excluded: the 165-run benchmark (its result is a recorded
measurement, not a pass/fail gate, and running it per-PR would invite "fixing" a
timing difference already shown to be noise), live JEV calls (they cost money),
and the slow suite (~7 minutes; run it before a release).

Every command in the workflow was executed locally and passes. `uv lock --check`
confirms the lock file is in sync, which `--locked` in CI depends on.

## Documentation written

| File | Purpose |
|---|---|
| `README.md` | What it is, what it is not, architecture, quickstart, measured results, privacy |
| `docs/architecture.md` | Components, dependency boundaries, state machine, data flow, every boundary |
| `docs/security.md` | Threat model, injection, traversal, secrets, authorisation, what remains open |
| `docs/jev.md` | Adapter design, decision boundary, paid-call gate, payload privacy, live call, failure behaviour |
| `docs/cli.md` | Every command with real output, exit codes, audit trail |
| `docs/configuration.md` | Every setting with defaults, and what config cannot change |
| `docs/benchmarks.md` | Methodology, measured results, timing limits, regression findings |
| `docs/testing.md` | Test layers, commands, results, how to write a test |
| `docs/limitations.md` | What it cannot do, in 13 sections |
| `docs/verification-report.md` | This document |
| `docs/adr/` | Twelve ADRs; ADR-0011 amended with a supersession notice |
| `docs/implementation-plan.md` | Updated: Phase 8/9/10 complete, two limitations resolved, Phase 8 findings recorded |

## Repository state

- 9 commits, **nothing pushed**, no remote configured
- Working tree contains the completed work, uncommitted
- No secrets in any file, log, or generated artifact. Verified: the live
  `TYPESAFE_API_KEY` value appears in **no** file in the repository (checked by
  exact match, without printing it). The only `sk-`-shaped strings present are
  deliberate dummy values in `tests/test_verification.py`, exercising the
  redactor
- No TODO, FIXME, or HACK markers in library code
- No debug statements; `print` is banned in library code by a test

## What this report does not claim

- **Not production readiness.** 536 tests, 81 security tests, and 165 benchmark
  runs are evidence of care, not of fitness for a workload nobody has run.
- **Not accuracy.** No benchmark here measures whether a change was *correct* in
  a business sense. It measures whether the system did what it was told and
  reported honestly when it could not.
- **Not a performance claim.** Timing was not measurable.
- **Not a security guarantee.** See §"Remaining risks" in
  [`security.md`](security.md) for the six open items.

The honest summary: this is a well-tested, carefully bounded system whose
documented limits match its implemented behaviour, and whose safety properties
are structural rather than promised.
