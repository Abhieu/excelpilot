# Configuration

Configuration is TOML, loaded with `--config`. Every field is optional; the
defaults below are what runs when nothing is specified.

**Credentials never appear in this file.** ExcelPilot reads them from the process
environment only, and deliberately does not load a `.env` (ADR-0002) — a `.env`
in the working directory must not be able to silently redirect traffic or supply
keys.

```bash
uv run excelpilot policy --config my-config.toml
```

Every value below is the actual default, read from `ExcelPilotConfig()`.

## Top level

| Key | Default | Meaning |
|---|---|---|
| `workspace_root` | `"."` | All paths are confined to this. Resolved, so `..` and symlinks cannot escape. |
| `runs_dir` | `".excelpilot"` | Where run directories live, under the workspace. |
| `allow_paid_calls` | `false` | Default for the paid-call gate. Overridden by `--allow-paid-calls`. |
| `audit_enabled` | `true` | Write the append-only audit trail. |

## `[limits]`

Resource bounds. Checked **before** the expensive work, not after.

| Key | Default | Stops |
|---|---:|---|
| `max_file_size_bytes` | 268435456 (256 MB) | memory exhaustion |
| `max_compression_ratio` | 200 | zip bombs |
| `max_sheets` | 512 | sheet-count exhaustion |
| `max_rows_per_sheet` | 1048576 | declared-dimension bombs |
| `max_columns_per_sheet` | 16384 | declared-dimension bombs |
| `max_total_cells` | 20000000 | aggregate memory exhaustion |
| `max_formula_count` | 2000000 | formula-parsing cost |
| `max_untrusted_chars` | 4000 | a huge cell becoming a huge prompt |

## `[policy]`

Escalation thresholds. These are configurable.

| Key | Default | Escalates approval above |
|---|---:|---|
| `cell_change_approval_threshold` | 1000 | cells to change |
| `row_change_approval_threshold` | 500 | rows to change |
| `formula_removal_requires_approval` | `true` | any formula removed or overwritten |
| `structural_change_requires_approval` | `true` | any sheet added, removed, renamed, or reordered |
| `hidden_sheet_change_requires_approval` | `true` | any write to a hidden sheet |
| `restricted_data_requires_approval` | `true` | a workbook that looks sensitive |
| `ambiguous_task_requires_approval` | `true` | a plan carrying missing information |
| `max_operations_per_plan` | 50 | operations per plan |
| `deny_cells_affected_above` | 20000000 | **denies** rather than escalates |

### What configuration cannot do

Six rules are **hard denies** and no configuration file can disable them:

```
source_never_overwritten
output_within_workspace
cell_ceiling
operation_ceiling
vba_read_only
no_guessing
```

There is no `[policy]` key that turns them off, because a configuration file
cannot grant a permission the deny set forbids. `excelpilot policy` prints this
list alongside the effective configuration, and a `config_fingerprint` hash of
the effective settings is recorded in every run's audit trail — so a run's
behaviour can be tied to the exact rules that produced it.

## `[jev]`

| Key | Default | Meaning |
|---|---|---|
| `provider` | `"auto"` | `auto`, `typesafe`, `openrouter`, or `disabled` |
| `model` | `null` | `null` uses the endpoint's documented default |
| `timeout_seconds` | 30.0 | request timeout |
| `min_probability` | 0.8 | below this, a decision is `needs_review` (floor 0.5) |
| `min_margin` | 0.15 | below this, a decision is `needs_review` |
| `enabled` | `true` | master switch |
| `review_labels` | 9 labels | labels treated as "uncertain" |

`min_probability` and `min_margin` are described by upstream JEV as
*"uncalibrated starting points, not deployment recommendations"*, and are
reproduced as defaults here rather than presented as tuned values.

### Provider resolution

`auto` resolves from **credential presence only**:

```
TYPESAFE_API_KEY set   → typesafe
else OPENROUTER_API_KEY set → openrouter
else → disabled
```

It never probes an endpoint and never falls back after an error. Checking
presence is not authentication: a key can be set and still be invalid or out of
credit, and the code says so rather than implying otherwise.

TypeSafe is preferred under `auto` because the upstream CLI defaults to
OpenRouter and never falls back, so relying on the default would fail on a
machine holding only the TypeSafe key.

