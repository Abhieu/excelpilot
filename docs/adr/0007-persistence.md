# ADR 0007 — Persistence: append-only JSONL audit, filesystem run store

- **Status:** Accepted
- **Date:** 2026-09-25

## Context

ExcelPilot must let an engineer reconstruct exactly what happened in a run: the
request, the plan, every decision, every policy evaluation, every operation, the
approval, verification results, and the output artifact with hashes.

The specification requires replay, secret redaction, and an audit trail that survives
the process. It does not require concurrent multi-user operation.

## Decision

### Layout

```
.excelpilot/
  runs/
    <run_id>/
      run.json            # RunRecord: request, config fingerprint, outcome, timings
      audit.jsonl         # append-only, one AuditEvent per line
      manifest.json       # ChangeManifest (machine-readable)
      report.txt          # human-readable change report
      source.snapshot.xlsx  # byte copy of the input, taken before any work
      output/<stem>__run-<run_id>.xlsx
      verification.json
```

### Audit log

Append-only JSONL. One `AuditEvent` per line, each with `seq` (monotonic),
`timestamp`, `run_id`, `actor` (`user` | `ai` | `jev` | `policy` | `system`),
`event_type`, and a redacted payload.

**Why JSONL rather than SQLite:**

- An engineer can `cat`/`grep`/`jq` it. For a tool whose selling point is
  inspectability, "open it in a text editor" is a real feature.
- Append-only writes are crash-safe: a partial write costs at most the last line,
  and `seq` gaps are detectable.
- No schema migration story for a v1 whose event types are still evolving.
- Zero dependencies (ADR-0002).

**Known limitation, accepted:** not suitable for concurrent writers to one run. ExcelPilot
executes one run at a time per workspace directory, enforced by a lock file. Multi-user
is deferred (ADR-0012 territory).

### Redaction is applied at write time, not read time

`audit.emit()` passes every payload through `redact()` before serialisation. Redacting
on read would mean secrets were already written to disk. The redactor replaces
anything matching registered secret values and secret-shaped patterns with
`[REDACTED:<kind>]`, and never raises — a redactor that can crash a run is worse than
one that over-masks.

### Replay is read-only by default

`excelpilot replay RUN_ID` reconstructs the run narrative and **re-executes nothing**.
`--replay-execute` re-runs the plan, but only in dry-run unless the run is explicitly
authorised, and a replayed run gets a **new** `run_id` and a link back to the original.
Replay never overwrites the original output.

### Storage is behind a `RunStore` protocol

`FileRunStore` is the only implementation, but audit and the dashboard depend on the
protocol, not the directory layout. A future backend does not change callers.

## Alternatives considered

| Option | Why rejected |
|---|---|
| SQLite | Better concurrency and querying; worse inspectability; adds a dependency and a migration story for a single-writer tool |
| MongoDB / Postgres | Grossly disproportionate; operational burden dwarfs the problem |
| Single JSON blob per run | Must be rewritten wholesale on every event; a crash mid-write loses the audit trail — unacceptable for the component whose purpose is crash resilience |
| In-memory only | Audit must survive the process |
| Append-only log, no run directory | Loses the atomic source snapshot and the manifest/output grouping |

## Consequences

**Positive**
- Zero dependencies; trivially inspectable and diffable.
- Crash-safe: a truncated final line is detectable, prior events are intact.
- Secrets never reach disk.
- The whole run directory can be archived or attached to a bug report.

**Negative**
- No indexed queries. `grep` is the query interface at this scale.
- Single-writer per run, enforced by a lock file rather than by the storage layer.
- Run directories accumulate; `--gc` prunes them by age with an audit event.
