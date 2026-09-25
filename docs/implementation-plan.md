# ExcelPilot — Living Implementation Plan

> This document is the authoritative record of ExcelPilot's construction.
> It is updated at the end of every phase. Findings marked **VERIFIED** were
> established by running a command; everything else is a plan, not a result.

- **Project:** ExcelPilot — AI Excel Operations Engine
- **Status:** Phase 8 — Benchmarks (in progress)
- **Last updated:** 2026-09-25

---

## 1. Phase 0 — Reconnaissance findings (COMPLETE)

All findings below were established by inspecting this machine and the existing
workspace. No assumption was carried forward unverified.

### 1.1 Repository

| Finding | Evidence |
|---|---|
| ExcelPilot did not exist; greenfield | No `.git` at `My Projects/`; no `ExcelPilot/` before this work |
| Nearest sibling project is `Excel Automation/AirtelGLAutomation` | Python 3.10+, setuptools, pydantic v2, openpyxl, pytest, ruff; `app/` layout, `Makefile`, `IMPLEMENTATION_PLAN.md`, 52 modules, 31 test files |
| Existing conventions to match | `pyproject.toml` + `[tool.ruff]` + `[tool.pytest.ini_options]`, `app/` package, `tests/`, `Makefile` targets `install/test/test-fast/lint/clean` |
| Real Excel fixtures available | 24 `.xlsx`/`.xlsm` files under `Excel Automation/Airtel Internship Macros/`, incl. one 37,883×120 sheet and workbooks with 16 defined names, `veryHidden` sheets, and 826 formulas |

### 1.2 Runtime

- **VERIFIED:** Python 3.14.2 system, 3.13.15 and 3.12.14 available via uv. Node 24, pnpm 12, bun 1.4 also present. No Go/Rust/Java.
- **Decision:** Python 3.13 (`requires-python = ">=3.11,<3.14"`). Python 3.14 was rejected for the project runtime because third-party wheel availability for the optional recalculation stack is unproven there. ExcelPilot runs on 3.13; the workbook-engine findings below were *additionally* re-confirmed on system 3.14 so the conclusion is not an artifact of the pinned runtime.

### 1.3 Libraries

- **VERIFIED present (system py3.14):** openpyxl 3.1.5, pandas 3.0.5, numpy, xlsxwriter, lxml, pytest
- **VERIFIED absent:** pydantic, typer, click, rich, hypothesis, defusedxml
- **VERIFIED openpyxl 3.1.5 is the current PyPI release** (checked against `https://pypi.org/pypi/openpyxl/json`)

### 1.4 Workbook engine — measured round-trip fidelity

**VERIFIED** on `Sunset Tracker.xlsx` (6 sheets) via a load → save → reload probe:

| Metric | Original | Round-trip | Result |
|---|---:|---:|---|
| Sheets | 6 | 6 | preserved |
| Non-empty cells | 69,660 | 69,660 | preserved |
| Formulas | 69,221 | 69,221 | preserved |
| Fills | 70,002 | 70,002 | preserved |
| Merged ranges | 219 | 219 | preserved |
| Data validations | 18 | 18 | preserved |
| Conditional formats | 36 | 36 | preserved |
| Defined names | 16 | 16 | preserved |
| Tables | 3 | 3 | preserved |

**VERIFIED** on `VBA_Seed.xlsm`: 5,873 / 5,873 cell values identical after round-trip.

**VERIFIED losses / rewrites in the OOXML package:**

| Observation | Consequence for ExcelPilot |
|---|---|
| Byte content differs after a save (zip re-serialization) | **Change detection must use content hashing, never byte identity.** |
| `xl/sharedStrings.xml` is dropped | String values are still exact (5,873/5,873); the cache is rebuilt. Not a data loss, but part identity is not stable. |
| Comments relocate `xl/comments1.xml` → `xl/comments/comment1.xml` | Part paths are not stable. Manifests must key on logical location, not zip path. |
| None of the workspace `.xlsm` files contain `xl/vbaProject.bin` | **VBA preservation could not be verified against real fixtures.** See §7.1. |

### 1.5 Formula recalculation

- **VERIFIED:** `formulas` 1.3.4 and `pycel` 1.0b30 both *resolve* on CPython 3.14 via `uv pip install --dry-run`. `formulas` pulls scipy 1.18.1 + schedula; `pycel` needs only 3 packages.
- **CORRECTION:** an earlier inference from their trove classifiers (both stop at 3.9) was wrong. Resolution is not function.
- **VERIFIED (Phase 6):** neither library successfully recalculates ExcelPilot's benchmark workbook. See §7.2.

