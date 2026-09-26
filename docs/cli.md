# The CLI

Every command below was run; the output shown is real. Installed as
`excelpilot`, and runnable as `uv run excelpilot`.

## Global options

Available on most commands:

| Option | Effect |
|---|---|
| `--json` | Emit a versioned JSON document on stdout. Errors go to stderr. |
| `--config`, `-c` | Path to a TOML configuration file. |
| `--workspace`, `-w` | Workspace root. All paths are confined to it. |
| `--verbose`, `-v` | More detail on stderr. |

## Exit codes

Distinct codes are the point: a script can tell "policy said no" from "the output
was written but verification failed" without parsing text.

| Code | Meaning |
|---:|---|
| 0 | success, dry run, or rolled back |
| 1 | the run failed — including a policy denial caught at **execution** time |
| 2 | usage error, including a missing or unreadable workbook path |
| 3 | policy denied the run at the **planning** stage |
| 4 | approval required (or explicitly rejected) |
| 5 | verification failed |
| 6 | paid call blocked |
| 7 | an unknown **run id** |

**Code 5 is the load-bearing one.** A file was written, but the outcome was not
established. Returning 0 there would make "saved" indistinguishable from
"verified", which is the distinction the whole verification layer exists to
preserve.

A dry run that stops at the approval gate returns **0** — nothing was attempted,
so nothing failed.

### Two distinctions worth knowing

**"Not found" means a run id, not a file.** Passing a workbook path that does not
exist is a *usage* error (2), because the argument itself is wrong. Exit 7 is
reserved for a run id that is not in the store — `replay run-nope`,
`verify --run run-nope`.

**Policy denial has two exit codes, by design.** A denial at the planning stage
is 3: nothing was attempted, and the refusal is the whole answer. A denial caught
at execution time is 1: the run had already entered execution and then stopped.
The distinction exists because the executor re-checks policy as defence in
depth, so a denial there means something changed between planning and execution.

In both cases the **rule that fired is recorded**, in `run.json`
(`policy_rule_ids`), in the audit trail, and in the `--json` payload under
`policy.rule_ids`. A script that needs to know *which* control stopped a run
should read the rule id rather than infer it from the exit code.

## Exit codes in practice

```console
$ excelpilot run sales.xlsx -t "..." -w "$D"; echo "exit=$?"
exit=4

$ excelpilot run sales.xlsx -t "..." --approve -w "$D"; echo "exit=$?"
exit=0

$ excelpilot run sales.xlsx -t "..." --approve -w /elsewhere; echo "exit=$?"
ERROR remove_duplicates: policy denied this operation at execution time: output
path /elsewhere/sales__run-….xlsx is outside the workspace root /path/to/project
exit=1
```

That third case is worth reading twice: policy passed the path at planning time
and the executor re-checked it at execution time and denied it. Defence in depth
is not a slogan.

---

## `inspect`

Read-only. The file is never modified.

```console
$ excelpilot inspect $D/sales.xlsx
───────────────────────────── Workbook: sales.xlsx ─────────────────────────────

  path:                        /tmp/excelpilot-demo/sales.xlsx
  size:                        9,641 bytes
  sha256:                      b7eabba9406458afc5345849f5a59df3...
  sheets:                      3
  hidden sheets:               1
  total rows:                  74
  formulas:                    65
  non-empty cells:             599
  tables:                      0
  defined names:               0
  sensitivity:                 public
  has macros:                  False
  external links:              False

                          Sheets
┏━━━┳━━━━━━━━━┳━━━━━━━━━┳━━━━━━┳━━━━━━┳━━━━━━━━━━┳━━━━━━━━┓
┃ # ┃ Name    ┃ State   ┃ Rows ┃ Cols ┃ Formulas ┃ Tables ┃
┡━━━╇━━━━━━━━━╇━━━━━━━━━╇━━━━━━╇━━━━━━╇━━━━━━━━━━╇━━━━━━━━┩
│ 0 │ Sales   │ visible │ 63   │ 10   │ 62       │ -      │
│ 1 │ Summary │ visible │ 6    │ 2    │ 3        │ -      │
│ 2 │ _Lookup │ hidden  │ 5    │ 2    │ 0        │ -      │
└───┴─────────┴─────────┴──────┴──────┴──────────┴────────┘
```

The content hash is of the logical content, not the bytes — see
[ADR-0009](adr/0009-diff-strategy.md).

## `plan`

Reads, decides, writes nothing, costs nothing. The safest way to find out what
ExcelPilot thinks a request means.

