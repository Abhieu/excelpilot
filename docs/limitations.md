# Limitations

What ExcelPilot cannot do. Read this before trusting it with anything that
matters.

Every item here is a real, measured constraint — not a hedge. Where something
was investigated and found workable, that is said too, because a limitations
document that only lists negatives does not tell you where the edges are.

---

## 1. Formula recalculation is available, but conditional and narrow

**Status: integrated, optional, and honestly reported.**

Recalculation works. It is available as the optional `recalc` extra
(`formulas` 1.3.4) and was verified against Python ground truth: `Sales!G2..G4`
and cross-sheet `Summary!B2..B4` all matched exactly, with 205 formulas in 0.30 s.
A missing external reference fails loudly rather than silently.

**But:**

- It requires the extra. Without it, verification is static and says so.
- It is not Excel. `formulas` implements a subset of Excel's function set. An
  unsupported function or an exotic behaviour will not match Excel, and the
  failure surfaces as an error rather than a wrong number — which is the right
  direction, but it does mean recalculation is not a guarantee.
- Pivot tables, dynamic arrays, and newer functions are not supported.
- It can be slow on large workbooks, which is why it is bounded by
  `max_recalculation_cells` and a timeout.

**The report is always honest about which happened.** `recalculated` and
`static_formula_checks` are mutually exclusive and exactly one is true, so a
static result can never be read as an evaluated one. This distinction was
originally documented backwards, and the stale text was found and corrected
during the documentation audit.

## 2. The LLM planner has never run against a live provider

The `openai_compatible` planner is implemented and contract-tested, but
`model.enabled` defaults to `false` and **no live call was ever made** — no LLM
credential exists in this environment.

Its output is untrusted either way: parsed into typed contracts, rejected on
validation failure, and subject to the same policy as a hand-written plan. But
contract tests are not a substitute for having seen real responses, and a live
model could return shapes the contract does not anticipate.

**The deterministic planner is the default, needs no credential, and is what
everything measured here actually used.**

## 3. Macro-enabled workbooks: read and preserve, never write

**Status: measured against a real 152 KB VBA project on 2026-09-26.** This
section previously said the behaviour was unverified. It is now measured, and the
answer is more specific than "preserved".

### What ExcelPilot does

| Capability | Measured result |
|---|---|
| **Read / inspect** | Yes. Opens the workbook, reads all sheets, reports `has_vba: true` |
| **Detect** | Yes. `has_vba()` inspects the package, not the extension |
| **Preserve** | Yes — `vbaProject.bin` survives a read/save round trip **byte-for-byte** (SHA-256 identical, 152,576 bytes in and out) |
| **Transform** | **No.** Every mutating operation is denied |
| **Mutate** | **No.** Unconditionally refused |
| **Author** | No. VBA authoring is out of scope entirely |

The hard rule is `vba_read_only`, a **hard deny** rather than an escalation.
`--approve` does not override it, and no configuration file can disable it — both
are asserted by tests. A run on a macro-enabled workbook returns
`rejected_by_policy` with the reason *"workbook contains a VBA macro project;
ExcelPilot can read it but will not write to it"*.

Read-only operations *are* permitted. The rule refuses writes, not reads.

### What the measurement found that was not previously known

Two silent content-loss defects, both found during the release-hardening audit.
Both are **fixed**; what remains is the underlying openpyxl limitation.

**1. A read-only run silently stripped the macro project.** The source snapshot
was named `source.snapshot.xlsx` regardless of the source's real extension. The
executor opens the snapshot, not the user's file, and the reader decides
`keep_vba` from the extension it sees — so a `.xlsm` snapshotted as `.xlsx` was
loaded with `keep_vba=False` and openpyxl dropped `xl/vbaProject.bin` on save.
A read-only run, which policy deliberately *permits* on a macro workbook,
produced an `.xlsm` output containing no macros, while the source stayed intact
and every other check passed.

Fixed: the snapshot now keeps the source's real extension. A read-only run on a
macro workbook now succeeds with the project byte-identical. Covered by
`tests/test_content_loss.py`.

**2. Dropped OOXML parts were invisible to every check.** openpyxl does not
round-trip every part. Measured on real workbooks, a read/save drops:

