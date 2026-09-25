# ADR 0001 — Workbook engine: openpyxl

- **Status:** Accepted
- **Date:** 2026-09-25
- **Deciders:** ExcelPilot core

## Context

ExcelPilot must inspect, mutate, and re-save XLSX workbooks while preserving
formulas, formatting, tables, defined names, and data validation. A workbook that
loses formulas on save is a data-integrity failure, not a cosmetic one.

The workspace contains real workbooks as the fidelity bar: `Sunset Tracker.xlsx`
(6 sheets, 69,221 formulas, 70,002 fills, 219 merged ranges, 36 conditional
formats, 16 defined names, 3 tables) and `VBA_Seed.xlsm` (826 formulas,
10 sheets including a `veryHidden` sheet).

## Decision

Use **openpyxl 3.1.5** as the sole workbook engine. XLSX and XLSM only.
`.xls` (legacy binary) is not supported.

## Alternatives considered

| Option | Why rejected |
|---|---|
| `pandas` + `xlsxwriter` | Writes a *new* workbook from a DataFrame. Destroys formulas, styles, merged cells, and any sheet it does not model. Unusable for in-place operational edits. The sibling project `AirtelGLAutomation` uses this pattern for report generation, which is a different problem. |
| `xlrd` / `xlwt` | `.xls` only. Wrong format. |
| `pyexcelerate` | Unmaintained; weak formula/style support. |
| LibreOffice headless | Genuinely recalculates, but requires a ~500 MB binary, is slow, and is a large external dependency for a library. Deferred — see ADR-0011. |
| Direct OOXML/zip manipulation | Full fidelity control including recalculation-free cache handling, but reimplementing an OOXML engine is far beyond an MVP and would be less safe than a mature library. |

## Reasoning

Measured on the actual fixtures, not assumed:

```
Sunset Tracker.xlsx   load -> save -> reload
  sheets 6->6   cells 69,660->69,660   formulas 69,221->69,221
  fills 70,002->70,002   merges 219->219   data validations 18->18
  conditional formats 36->36   defined names 16->16   tables 3->3
```

Cell values on `VBA_Seed.xlsm`: **5,873 / 5,873 identical**.

Two losses were found and are documented rather than hidden:

1. `xl/sharedStrings.xml` is dropped (values unaffected; the cache is rebuilt).
2. Comment parts relocate `xl/comments1.xml` → `xl/comments/comment1.xml`.

Both are why change detection is content-based, not package-based (ADR-0009).

openpyxl is also the only mature option that preserves VBA via `keep_vba=True`,
which matters because the user's real macro workbooks are `.xlsm`.

## Consequences

**Positive**
- Verified lossless round-trip on the real fixture set.
- Preserves formatting, formulas, tables, defined names, conditional formatting.
- `keep_vba=True` available for `.xlsm`.
- Pure Python; no external binary.

**Negative**
- **No formula recalculation.** openpyxl reads formulas as strings and, with
  `data_only=True`, reads whatever cached value Excel last wrote. It cannot
  compute. Any "verify totals" feature must be built on deterministic recomputation
  of the *data*, not on trusting cached values (ADR-0011).
- Does not preserve every OOXML part (see above).
- Some features are unsupported or lossy: pivot table *creation*, chart creation,
  and slicers. These are deferred with typed extension points, not half-built.
- Whole-file memory model: a large workbook is loaded entirely into RAM. Mitigated
  by explicit resource limits in `app/workbook/limits.py`.

**Superseded if** a real requirement for pivot/chart authoring or true
recalculation appears. That would justify the LibreOffice route.
