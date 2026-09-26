# Changelog

All notable changes to this project are recorded here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Release notes with context and evidence: [`RELEASE_NOTES.md`](RELEASE_NOTES.md).

## [Unreleased]

Nothing yet.

## [0.1.0] — 2026-09-26
First baseline release. Alpha quality: tested and measured, not hardened for
production workloads. See [`docs/limitations.md`](docs/limitations.md) for the
edges.

### Added

**Workbook engine**
- Content-based inspection: sheets, headers, types, formulas, hidden sheets,
  tables, defined names, sensitivity classification
- Resource limits enforced before expensive work — file size, compression
  ratio, sheet count, declared dimensions, cell and formula totals
- Content hashing for change detection; byte comparison is never used, because
  bytes change on every save

**Execution**
- Thirteen typed operations in a closed discriminated union, dispatched through a
  registry that refuses to register an operation without a policy rule
- Per-operation re-validation, target re-resolution, and execution-time policy
  re-check before any handler runs
- Atomic versioned writes; the source workbook is never opened for writing
- Optional formula recalculation (`recalc` extra), verified against Python ground
  truth, with the result always stating which kind of claim it is making

**Policy and approval**
- Six hard-deny rules that no configuration can disable; seven configurable
  escalation rules
- Fail-closed approval gate, enforced as a transition in a validated state
  machine
- No unsafe override flags; a test asserts they do not exist

**JEV integration**
- `JevAdapter` protocol with an offline mock and a live HTTP implementation
  calling the documented contract directly
- Asymmetric combination: `requires_approval = policy_requires OR jev_escalates`
- Paid calls gated behind explicit opt-in; the adapter raises rather than
  silently reporting "not called"
- Payload built from an explicit field allowlist — no cell values, formulas, cell
  addresses, or file paths leave the machine

**Verification and audit**
- Structural, data, formula, recalculation, and anomaly checks, independent of
  the executor
- Reconciliation recomputed in Python from the workbook's own cells
- Part-inventory check that fails a run when workbook content disappears
- Append-only JSONL audit with redaction at write time; a security denial records
  which rule fired
- Read-only replay

**CLI**
- `inspect`, `plan`, `run`, `diff`, `verify`, `replay`, `runs`, `policy`, `gc`,
  `version`
- Distinct exit codes 0–7 so a script can distinguish a policy denial from a
  verification failure without parsing text
- `--json` on every command for machine consumers

**Benchmark**
- 11 scenarios × 3 modes × 5 repeats, with a discarded warmup pass and a
  spread-based comparison that declines to report unmeasurable timings
- 35 tests of the harness itself, so a benchmark that cannot fail is caught

**Fixtures and tests**
- 581 tests: unit, integration, end-to-end, security, regression, and benchmark
  harness
- A generated, structurally genuine macro-enabled package fixture, committed as
  source rather than as a binary
- Every bug found during construction preserved as a regression test with its
  reasoning

**Repository**
- CI running lint, strict mypy, the pre-commit gate, security, E2E, benchmark
  harness, smoke, and packaging checks — with no paid calls
- MIT licence, shipped in the built wheel
- Documentation set covering architecture, security, JEV, CLI, configuration,
  benchmarks, testing, limitations, verification, and roadmap

### Fixed

Defects found by the project's own verification, recorded because each one was
real and each now has a regression test.

- **Silent OOXML content loss on ordinary `.xlsx` files.** A workbook carrying a
  drawing reported `succeeded` and `passed` with zero anomalies while the drawing
  was destroyed. Verification now compares part inventories and fails the run.
- **Macro-project stripping on `.xlsm` read-only runs.** The source snapshot was
  named `source.snapshot.xlsx` regardless of the source's extension, so the
  reader chose `keep_vba=False` and openpyxl dropped the project. The snapshot now
  keeps the real extension.
- **Missing policy-denial attribution.** A hard-deny refusal reached the run
  record with no rule id, making a security stop indistinguishable from an
  incidental failure. Rule ids now propagate to the record, the audit trail, and
  the `--json` payload.
- **Underscore-prefixed sheet names were unreachable**, which let a request naming
  a hidden internal sheet fall through to the main sheet — bypassing the
  hidden-sheet escalation rule.
- **`excelpilot verify` ignored the configured recalculation setting**, so a
  standalone check was weaker than the run it was checking.
- **A `security` marker was declared but applied to nothing**, so
  `make test-security` selected zero tests while looking like a failing gate.
- **`make bench` discarded the recorded live JEV call**, destroying unrepeatable
  evidence of a paid call.
- **`make smoke` called `input()`**, failing in every non-tty shell.
- **Recalculation was documented as unavailable** in nine places after it was
  integrated.
- **`defusedxml` appeared unused** — it is loaded by openpyxl, not by ExcelPilot,
  and every OOXML parse depends on it. Verified rather than removed.

[Unreleased]: https://github.com/Abhieu/excelpilot/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/Abhieu/excelpilot/releases/tag/v0.1.0
