# ADR 0011 — Verification strategy

- **Status:** Accepted
- **Date:** 2026-09-25
- **Related:** ADR-0001 (workbook engine), ADR-0009 (diff is content-based)
- **Amended:** Phase 6 — recalculation became an optional capability. The static-analysis
  reasoning below is unchanged and still necessary; only the *absence* of evaluation was
  superseded. See the "What formula verification does and does not claim" section.

## Context

The specification's sharpest requirement is the distinction between:

> *file saved successfully* — and — *desired workbook outcome verified*

and the instruction: *"A run must not be marked fully successful if verification
fails"* and *"Never claim a workbook is correct merely because it saved successfully."*

The hard constraint is that **openpyxl cannot recalculate formulas** (ADR-0001). It
reads formulas as strings; with `data_only=True` it returns whatever value Excel last
cached, which may be stale, absent, or wrong. Any verification built on trusting those
cached values is a verification built on a lie.

## Decision

Verification is an **independent subsystem** that re-reads the output workbook from
disk and checks it against the plan's declared intent. It does not consult the
executor's in-memory state, and a verification failure fails the run (exit code 5).

### Four independent check families

**1. Structural** — expected sheets exist; no unexpected sheet was removed; sheet
visibility is as expected; tables remain valid; defined names resolve; dimensions are
plausible; the file re-opens cleanly.

**2. Data** — row counts, null rates, duplicate rates, key-field validity, expected
ranges populated, no unexpected truncation. Compares against pre-run values captured by
the inspection, so a silent data loss is caught.

**3. Reconciliation** — subtotals, grand totals, and cross-sheet reconciliation,
recomputed **deterministically from cell values** with configurable tolerances, and
reported as `passed` / `failed` / `warning` with `expected`, `actual`, `variance`,
`tolerance`, and an explanation.

**4. Formula** — presence, pattern consistency, reference-graph resolution, broken
`#REF!`-style references, unexpected hard-coded replacements where formulas were
expected, and unexpected formula deletion.

### What formula verification does and does not claim

**Superseded in part (Phase 6).** This ADR originally recorded that no
recalculation backend was available. That is no longer true, and the original
reasoning is preserved below because the reasoning still holds — it is what
makes the *static* checks necessary even when evaluation succeeds.

The original position was:

> Formula checks are **static**. ExcelPilot reports `static_formula_checks` and states
> `recalculated: false` in every verification result. It never emits a message implying a
> formula was evaluated.
>
> Totals used for reconciliation are recomputed from **data cells**, not from formula
> results. If a workbook is entirely formula-driven, ExcelPilot says so and marks the
> reconciliation `warning` with `explanation: "totals derived from formula cells; no
> recalculation available"`. It does not guess.

The part that still holds: **openpyxl cannot recalculate formulas** (ADR-0001). It
reads formulas as strings; with `data_only=True` it returns whatever Excel last
cached, which may be stale, absent, or wrong. So reconciliation computed *during
execution*, against an in-memory workbook, still reads cached values and is still
downgraded to a warning. That is unchanged.

What changed: recalculation is now an **optional, additive** capability
(`app/verification/recalc.py`, the `recalc` extra, `formulas` 1.3.4), measured
against Python ground truth. The result carries two mutually exclusive flags:

| `recalculated` | `static_formula_checks` | Meaning |
|---|---|---|
| `true` | `false` | Formulas were evaluated. A stronger claim. |
| `false` | `true` | Static checks only. Real, but strictly weaker. |

Exactly one is always true, so a consumer can never read a static result as
though it had been evaluated. Both static and evaluated checks run when
evaluation is available, because they catch different things: evaluation catches
arithmetic and reference failures that look fine as text, and static analysis
catches a formula replaced by the number it happened to evaluate to.

`pycel` was evaluated and rejected — wrong API surface. `formulas` works but
implements a subset of Excel's function set, so a failure surfaces as an error
rather than a wrong number. See [limitations.md](../limitations.md) §1.

One consequence worth recording: this ADR's own wording was quoted into a JEV
prompt that told a model ExcelPilot could not recalculate, which had become
false. The prompt is now derived from the run's measured capability and
`state.capabilities` carries the fact as evidence, so the two cannot drift apart
again.

### A run cannot succeed on save alone

`VerificationResult.passed` is a required input to run outcome. `RunOutcome` is:

```
succeeded  requires: no errors AND verification.passed AND reconciliation not failed
failed    otherwise
```

The CLI returns exit code 5 when a file was written but verification failed, so the
failure is impossible to mistake for success in a script.

### Anomalies are attributed

Every `Anomaly` records `source` ∈ `deterministic` | `model` | `jev` | `human`. A
probabilistic finding is never presented as a verified fact. Anomalies are surfaced
with their evidence; they do not by themselves fail a run unless policy says so.

## Alternatives considered

| Option | Why rejected |
|---|---|
| Trust openpyxl's cached formula values | Stale or absent; would be exactly the "looks correct" claim the spec forbids |
| `pycel` / `formulas` for recalculation | Measured: does not work on the benchmark workbook (limitations doc) |
| LibreOffice headless for real recalculation | Genuinely works, but adds a ~500 MB binary dependency and significant runtime. Correct long-term answer, disproportionate for v1. Recorded as the recommended path if recalculation becomes a hard requirement |
| Verify only that the file opens | Proves nothing about the outcome |
| Let the executor verify its own work | Self-confirming. Verification must be independent of the code that made the change |
| Treat verification failure as a warning | Defeats the purpose. It fails the run |

## Consequences

**Positive**
- "Saved" and "verified" are genuinely different, and the difference is visible in the
  exit code.
- Static formula analysis catches the realistic failure modes: a formula overwritten
  by a literal, a reference broken by a sheet rename, a column of formulas deleted by
  a bad sort.
- Verification re-reads from disk, so it validates the actual artifact rather than
  in-memory state.

**Negative — stated plainly**
- **No formula recalculation.** ExcelPilot cannot prove that a formula computes the
  right value. It can prove the formula is present, well-formed, consistently applied,
  and not newly broken. This is the project's most significant limitation.
- Reconciliation on formula-derived columns is weaker than on literal data columns,
  and is labelled as such rather than silently degraded.
- Reference-graph resolution covers the common reference forms; exotic dynamic
  constructs (INDIRECT, OFFSET chasing external workbooks) are detected as
  `unresolvable` and reported, not verified.