```console
$ excelpilot plan $D/sales.xlsx -t "normalise the Customer column and remove duplicate invoices"
───────────────────────────────────── Plan ─────────────────────────────────────

  intent:                      normalize, remove duplicates on sheet 'Sales'
                               (chosen as the largest sheet; name it explicitly to target another)
  interpretation:              sufficiently_clear
  planner:                     deterministic
  operations:                  remove_duplicates, normalize_values

  note:                        duplicate removal keyed on ['InvoiceId']
  note:                        normalise applied to 1 column(s) on 'Sales'

──────────────────────────────────── JEV ─────────────────────────────────────
                                Decisions
┏━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━━━━━┳━━━━━━━━┓
┃ Question       ┃ Value              ┃ Status   ┃ Probability ┃ Margin ┃
┡━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━━━━━╇━━━━━━━━┩
│ automation     │ approval_required  │ selected │ 0.88        │ 0.54   │
│ risk           │ medium             │ selected │ 0.88        │ 0.54   │
│ interpretation │ sufficiently_clear │ selected │ 0.88        │ 0.54   │
│ verification   │ reconciliation     │ selected │ 0.88        │ 0.54   │
└────────────────┴────────────────────┴──────────┴─────────────┴────────┘
JEV is advisory. It can make a run more cautious; it can never authorise one.

──────────────────────────────────── Policy ────────────────────────────────────
  outcome:                     require_approval
    2 formula(s) would be removed or overwritten
    destructive operation(s) present: remove_duplicates
    JEV raised scrutiny: automation=approval_required (selected p=0.88); …
  rules:                       formula_removal, destructive_operation
```

Note `plan` shows JEV decisions. That is the **mock** adapter by default — no
network, no cost, no credential. See [jev.md](jev.md).

## `run`

Plan, decide, authorise, execute, verify, audit. The source is never written to.

| Option | Effect |
|---|---|
| `--task`, `-t` | The request. Required. |
| `--dry-run` | Measure without writing anything. |
| `--output`, `-o` | Output path. Must be inside the workspace. |
| `--approve` | Approve a run that policy escalated. |
| `--reject` | Reject one. |
| `--allow-paid-calls` | Permit live JEV/LLM calls. **Off by default.** |
| `--jev-scenario` | Mock JEV scenario for offline runs. Default `approve`. |
| `--run-id` | Use a specific run id, for replay and testing. |

### Dry run

```console
$ excelpilot run $D/sales.xlsx -t "$TASK" --dry-run -w $D

nothing was written and nothing was changed

────────────────────────────── APPROVAL REQUIRED ───────────────────────────────
  workbook:                    sales.xlsx
  intent:                      normalize, remove duplicates on sheet 'Sales' …
  operations:                  remove_duplicates, normalize_values
  sheets:                      Sales
  ranges:                      Sales!(used area)
  cells to change:             12
  formulas added:              0
  formulas removed:            2
  records removed:             2
  structural change:           no
  risk:                        MEDIUM
  policy:                      policy: require_approval via formula_removal,
                                destructive_operation
  proposed output:             /tmp/excelpilot-demo/sales__run-837ca44c50484139.xlsx
  verification:                structural_check, data_check, formula_validation,
                                value_check

OK  dry run run-837ca44c50484139 complete; nothing was written
```

It gives exact cell counts, formula removals, the proposed path, and which
verification checks will run — all before anything happens.

### Approved run

```console
$ excelpilot run $D/sales.xlsx -t "$TASK" --approve -w $D
…
Data:
  [OK  ] data_row_count: row count 60 (was 62)
  [OK  ] data_duplicate_rate: duplicate rate in 'Date': 3.2% -> 0.0%

Formula:
  [OK  ] formula_broken_references: no broken references found (static check)
  [OK  ] formula_preservation: all previously present formulas are intact; 2
         disappeared with rows the run deliberately removed
  [OK  ] formula_recalculated: 584 formula value(s) were evaluated with
         formulas; this is a real recalculation, not a static check
  [OK  ] formula_evaluation_errors: no formula evaluates to an Excel error

OK  run run-d0939b69c1014a07 completed and verified
```

`formula_recalculated` appears only when the `recalc` extra is installed and
evaluation actually completed. Without it, the formula checks are static and the
result says so.

## `diff`

Content-based, never byte-based. Saving a workbook rewrites the OOXML package
even when nothing changed, so bytes cannot answer "what changed".

```console
$ excelpilot diff $D/sales.xlsx $D/sales__run-d0939b69c1014a07.xlsx
───────────────────────────────────── Diff ─────────────────────────────────────

  identical:                   no
  structural change:           no

  cells changed:   12
  formulas changed: 2
  formatting:      0

Sample changes:
  Sales!I63: removed 'Paid' -> None
  Sales!B17: value 'UMBRELLA LTD' -> 'UMBRELLA LTD'
  Sales!G62: removed '=E7*F7' -> None
  …
```

Note `Sales!B17: value 'UMBRELLA LTD' -> 'UMBRELLA LTD'` — the same string. That
is a real change (the surrounding row moved when duplicates were removed), and
the diff reports it rather than hiding it.

