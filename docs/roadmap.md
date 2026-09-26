# Roadmap

Post-baseline work. **Nothing here is scheduled, and nothing here is a
priority ranking** — the order below is thematic, not a claim about what matters
most.

Each item states the current limitation, why it matters, the evidence that would
justify building it, and its likely architectural impact. That last column is
included because most of these are *not* small, and pretending otherwise is how a
baseline gets quietly rewritten.

The bar for all of it: **do not implement because a limitation is listed here.
Implement because a measurement or a user demonstrated the limitation has a cost.**

---

## Integrity

### Part-level content comparison

**Current limitation.** Verification compares the set of OOXML part *names*. A
part that survives with altered contents is not detected. Comments and drawings
are detected as *absent* but are still absent from the output.

**Why it matters.** The current check answers "did anything disappear?" It does
not answer "did anything change?" A drawing that survives but loses its macro
binding would pass.

**Evidence that would justify it.** A reproducible case where a part survives a
run with different contents, found on a real workbook. A corpus of real workbooks
whose part *contents* differ after a round trip, measured rather than argued.

**Likely impact.** Moderate. A part-level content hash map, compared per part, is
a contained change to `app/verification/structural.py`. Deciding whether a
difference is *acceptable* is the hard part — that needs a per-part
semantic classification, which is where the real cost sits.

### Preserving the parts that are currently lost

**Current limitation.** Cell comments and drawings are dropped from the output.

**Why it matters.** Comments carry annotations a user expects to survive.
Drawings on a macro workbook include the buttons that invoke the macro.

**Evidence that would justify it.** A user need, plus a decision about the
*semantics* of preservation: is copying a part forward verbatim correct when the
cells it references have changed? For a comment, usually. For a drawing bound to
a macro, frequently not.

**Likely impact.** Large, and it is the item most likely to force an architectural
change. openpyxl does not model these parts, so this means post-processing the
saved package at the zip level, and deciding which relationships to rewrite.
Doing it naively produces a corrupt file, which is worse than a documented loss.
This is a design cycle, not a task.

### Richer change reporting

**Current limitation.** The diff is cell-level: values, formulas, formatting
counts, sheets, defined names, tables.

**Why it matters.** A user reviewing a dry run sees counts, not intent. "2,000
cells changed" says less than "5 duplicates removed by InvoiceId, 3 whitespace
normalisations".

**Evidence that would justify it.** Operators saying the current manifest is not
enough to decide whether to approve. A measurement of how often dry runs are
approved without being read.

**Likely impact.** Small to moderate. The execution state already carries
per-operation details; surfacing them in the manifest and the report is mostly
presentation.

---

## Spreadsheet fidelity

### Broader formula support

**Current limitation.** Recalculation via `formulas` implements a subset of
Excel. Unsupported functions surface as errors rather than wrong numbers, which
is the safe direction, but the run still fails verification.

**Why it matters.** A workbook using a function outside the subset cannot be
processed at all, even for a change that never touches it.

**Evidence that would justify it.** A count of real workbooks hitting unsupported
functions. That requires a corpus and a function-usage scan — neither exists.

**Likely impact.** Depends entirely on the answer. More `formulas` coverage is
free; replacing the evaluation engine is not. Note also that evaluation is
*optional* — a deployment without the extra degrades to static checks, and that
must remain the supported path.

### Additional Excel semantics

**Current limitation.** Pivot tables, charts, slicers, and some drawing objects
are not round-tripped. There is no calendar, date-system, or locale handling
beyond what openpyxl provides.

**Evidence that would justify it.** A concrete workbook a user needs processed
that hits one of these.

**Likely impact.** Feature work, not architecture work. Some of it is blocked on
the preservation question above rather than being independent of it.

---

## AI / JEV evaluation

### A live planner evaluation methodology

**Current limitation.** The LLM planner was **never run against a live
provider**. It is contract-tested; its real responses are unobserved.

**Why it matters.** Every claim about plan quality rests on the *deterministic*
planner. The LLM path is unmeasured, so it is neither recommended nor ruled out.

**Evidence that would justify it.** Its own methodology: a fixed corpus of
requests, a fixed set of workbooks, a defined correctness criterion per request,
and a cost ceiling. None of that exists. **Designing the evaluation is the work**;
running it without a methodology would produce numbers nobody could interpret.

**Likely impact.** None to the architecture — it is an offline exercise. The
constraint is that the evaluation must not become a CI gate, and must not send
workbook contents to a provider.

### Calibration of the JEV thresholds

**Current limitation.** `min_probability` (0.8) and `min_margin` (0.15) are
upstream defaults, which upstream itself describes as *"uncalibrated starting
points, not deployment recommendations."* One live call returned two of four
questions at p=0.50.

**Why it matters.** Thresholds that are too low let weak advice through;
too high escalates everything and makes the approval gate noise.