## `[model]`

The optional LLM planner. **Disabled by default** — the deterministic planner is
the default and needs no credential.

| Key | Default | Meaning |
|---|---|---|
| `provider` | `"openai_compatible"` | any OpenAI-compatible chat-completions endpoint |
| `base_url` | `null` | endpoint URL |
| `model` | `null` | model name |
| `api_key_env` | `"OPENAI_API_KEY"` | **name** of the variable holding the key |
| `timeout_seconds` | 60.0 | request timeout |
| `max_tokens` | 4000 | response cap |
| `temperature` | 0.0 | determinism where the provider allows it |
| `enabled` | `false` | master switch |

Note `api_key_env` holds a *variable name*, never a key. The value is read from
the environment at call time and is never written to a file, a log, or an error
message.

This path was **never run against a live provider** — see
[limitations.md](limitations.md).

## `[output]`

| Key | Default | Meaning |
|---|---|---|
| `versioned_outputs` | `true` | write to `<stem>__<run-id>.<ext>` |
| `never_overwrite_source` | `true` | the source is never opened for writing |
| `atomic_write` | `true` | write to a temp file and rename |
| `keep_snapshot` | `true` | keep a byte copy of the source |
| `neutralize_formula_injection` | `true` | neutralise strings starting `=`, `+`, `-`, `@` |

Setting `never_overwrite_source` to `false` does not enable overwriting: the
`source_never_overwritten` hard-deny rule is not configurable. These keys
document the behaviour; they do not relax it.

`neutralize_formula_injection` is the one safety default worth understanding
before turning off — it is what stops a string that looks like a formula from
executing when the output is opened in Excel.

## `[reconciliation]`

| Key | Default | Meaning |
|---|---:|---|
| `default_tolerance` | 0.0 | absolute tolerance for a reconciliation check |
| `default_relative_tolerance` | 0.0 | relative tolerance |
| `fail_run_on_variance` | `true` | a variance fails the run rather than warning |

With zero tolerance, a reconciliation that does not match exactly fails. That is
deliberate: totals are recomputed in Python from the workbook's own cells, so an
exact match is the only thing that should pass.

## `[anomaly]`

Thresholds for anomaly detection. These produce **warnings and reported
anomalies**, not policy decisions.

| Key | Default | Flags |
|---|---:|---|
| `row_count_change_ratio` | 0.1 | a large change in row count |
| `null_rate_increase` | 0.05 | a large increase in nulls |
| `duplicate_rate_increase` | 0.05 | a large increase in duplicates |
| `large_value_change_ratio` | 0.5 | a large proportion of values changed |
| `outlier_z_score` | 3.5 | a value beyond this many standard deviations |
| `planned_vs_actual_divergence` | 0.25 | the change diverged from its preview |

`planned_vs_actual_divergence` is the interesting one: it compares what the
preview said would happen with what actually happened, so a plan that behaves
differently from its dry run is caught rather than assumed benign.

## `[verification]`

| Key | Default | Meaning |
|---|---:|---|
| `enable_recalculation` | `true` | evaluate formulas when the library is available |
| `max_recalculation_cells` | 500000 | refuse to evaluate beyond this |
| `recalculation_timeout_seconds` | 120.0 | give up on evaluation after this |

When `enable_recalculation` is `false`, or the optional `recalc` extra is not
installed, or evaluation fails, the result reports `recalculated: false` and
`static_formula_checks: true`. **It never claims an evaluation it did not
perform.**

```bash
uv sync --extra recalc      # install `formulas`, enabling evaluation
```

## Optional extras

| Extra | Adds | Enables |
|---|---|---|
| `dev` | test and lint tooling | development |
| `recalc` | `formulas` | real formula evaluation in verification |

## Credentials

```bash
export TYPESAFE_API_KEY=...      # JEV via TypeSafe
export OPENROUTER_API_KEY=...   # JEV via OpenRouter
export OPENAI_API_KEY=...       # LLM planner, if enabled
```

None are required for inspection, planning, execution, or verification. They are
needed only for a live JEV call, which additionally needs `--allow-paid-calls`.

Values are never printed, logged, written to an audit record, or included in an
error message. Only their **presence** is checked.