## `verify`

Verify a workbook on its own, or against a run's source snapshot with `--run`.

```console
$ excelpilot verify $D/sales__run-<run-id>.xlsx -w $D
Verification: passed

  recalculated:      True
  static formula checks: False
```

Those two lines are the honest statement of what was done. A standalone check is
no weaker than the run it is checking — it honours the same
`verification.enable_recalculation` setting and accepts `--config`.

## `replay`

Reconstructs what a run did. Read-only: re-executes nothing by default.

```console
$ excelpilot replay run-d0939b69c1014a07 -w $D
───────────────────────── Replay: run-d0939b69c1014a07 ─────────────────────────
read-only: nothing was re-executed and nothing was modified

  outcome:                     succeeded
  state:                       completed
  duration:                    1.68s
  task:                        normalise the Customer column and remove duplicate invoices
  source:                      sales.xlsx (b7eabba9406458afc...)
  policy:                      require_approval
  approval:                    approved
  JEV:                         mock (called=True)
  verification:                passed
```

Replay never overwrites a workbook and never mutates the original run's
artefacts. A replay that does execute gets a new run id.

## `runs`

```console
$ excelpilot runs -w $D --limit 5
                                      Runs
┏━━━━━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━━┳━━━━━━━━━━━━┳━━━━━━━━━━━━━━┳━━━━━━━━━━━━━┓
┃ Run         ┃ Outcome   ┃ Duration ┃ Workbook   ┃ Task         ┃ Output      ┃
┡━━━━━━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━━╇━━━━━━━━━━━━╇━━━━━━━━━━━━━━╇━━━━━━━━━━━━━┩
│ run-6d6919… │ succeeded │ 1.2s     │ sales.xlsx │ normalize,   │ sales__run… │
└─────────────┴───────────┴─────────┴────────────┴──────────────┴─────────────┘
```

## `policy`

The effective policy: rules, thresholds, and what cannot be changed.

```console
$ excelpilot policy
──────────────────────────────────── Policy ────────────────────────────────────
  config fingerprint:          6fa45566a3981d93

Hard deny rules (cannot be disabled by configuration):
    source_never_overwritten
    output_within_workspace
    cell_ceiling
    operation_ceiling
    vba_read_only
    no_guessing

Escalation rules (set require_approval when they fire):
    bulk_change
    formula_removal
    structural_change
    destructive_operation
    hidden_sheet_change
    restricted_data
    ambiguous_task

────────────────────────────────── Thresholds ──────────────────────────────────
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━┓
┃ Name                          ┃ Value    ┃
┡━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━┩
│ cell_change_approval_threshold│ 1000     │
│ deny_cells_affected_above     │ 20000000 │
│ max_operations_per_plan       │ 50       │
│ row_change_approval_threshold │ 500      │
└───────────────────────────────┴──────────┘

Hard deny rules cannot be disabled by configuration.
JEV may raise the requirement for approval but can never lower it.
requires_approval = policy_requires OR jev_escalates
```

The `config fingerprint` is a hash of the effective configuration, recorded in
every run's audit trail. Two runs with the same fingerprint were judged by the
same rules.

## `gc`

```console
$ excelpilot gc --keep 50
```

Deletes the oldest run directories, keeping the newest 50. Run directories are
the audit record, so this is explicit rather than automatic. **Deleting a run
removes its audit trail.**

## `version`

```console
$ excelpilot version
```

---

## The audit trail

Append-only JSONL, one directory per run. Inspectable with `cat` and `jq` — no
database, no tooling.

```console
$ ls $D/.excelpilot/runs/run-d0939b69c1014a07/
audit.jsonl  manifest.json  output/  report.txt  run.json
source.snapshot.xlsx  verification.json

$ python -c "import json;[print(f\"{e['seq']:>2} {e['actor']:<7} {e['event_type']}\") for e in map(json.loads, open('$D/.excelpilot/runs/run-d0939b69c1014a07/audit.jsonl'))]"
 1 user    run.created
 2 system  workbook.inspected
 3 system  source.snapshot
 4 system  plan.built
 5 jev     jev.decided
 6 policy  policy.evaluated
 7 system  approval.requested
 8 user    approval.granted
 9 system  execution.started
10 system  execution.completed
11 system  output.written
12 system  verification.completed
13 system  diff.computed
14 system  run.completed
```

The `actor` field distinguishes `user`, `system`, `jev`, and `policy`. That
separation is what makes "JEV said yes" and "JEV was never asked" distinguishable
after the fact — `jev.decided` is present only when a decision was actually
returned.

Redaction happens as each record is written, so a secret never reaches the file.

---

## What is deliberately absent

There is **no** `--force`, `--no-verify`, `--skip-policy`, `--overwrite`, or
`--in-place`. A test asserts they do not exist, because the absence of an escape
hatch is only a real property if something checks it.