| Part | What is lost |
|---|---|
| `xl/drawings/drawing1.xml` | Shapes, including ones bound to a macro (`<xdr:sp macro="[0]!SomeMacro">` — the button a user clicks to run it) |
| `xl/comments1.xml` … `comments7.xml` | **Cell comments** — seven parts lost on one real workbook |
| `xl/drawings/vmlDrawing1.vml` | The VML anchor for legacy comments |

This is not macro-specific. A **plain `.xlsx`** carrying a drawing went through
the ordinary approved run path and reported `succeeded`, `status: passed`, zero
anomalies, and `structural_change: false` — while the drawing was destroyed.
openpyxl's own warning says the same thing: *"DrawingML support is incomplete…
Shapes and drawings will be lost."*

Fixed: verification now compares the set of OOXML part names before and after,
and **fails** the run if any part disappeared other than two known-benign caches
(`xl/sharedStrings.xml` and `xl/calcChain.xml`, both of which Excel rebuilds and
whose values are verified exact by the data checks). It is a name-set comparison,
deliberately the smallest mechanism that makes the verdict honest — it does not
merge, repair, or understand the parts it finds.

**What is still true:** the content is still lost *in the output file*. What
changed is that you are now told, the run fails, and the untouched source
survives — so discarding the output loses nothing. The underlying openpyxl
limitation is unchanged and is not fixed.

### Why no macro fixture is committed

Real macro workbooks are somebody's business data: they embed Windows usernames,
absolute business paths, and proprietary macro code. None is in this repository,
and no filesystem location is assumed anywhere in it. The real-workbook tests are
supplied at test time through `fixtures/real.py` (see
[`implementation-plan.md`](implementation-plan.md) §7.4) and skip when not
configured.

The committed fixture is instead generated from reviewable source
(`fixtures/vba.py`): a genuinely macro-enabled package — correct
`[Content_Types].xml`, correct `xl/_rels/workbook.xml.rels` reference, a valid
OLE/CFB container with a self-consistent FAT and a named stream.

**That fixture is a structural stand-in, not a real VBA project.** It has no
`dir` stream and no MS-OVBA modules, so Excel would not run macros from it. It
exercises everything ExcelPilot does with a macro workbook — extension handling,
`keep_vba`, byte preservation, package validity, the policy denial — none of
which depends on the macro source being meaningful. The real-project claims rest
on the skipped-by-default `TestRealMacroWorkbook` tests, not on it.


## 4. openpyxl does not preserve every OOXML part

Measured on a real workbook:

| Observation | Effect |
|---|---|
| Byte content differs after every save | Change detection is content-based, never byte-based (ADR-0009) |
| `xl/sharedStrings.xml` is dropped | String values remain exact; the cache is rebuilt. Not a data loss, but part identity is not stable |
| Comment parts relocate `xl/comments1.xml` → `xl/comments/comment1.xml` | Manifests key on logical location, not zip path |

So a "snapshot" must be a **byte copy**, not a re-save. It is.

Other parts openpyxl does not round-trip — charts, some drawing objects,
slicers, pivot caches — will be lost. If a workbook has them, do not run a change
through ExcelPilot and expect a faithful copy.

**Drawing parts and comments specifically: measured, not assumed.** On the real
macro workbook, `xl/drawings/drawing1.xml` and
`xl/worksheets/_rels/sheet3.xml.rels` were both dropped by a round trip, while
`xl/vbaProject.bin` was preserved byte-identically. On a second real workbook,
**seven comment parts** were dropped. `xl/theme/theme1.xml` was the only other
part preserved byte-for-byte.

This loss is now **detected**: verification compares part inventories and fails
the run when content disappears. It is not *prevented* — openpyxl still does not
round-trip these parts, and ExcelPilot does not attempt to repair them. The
guarantee is that you are told, and that the untouched source is still there.
See §3.

## 5. Unsupported Excel features