**Evidence that would justify it.** A labelled set of decisions where the
"correct" answer is known, large enough to compute a false-accept and
false-escalate rate at candidate thresholds. One live call establishes nothing
here.

**Likely impact.** Small technically — configuration only. The hard part is
obtaining ground truth, which is why this is a project rather than a change.

### Failure-mode analysis for JEV

**Current limitation.** A well-formed but *confidently wrong* JEV answer is not
detectable. The asymmetric-OR design bounds the damage: the worst it can do is
escalate something policy would have allowed.

**Why it matters.** That bound is argued, not measured.

**Evidence that would justify it.** The labelled set above, plus deliberately
adversarial decisions, to confirm the bound empirically.

**Likely impact.** Analysis only. If the bound were found to be wrong, the
response would be a policy change, not a JEV change.

---

## Security and platform

### Authentication

**Current limitation.** Anyone who can run ExcelPilot has its full authority.
There is no user model and no permission system.

**Why it matters.** Fine for a single-operator tool on a laptop; not for a shared
host, a scheduled job, or a server.

**Evidence that would justify it.** A deployment where more than one party runs
it, or where the workspace is reachable by others. This is a **deployment**
decision, not a code one.

**Likely impact.** Large, and it changes the product's shape — the current
"single operator, single workspace" assumption is baked into the run store, the
lock, and the approval model. It should not be bolted on.

### Detecting a disabled XML hardening environment

**Current limitation.** `OPENPYXL_DEFUSEDXML=False` makes openpyxl fall back to
stdlib XML parsing with entity expansion enabled. ExcelPilot neither prevents
nor prominently detects this.

**Why it matters.** A deployment that sets that variable has silently removed an
XML security control, and nothing in the product says so.

**Evidence that would justify it.** This is arguably already justified — the
protection is real and the switch is undocumented in most deployment guides.
It is small work, unlike most items here.

**Likely impact.** Small. A startup-time check that surfaces a loud warning, and a
note in the verification report's notes list so it appears in every run's output.

### Broader threat-model coverage

**Current limitation.** The threat model
([`security.md`](security.md)) is documented and tested, but rests on one
reviewer and one pass. Areas not exercised adversarially: concurrent runs against
the same workbook, filesystem-level attacks (symlink races between resolution and
write), and zip archives crafted to be slow rather than large.

**Evidence that would justify it.** A structured second review, or a fuzzer over
the workbook reader.

**Likely impact.** Unknown until the review, which is the point.

---

## Operations

### Stronger rollback and recovery

**Current limitation.** Rollback is "delete the versioned output". There is no
undo, no re-run from a snapshot, and no recovery if an output is consumed before
being checked.

**Why it matters.** Deleting the output is safe but manual, and it assumes
somebody noticed. For a destructive change that is a thin margin.

**Evidence that would justify it.** A user asking for undo, or an incident where a
bad output was consumed.

**Likely impact.** Moderate. The snapshot already exists and is a byte copy, so
the raw material is there. The design question is what "revert" means when the
run created a new file rather than modifying one — arguably it is already
achieved, and the work is in making that obvious rather than in new capability.

### Richer replay

**Current limitation.** `replay` reconstructs what a run did. It re-executes
nothing by default, and the record is a summary rather than a re-derivation.

**Why it matters.** A summary can disagree with reality. An auditor wants
"show me that this output is what that input and that plan produce".

**Evidence that would justify it.** Someone needing to prove reproducibility
rather than read a record.

**Likely impact.** Moderate. Re-deriving means re-running the planner and
executor against the stored snapshot — which is a *write*, so it needs its own
workspace and its own run id. The current design already reserves a new run id
for a replay that executes, so the groundwork is laid.

### Expanded observability

**Current limitation.** Run records, manifests, and audit logs are comprehensive
per run. There is no cross-run view: no trend of failure rates, no alerting, no
export for external analysis.

**Why it matters.** Individually inspectable artefacts are not a fleet view.

**Evidence that would justify it.** Operating ExcelPilot on more than a handful of
workbooks, or needing to answer "how often does verification fail, and on what".

**Likely impact.** Small to moderate. The JSONL is already machine-readable;
most of this is a consumer, not a change to the writer.

---

## Explicitly not planned

Recorded so their absence is a decision rather than an oversight.

| Not planned | Reason |
|---|---|
| **Arbitrary code execution** | Deliberately excluded from the specification. A crafted workbook value must never become code |
| **VBA authoring or editing** | openpyxl cannot reason about a macro project; `vba_read_only` is a hard deny for that reason. Editing macros is a different product |
| **A general OOXML diff engine** | Would mean reimplementing OOXML understanding. The part-inventory check is the smallest thing that made the verdict honest; going further is a design cycle |
| **A web dashboard** | Would have to re-implement the approval and policy gate to be trustworthy, and a UI that bypasses either is worse than no UI |
| **Multi-workbook operations** | Every safety property is scoped to one workbook and one workspace |