### 1.6 JEV — mandatory investigation

Findings established by reading the authoritative source
`~/.local/share/uv/tools/jev-skill/lib/python3.13/site-packages/jev.py` (300 lines,
Python standard library only), and cross-checked against
`https://github.com/wuyoscar/jev-skill` (`docs/installation.md`).

| Question | Answer | How established |
|---|---|---|
| What is JEV? | `jev-skill` 0.2.0, MIT, by `wuyoscar` — a *decision* service (choose / classify / score). It is not an executor. | Read `jev.py`, PyPI metadata, upstream README |
| Install method | `uv tool install git+https://github.com/wuyoscar/jev-skill.git@v0.2.0` | upstream `docs/installation.md` |
| Is the local install usable? | **No — broken.** `~/.local/bin/jev-decide` shebang points at `.../jev-skill/bin/python`, which does not exist. Invoking `python3 <site-packages>/jev.py` works. | Ran `jev-decide --help` → `bad interpreter` |
| Input shape | `{model, state, questions}`; `state` = text \| object \| array of evidence; `questions` = `{id: {type, instructions, criteria}}`; `type` ∈ `choice` (2–255 criteria dict), `noul`, `score` (2–10 ordered) | Read `validate_request()` |
| Output shape | `answers` keyed by question id. `choice` → `{choice, probabilities, confidence}`; `noul` → `{noul}`; `score` → `{score, probabilities, legend}` | Read `distribution()` / `build_report()` |
| Is confidence available? | **Yes.** `probabilities` per label, `confidence`, and a computed `margin`. | Read `build_report()` |
| Decision statuses | `selected`, `needs_review`, `scored`; reserved review labels `other, unknown, abstain, review, ask_user, wait, none, defer, insufficient_evidence` | `REVIEW_LABELS` |
| Endpoints | OpenRouter `https://openrouter.ai/api/alpha/decisions` (model `typesafe/jev-1.13`); TypeSafe `https://api.typesafe.ai/v1/systemone` (model `jev-1.13.0`) | `http_json()` |
| Failure modes | `JevError` for bad input, missing key, HTTP error (**no auto-retry**), non-JSON, provider error body; keys never logged; redirects refused | Read `http_json()` |
| Exit codes | `0` selected/scored, `2` some needs-review, `1` error | `main()` |
| Authority | Response contains `policy.executes_actions: false`. Upstream: *"None grants permission to execute an action."* | Read `build_report()`; upstream docs |
| Threshold guidance | `--min-probability 0.8` / `--min-margin 0.15` are *"uncalibrated starting points, not deployment recommendations"* | upstream docs |

**Local credential state (presence only, values never read or printed):**

- **VERIFIED:** `TYPESAFE_API_KEY` is set. `OPENROUTER_API_KEY` is not set.
- **Consequence:** the CLI defaults to OpenRouter and never falls back, so ExcelPilot
  must pass `--provider typesafe` explicitly or every live call fails with
  "Set OPENROUTER_API_KEY in the calling process environment".

**Contract verified without spending money** — `jev.py setup` and
`jev.py decide … --dry-run` both make zero network calls. Both were run and their
output matched the source reading.

**Decision:** ExcelPilot implements `JevAdapter` against the documented HTTP contract
directly (stdlib `urllib`), rather than shelling out to the broken venv or depending on
it. The adapter is contract-tested against `jev.py --dry-run`.

### 1.7 LLM provider

- **VERIFIED:** no `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, or `OPENROUTER_API_KEY` is set.
- **Decision:** the MVP ships a **deterministic** natural-language → `ExecutionPlan`
  compiler that runs with zero credentials, plus a provider abstraction whose LLM path
  is implemented and contract-tested but cannot be exercised end-to-end here. This is
  reported as a limitation, not papered over.

---

## 2. Architecture

### 2.1 Package layout

```
app/
  contracts/    typed, versioned vocabulary. No logic. Imports nothing internal.
  workbook/     inspector, reader, content hasher, limits
  planner/      deterministic NL->plan compiler; LLM provider adapter
  decisions/    JevAdapter protocol; TypeSafe/OpenRouter HTTP impls; mock
  policy/       deterministic rules engine
  executor/     typed operation dispatch (the only component that mutates workbooks)
  verification/ structural, data, reconciliation, formula, anomalies
  diff/         content-hash diff, ChangeManifest
  audit/        append-only event log, redaction, replay
  storage/      run store, artifact store, atomic versioned writes
  safety/       path sandbox, injection firewall, formula-injection guard, limits
  app/          run state machine (sole orchestrator)
  cli/          Typer CLI
  dashboard/    FastAPI + server-rendered (final phase)