| Feature | Status |
|---|---|
| Charts | Not implemented. The typed operation namespace is reserved; nothing is built |
| Pivot tables | Not implemented, and not preserved through openpyxl |
| Slicers, timelines | Not implemented |
| Conditional formatting | Preserved on round-trip; not created or edited |
| Data validation | Preserved on round-trip; `ApplyValidation` can create it |
| Legacy `.xls` | Out of scope. XLSX and XLSM only |
| Macros | Read and preserved, never written, never authored |
| Cross-workbook / multi-file operations | Out of scope. Single workbook per run |
| Concurrent or multi-user runs | Out of scope. No locking beyond the single-run lock |

## 6. The natural-language planner is a rule engine, not a model

The default planner is deterministic and pattern-based. It handles the common
shapes — normalise, deduplicate, summarise, sort, filter, rename, add a sheet,
reconcile — and **refuses** when a request is not specific enough.

It does not understand arbitrary phrasing. If a request does not match its
patterns, `no_guessing` denies it. That is the correct trade: a refusal is
recoverable, a wrong guess on a spreadsheet is not.

Its limits are known and were found the hard way — eight planner recall and
precision bugs are recorded in `tests/test_regressions.py` with their reasoning.

## 7. JEV limitations

- **One live call was made.** That validates the integration end to end. It
  establishes nothing about accuracy, reliability, or latency distribution, and
  no such claim is made.
- Two of the four questions came back `needs_review` at 0.5 probability, so the
  model found the probe context genuinely ambiguous. That is the conservative
  path working, but it means the thresholds are not well calibrated.
- `min_probability` and `min_margin` are upstream's own uncalibrated defaults.
- A well-formed but **confidently wrong** JEV answer is not detectable from
  here. The asymmetric-OR design bounds the damage: the worst it can do is
  escalate something policy would have allowed.
- The `jev-decide` CLI is broken on this machine (bad shebang). The adapter calls
  the HTTP contract directly and does not use it.

## 8. The benchmark does not measure what you might hope

Eleven synthetic scenarios on small generated workbooks are not a workload. It
reports **no quality score**, because there is no ground truth against which to
score one. Timing differences between modes were **not measurable** — the
run-to-run spread exceeded every difference — so no performance claim is made
for any mode.

It also made no claim about accuracy, because it cannot measure it. See
[benchmarks.md](benchmarks.md).

## 9. Scale

Tested against a real 37,883 × 120 workbook with ~69,000 formulas. Inspection
takes ~70 s and a round trip ~226 s. That is acceptable for a supervised,
human-in-the-loop operation and **not** acceptable for an interactive one.

Cell and formula ceilings (`max_total_cells` 20 M, `max_formula_count` 2 M) are
configurable, and raising them is a decision with consequences.

## 10. No dashboard, and no authentication

There is **no web UI**. It was deliberately not built: it would have to
re-implement the approval and policy gate to be trustworthy, and a UI that
bypasses either is worse than no UI. The CLI cannot skip a stage, which is
partly why it is the only interface.

There is **no authentication, no user model, and no multi-tenancy**. ExcelPilot
runs as whoever runs it. Anyone who can edit the configuration can edit the
policy thresholds, and anyone with write access to the workspace can read the
output.

## 11. Rollback is "discard the output"

There is no undo. That is a deliberate consequence of never overwriting the
source: rollback means deleting the versioned output, because nothing was
replaced. This is safe but manual, and it means a bad run leaves a bad file on
disk until someone removes it.

## 12. Injection detection is heuristic

The scanner looks for instruction-override phrasing, role reassignment, fake
turn and tag boundaries, exfiltration attempts, credential references, policy
bypass attempts, and destructive instructions. A sufficiently indirect injection
may not match any pattern.

The mitigation is structural: **a model cannot mutate a workbook.** The worst a
successful injection achieves is a *request* for an operation that policy then
evaluates on its merits. Detection is defence in depth, not the boundary.

## 13. Other constraints

| Constraint | Detail |
|---|---|
| Local filesystem only | No cloud storage backend |
| Python 3.11–3.13 | 3.14 was excluded because wheel availability for the recalculation stack is unproven there |
| No concurrency model | One run at a time per workspace |
| Audit trails are deletable | `excelpilot gc` removes them. Deleting a run removes its record |
| openpyxl XML parsing is not hardened against every malformed-package attack | Resource limits bound the blast radius; they do not eliminate it |
| `pytest` warning | A `ZipFile.__del__` GC-timing artefact, cosmetic, present at the previous commit |
