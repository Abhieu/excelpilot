# ADR 0009 — Diff strategy: content hashing

- **Status:** Accepted
- **Date:** 2026-09-25

## Context

The change manifest is how an operator answers "what did this run actually do to my
workbook?". It must be accurate, explainable, and stable across runs.

A tempting shortcut is to compare files. That is wrong here, and measurably so.

## Evidence

A load → save → re-save cycle of an **unchanged** workbook produces a different file:

```
Sunset Tracker.xlsx
  byte-identical after round-trip: False
  xl/sharedStrings.xml           -> dropped
  xl/comments1.xml               -> xl/comments/comment1.xml   (relocated)
  69,221 formulas / 70,002 fills / 16 defined names  -> all preserved
```

openpyxl re-serialises the OOXML zip. Part ordering, compression, and the shared-string
cache all change on every save, whether or not ExcelPilot touched a single cell.

**Therefore byte comparison would report a change on every run, for every workbook.**
A diff that cries wolf is worse than no diff: operators learn to ignore it.

## Decision

Diff is computed on **logical cell content**, not on bytes or zip parts.

### Fingerprint

For each sheet, for each non-empty cell, a stable record:

```
sheet | coordinate | value-repr | data_type | number_format | style_id
```

Keyed and compared by `(sheet, coordinate)` — a *logical location*, deliberately
independent of zip part paths, which are not stable (§Evidence).

`value-repr` normalises: `datetime` → ISO-8601, `float` → repr with trailing-zero
normalisation, `None` → `""`, `bool` → `"TRUE"/"FALSE"` (Excel's own convention).
Without this, float noise and timezone representation would produce phantom diffs.

The workbook-level fingerprint is a SHA-256 over the sorted per-cell records, so it
can be compared in O(1) and recorded in the audit trail.

### `WorkbookDiff` reports

sheets added / removed / renamed; per-sheet cells added / removed / changed; formulas
added / removed / changed; formatting-only changes; row and column count deltas; table
changes; named-range changes; defined-name changes; a `structural_change` flag; and
per-change before/after values, capped and summarised beyond a limit.

### Detecting unexpectedly large change

The diff itself flags when the change set exceeds the configured expectations
(operations planned vs cells actually changed). A large divergence between planned and
actual is itself an anomaly — that is how an operation behaving unexpectedly gets
caught, rather than being reported as success.

## Alternatives considered

| Option | Why rejected |
|---|---|
| Byte/hash comparison of the file | Reports a change on every run (proven above) |
| Compare zip part listings | Part paths are not stable (proven above) |
| `openpyxl`'s own comparison utilities | None exists for this purpose |
| `pandas` merge/compare | Loses formulas, styles, and non-data cells; cannot see structural change |
| XML tree diff of the sheet parts | Extremely noisy; produces changes on every save for the same reason as bytes, and is unreadable for an operator |

## Consequences

**Positive**
- Zero false positives from serialisation.
- Directly meaningful to an operator: "Sales!C14 changed from `=SUM(B2:B13)` to `142`".
- Independent of the writer implementation, so it stays valid if the engine changes.

**Negative**
- **Not byte-faithful.** A workbook ExcelPilot does not touch is not reproduced
  byte-for-byte. Preserving the original is therefore done by *copying the file*, not
  by re-serialising it (ADR-0010).
- Cell-level diff is O(cells). Acceptable at the sizes involved here; a 37,883×120
  workbook (~4.5M cells) is bounded by the resource limits in `app/workbook/limits.py`,
  and diff on sparse sheets is optimised to iterate only non-empty cells.
- Style comparison uses `style_id`, which is an index into a workbook-local style
  table and can be renumbered on save. Mitigated: `style_id` is used only to detect
  *whether* formatting changed in a cell, and the manifest reports the count of
  formatting-changed cells rather than claiming an exact style equality. This is
  stated in the manifest's own `notes` field so no consumer over-reads it.