benchmarks/  docs/adr/  examples/  fixtures/  tests/
```

### 2.2 Dependency direction (enforced by `tests/test_architecture.py`)

```
cli ─┐
     ├─> app ──> policy ──> contracts
dash┘          │  executor ─> workbook ─> contracts
                │  verification ─> diff ─> workbook
                ├─> decisions ─> contracts
                └─> planner ─> contracts
audit ─> contracts      storage ─> contracts
```

Forbidden and tested-for:

- `executor` must not import `planner`, `decisions`, or any provider
- `policy` must not import `planner`, `decisions`, `executor`
- `workbook` must not import anything above `contracts`
- `contracts` must not import any internal module
- No module outside `planner` may construct an LLM request

### 2.3 Trust boundaries

| Boundary | Rule |
|---|---|
| Workbook cell / sheet name / comment content | **Data, never instructions.** Carried as `UntrustedText`, structurally separated from prompts, size-capped, and stripped of instruction-like content before any model call. |
| Model output | Untrusted. Parsed into typed contracts; rejected on validation failure. Never reaches the executor except as a validated `ExecutionPlan`. |
| JEV output | Advisory. `JevDecision` has no field capable of expressing a mutation. |
| Policy | Deterministic, config-driven, and the *only* authority on permission. |
| Executor | Deterministic. Re-validates every operation and re-checks policy at execution time (defence in depth). |
| Verification | Independent of executor. A run cannot be marked successful on save alone. |

### 2.4 Safe defaults

- Source workbook is never opened for writing; it is copied to a read-only snapshot.
- Every run gets a UUID `run_id` and a content hash of source and output.
- Output is versioned (`<stem>__run-<run_id>.xlsx`) and written atomically.
- `--dry-run` mutates nothing and costs nothing.
- Approval is required whenever policy thresholds are crossed, independent of JEV.
- Verification failure fails the run. `file saved` ≠ `outcome verified`.
- Secrets are redacted in logs, audit records, manifests, and error messages.

---

## 3. Phase status

| Phase | Scope | Status | Verification |
|---|---|---|---|
| 0 | Reconnaissance | COMPLETE | §1 — evidence recorded |
| 1 | Repo, tooling, plan, ADRs 0001–0012 | COMPLETE | `ruff check` + `ruff format --check` + `mypy` clean |
| 2 | Typed contracts | COMPLETE | 85 tests: `pytest tests/test_contracts.py tests/test_architecture.py` |
| 3 | Workbook engine | COMPLETE | 63 tests + 6 slow real-workbook tests. `make test-slow`: 6 passed in 316s |
| 4 | Operations + executor | COMPLETE | 55 executor tests. `pytest tests/test_executor.py` |
| 5 | Planner, JEV adapter, policy | COMPLETE | 36 policy + 47 planner + 46 JEV tests. Real `jev.py --dry-run` accepted our request (3 contract-drift tests) |
| 6 | Diff, verification, audit, storage | COMPLETE | 63 tests: `pytest tests/test_verification.py` |
| 7 | CLI | COMPLETE | 45 CLI tests. `pytest tests/test_cli.py`. Exit codes 0-7 verified as subprocesses |
| 8 | Benchmarks (incl. the one consented live JEV call) | IN PROGRESS | — |
| 9 | Documentation + ADRs | NOT STARTED | — |
| 10 | Full verification + report | NOT STARTED | — |

---

## 4. Decisions

| ID | Decision | Rationale | Where |
|---|---|---|---|
| D1 | Python 3.11–3.13, uv, flat `app/` package | Only mature formula-preserving XLSX library; matches sibling project conventions; avoids monorepo overhead | `pyproject.toml` |
| D2 | openpyxl as the sole workbook engine | **VERIFIED** lossless round-trip on 69k-formula real workbook | `docs/adr/0001-workbook-engine.md` |
| D3 | Content hashing, never byte equality, for change detection | **VERIFIED** bytes differ after every save | `docs/adr/0009-diff-strategy.md` |
| D4 | JEV behind an adapter calling the HTTP contract directly | Local CLI is broken; subprocess coupling is brittle | `docs/adr/0004-jev-integration.md` |
| D5 | Deterministic policy is the sole authority; JEV is advisory | Upstream itself emits `executes_actions: false` | `docs/adr/0005-policy-engine.md` |
| D6 | Deterministic planner is the default; LLM is optional | No LLM credential exists in this environment | `docs/adr/0003-model-provider.md` |
| D7 | Append-only JSONL audit, one directory per run | Inspectable with `cat`/`jq`; no DB dependency | `docs/adr/0007-persistence.md` |
| D8 | Safe-output versioning, not in-place rollback | Never overwrite source; rollback = discard output | `docs/adr/0010-rollback.md` |
| D9 | Verification is independent of the executor | Prevents self-confirming verification | `docs/adr/0011-verification.md` |

---

## 5. Implemented capabilities

See `docs/limitations.md` for the authoritative list of what is *not* supported.

---

## 6. Risk register

| Risk | Likelihood | Impact | Mitigation | Residual |
|---|---|---|---|---|
| LLM path unexercised (no key) | certain | medium | Deterministic planner is the default; LLM adapter contract-tested | Open — report honestly |
| No real formula recalculation | unknown — untested | medium | Strongest available static + reference-graph verification; explicitly never called "recalculated" | Open — resolved in Phase 6 |
| VBA preservation unverified against real macros | **occurred** (no fixture has `vbaProject.bin`) | medium | Build a synthetic macro fixture; enforce `keep_vba=True` | Partly open |
| Malicious workbook | medium | high | Size/ratio/cell limits, hardened XML, timeouts, sandboxed paths | Security tests |
| Prompt injection via cells | high | high | `UntrustedText` + firewall + tests | Security tests |
| Policy bypass via crafted plan | medium | high | Executor re-validates and re-checks policy; typed discriminated union rejects unknown ops | Security tests |

---

## 7. Deferred and out of scope

### 7.1 Explicitly deferred with extension points

- Charts and pivot tables (typed operation namespace reserved; not implemented)
- Legacy `.xls` (xlrd) — XLSX/XLSM only
- Multi-workbook / cross-file operations
- Arbitrary code execution (deliberately **not** implemented, per spec §24)
- Full Excel formula recalculation engine
- Concurrent/multi-user runs, collaboration
- Cloud storage backends (local filesystem only)

### 7.2 Known limitations carried into release

1. Formula recalculation status is **not yet determined** — Phase 6 must test
   `pycel` and `formulas` functionally. Until then no claim about recalculation
   may appear anywhere in this project.
2. VBA authoring impossible. `.xlsm` is read/preserve only. No real-world macro
   workbook exists in the fixture set. A **synthetic** macro fixture (an xlsx
   re-saved as `.xlsm` with an injected `xl/vbaProject.bin`) is used to verify
   that `keep_vba` is enforced and that `has_vba` reports truthfully — but that
   is not the same as preserving a real macro project through a real edit cycle,
   which remains untested.
3. `openpyxl` does not preserve every OOXML part. Verified losses: the
   `sharedStrings` cache is dropped and comment parts are relocated. Values are
   unaffected.
4. The LLM planning path is implemented and contract-tested but was never run
   against a live provider.
5. No live JEV call has been made. One is planned for Phase 8, with user
   consent, and its measured result will be recorded in
   `benchmarks/results.json`. Until then, no result may be attributed to JEV.

---

## 8. How to extend

**Add a workbook operation** (the documented developer-experience requirement):

1. Add a variant to the `WorkbookOperation` discriminated union in
   `app/contracts/operations.py`.
2. Implement its handler in `app/executor/operations.py` and register it in
   `app/executor/registry.py`.
3. Add a policy rule in `app/policy/rules.py` (required — the registry refuses
   operations without one).
4. Add the dry-run preview path in `app/executor/preview.py`.
5. Add tests. `tests/test_architecture.py` and `test_executor_registry.py` fail if
   any step is skipped.

---

## 9. Final verification record

Recorded in `docs/verification-report.md`, produced by running
`make check && make test` on a clean checkout. Every claim in the README and in the
engineering report cites a command from that record.
