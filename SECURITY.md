# Security Policy

## Scope

ExcelPilot reads, plans, and writes Excel workbooks on behalf of a user. Its
security model is therefore mostly about **what happens to a file someone did
not expect to be modified**, and about **what happens when the input is hostile**.

The threats it is designed against, and the controls for each, are documented in
[`docs/security.md`](docs/security.md). Read that for the substance; this file
is about how to report a problem.

## Reporting a vulnerability

**There is no private reporting channel yet.** No repository, mailing list, or
security contact exists for this project, so there is nowhere to report
confidentially.

Until one does, open a public issue. Before you do, consider that a public issue
is visible immediately and permanently.

If that is not acceptable for what you have found, the honest options are to hold
the report until a private channel exists, or to raise it privately with whoever
maintains this repository through whatever channel you already have with them.

## What is worth reporting

More valuable, in rough order:

| Finding | Why it matters |
|---|---|
| A path that lets output escape the workspace | The whole safe-output model depends on it. Traversal, symlink escape, and absolute-path cases are all in scope |
| A way to overwrite the source workbook | The product's central guarantee is that the original is never modified |
| A way to make policy allow something its hard-deny rules forbid | Six rules are documented as non-configurable |
| A way for JEV or model output to reach execution without passing policy | The AI/JEV advisory boundary is structural, not conventional |
| A way to bypass the approval gate or the dry-run guarantee | Both are safety properties, not conveniences |
| Secret or workbook-content leakage into logs, audit records, or the JEV payload | The payload is allowlist-built specifically to prevent this |
| An unblocked prompt-injection path that reaches a cell write | Scanning is heuristic; the structural boundary is the real defence |
| Silent workbook corruption — output losing content while reporting success | Verification's job is to make this impossible |

## What is already known and documented

These are **not** vulnerabilities. They are documented limitations, and reporting
them as new findings wastes review time:

- Cell comments and drawings are lost from the **output** because openpyxl does
  not round-trip them. Verification now **detects** this and fails the run; the
  loss itself is not prevented. See
  [`docs/limitations.md`](docs/limitations.md) §3–4.
- Formula recalculation implements a subset of Excel.
- Writes to macro-enabled workbooks are refused by design.
- The LLM planner has never been run against a live provider.
- There is no authentication, and anyone who can run ExcelPilot has its
  authority. This is out of scope, not an oversight.
- Rollback is manual: delete the versioned output.
- Setting `OPENPYXL_DEFUSEDXML=False` disables XML entity-expansion hardening,
  and ExcelPilot neither prevents nor detects that. See
  [`docs/security.md`](docs/security.md).

## Out of scope

- Malicious or misbehaving ExcelPilot configuration. Anyone who can edit the
  config can edit the policy thresholds.
- A local attacker with write access to the process, or host-level compromise.
- Denial of service from a deliberately enormous workbook, beyond the
  configured resource limits. Those limits exist to bound it, not to eliminate
  it.
- The correctness of ExcelPilot's natural-language interpretation. It refuses
  what it cannot resolve rather than guessing; a request it resolves
  differently than you intended is a usability question, not a security one.
