# ExcelPilot 0.1.0

First baseline release. Deterministic Excel operations with an AI/JEV advisory
layer, a hard safety boundary, verification, and an audit trail.

**This is an alpha.** It is tested and measured, not hardened for production
workloads. The [Status](#status) section and
[`docs/limitations.md`](docs/limitations.md) say exactly where the edges are, and
nothing below claims otherwise.

---

## Implemented and verified

### Deterministic workbook execution

Thirteen typed operations, dispatched through a closed discriminated union. The
executor is the only component that can mutate a cell, it re-validates every
operation before running it, and it re-evaluates policy at execution time as
defence in depth.

Measured round-trip fidelity against a real 37,883 × 120 workbook with ~69,000
formulas: all formulas, fills, merged ranges, data validations, conditional
formats, defined names, and tables preserved. That measurement is why openpyxl
was chosen as the sole engine ([ADR-0001](docs/adr/0001-workbook-engine.md)).

### Policy and approval

Thirteen rules. **Six are hard denies** that no configuration file can disable:
`source_never_overwritten`, `output_within_workspace`, `cell_ceiling`,
`operation_ceiling`, `vba_read_only`, `no_guessing`. The other seven escalate
for human approval.

The approval gate is fail-closed — no approval means no execution — and is a
transition in a validated state machine, so skipping it raises rather than
passing. There are no `--force`, `--no-verify`, `--skip-policy`, `--overwrite`,
or `--in-place` flags, and a test asserts they do not exist.

### AI and JEV as advisory, never authoritative

`requires_approval = policy_requires OR jev_escalates`. JEV can make a run more
cautious and can never make it less. `JevDecision` has no field capable of
expressing a workbook mutation, and the executor will not accept a decision set.

A model cannot write a cell. Its output is parsed into typed contracts; an
invented operation or a missing target is a refusal, not a guess.

### Verification and reconciliation

Structural, data, formula, recalculation, and anomaly checks, run independently
of the executor. Totals are recomputed in Python from the workbook's own cells —
no model is ever asked whether a number looks right.

Formula evaluation is available as an optional extra and was verified against
Python ground truth. The result always states which kind of claim it is making:
`recalculated` and `static_formula_checks` are inverse and exactly one is true.

### Auditability

Append-only JSONL per run, redacted at write time, replayable read-only. The
`actor` field distinguishes `user`, `system`, `jev`, and `policy`, so "JEV said
yes" and "JEV was never asked" can never look the same afterwards. A security
denial records **which rule** stopped it, in `run.json`, the audit trail, and the
`--json` payload.

### Benchmark methodology and measured results

11 scenarios × 3 modes × 5 repeats = **165 runs**, with a discarded warmup pass
and a spread-based comparison that declines to report a timing claim it cannot
support. All 55 expectations and 35 checkable properties met in each mode; the
source workbook was byte-identical afterwards in **all 165 runs**.

**Timing differences between modes were not measurable.** The observed spread
(~0.24 s per mode) exceeded every between-mode difference by roughly two orders
of magnitude, so no performance claim is made for any mode.

The benchmark found three real defects the test suite had not, which is documented
in [`docs/benchmarks.md`](docs/benchmarks.md). It was used as a correctness
instrument, not a scoreboard.

### Security testing

99 security-boundary tests, plus 10 architecture invariants enforced statically
from the import graph and the source. Adversarial cases are tested as behaviour:
path traversal, symlink escape, source-overwrite attempts, dry-run write
attempts, prompt injection treated as data, invented operations, and permissive
configuration against hard denies.

### CI

A single-job workflow running lint, strict mypy, the pre-commit gate, the
security tests, the E2E tests, the benchmark **harness** tests, the smoke test,
and a packaging/import/fixture check. It makes **no paid calls** and depends on
no private workbook.

It deliberately does not run the 165-run benchmark, any live JEV call, or the
slow suite — the reasons are in the workflow file.

### `.xlsm` and VBA — measured, not assumed

A genuine 152 KB VBA project was measured, not a synthetic blob:

| Capability | Result |
|---|---|
| Read / inspect | Yes — 3 sheets, 59 rows |
| Detect macro-enabled | Yes — from the package, not the extension |
| Preserve `vbaProject.bin` | **Byte-for-byte**, SHA-256 identical |
| Transform | **No** — every mutating operation denied |
| Mutate / author | **No** — refused unconditionally, and no VBA authoring |

`--approve` and a maximally permissive configuration both fail to override the
deny. Read-only operations are permitted, and now preserve the project.

### OOXML content-loss detection

openpyxl does not round-trip every part: cell **comments** (seven parts on one
real workbook) and **drawings** — including shapes bound to a macro — are dropped
on save. This was found when a plain `.xlsx` reported `succeeded` and `passed`
while losing a drawing.

Verification now compares the set of workbook parts before and after and **fails
the run** when content disappears, so a silent loss can no longer be reported as
success. The source stays untouched, so discarding the output loses nothing.

### Clean-environment reproducibility

Verified from a fresh virtualenv: install, import, CLI entry point, the full
ten-step documented workflow, and 581 passing tests. `uv lock --check` is clean,
and the build produces zero warnings with the licence text shipped in the wheel.

---

## Known limitations

**None of these are solved. Several are only *detected*.**

| Limitation | Status |
|---|---|
| Formula recalculation is a **subset** of Excel | Optional extra; failures surface as errors, never wrong numbers |
| Comments and drawings are lost from the **output** | **Detected** — the run fails and names the lost parts. **Not prevented or repaired** |
| The part check compares part *inventories* | A surviving part with altered contents is not detected |
| VBA writes are prohibited | By design, non-configurable |
| VBA preservation | Measured byte-for-byte on one real project |
| The live LLM planner has **never been run** | Contract-tested only; the deterministic planner is the default |
| One live JEV call | Establishes the integration works. **Nothing** about accuracy, reliability, calibration, or latency distribution |
| JEV thresholds | Upstream defaults, described upstream as "uncalibrated starting points" |
| No authentication | Anyone who can run ExcelPilot has its authority |
| Rollback is manual | Delete the versioned output; nothing was overwritten |
| openpyxl loses other OOXML parts | Charts, slicers, pivot caches |
| `OPENPYXL_DEFUSEDXML=False` disables XML hardening | ExcelPilot neither prevents nor detects this |

See [`docs/limitations.md`](docs/limitations.md) and
[`docs/verification-report.md`](docs/verification-report.md).

---

## Status

Working and tested. **Not production-hardened, and with one lossy transformation
you must know about before using it on a workbook with comments or drawings.**

Formula recalculation implements a subset of Excel and is optional. The LLM
planner has never been run against a live provider. There is no authentication,
and rollback is manual by design.

Read [`docs/limitations.md`](docs/limitations.md) before relying on any of this.

---

## Licence

MIT — see [LICENSE](LICENSE). Security reporting: [SECURITY.md](SECURITY.md).

Roadmap for what comes after this baseline:
[`docs/roadmap.md`](docs/roadmap.md).
