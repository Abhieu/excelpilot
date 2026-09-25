# ADR 0010 — Safe output and rollback

- **Status:** Accepted
- **Date:** 2026-09-25

## Context

Workbook automation can destroy a user's data irrecoverably. The specification
requires: never overwrite the original, versioned output, preserved original, rollback
where feasible, and audited rollback that never silently replaces files.

The complication is that ExcelPilot's writer is **not byte-faithful** (ADR-0009):
saving an unchanged workbook through openpyxl changes the file's bytes, drops the
shared-string cache, and relocates comment parts. So "undo" cannot mean "re-serialise
the original".

## Decision

### 1. The source is never opened for writing

Not "opened read-only" — never opened for writing at all. The pipeline copies the
source to `source.snapshot.xlsx` inside the run directory before doing anything, and
all work happens on that copy. The user's file on disk is not a write target under any
code path.

### 2. Output is versioned and atomic

```
<stem>__run-<run_id>.xlsx
```

Written to a temp file in the destination directory, `fsync`'d, then `os.replace`'d
into position. `os.replace` is atomic within a filesystem, so a reader never observes
a partial workbook, and a crash mid-write cannot corrupt an existing file.

### 3. Rollback means "discard the output", not "rewrite the source"

Because the source was never modified, **true rollback is trivially safe and requires
no restoration logic**: the original is still exactly where the user left it. Rollback
is implemented as an auditable, explicit action:

```
excelpilot rollback RUN_ID [--delete-output]
```

- Verifies the output file's hash still matches what the run recorded. If it does not,
  the output has been modified since the run and is **not** deleted — the mismatch is
  reported and the operator decides.
- Emits `rollback.initiated` and `rollback.completed` audit events with before/after
  hashes.
- Never writes to the source path. There is no code path anywhere in ExcelPilot that
  opens the source for writing.

This is stronger than a restore-based rollback, because restore depends on a snapshot
being complete and correct. Here there is nothing to restore.

### 4. Hashes anchor everything

`source_sha256` (the user's original) and `output_sha256` are recorded in the run
record, the manifest, and the audit trail. Rollback and `--verify` both check them.

## What this guarantees, and what it does not

Stated precisely, because the specification asks for the guarantee to be documented
exactly:

**Guaranteed**
- The source file is byte-identical before and after any run. This is not a
  best-effort behaviour; there is no write path to the source.
- A run either produces a complete output file or no output file.
- A failed verification leaves the output in place but the run marked failed, with
  exit code 5 — the operator is never told a bad workbook is good.
- Rollback is itself audited and hash-checked.

**Not guaranteed**
- That the output preserves *every* OOXML part. openpyxl drops the shared-string
  cache and relocates comment parts (ADR-0001). Cell values, formulas, styles, tables,
  defined names, and conditional formatting are preserved — verified on the real
  fixture set.
- That ExcelPilot can reconstruct the original from the output. It does not attempt to;
  the original is simply still there.

## Alternatives considered

| Option | Why rejected |
|---|---|
| Edit the source workbook in place | Directly violates the safety requirement; one bug destroys user data |
| Snapshot/restore "rollback" | Depends on a correct snapshot and a correct restore path. Strictly weaker than never having written to the source |
| Timestamped backups of the source before in-place edit | Still writes to the user's file; a crash between backup and write can leave it corrupt |
| Git-based versioning of workbooks | Diff noise from re-serialisation makes this useless as a change record (ADR-0009), and binary storage in git is poor for large workbooks |
| `--force` to allow in-place writes | An in-place write flag on a tool whose product principle is "humans retain control" is a contradiction. The correct answer to "I want to overwrite" is "use the output file" |

## Consequences

**Positive**
- The strongest safety property available: the original cannot be lost.
- Rollback is simple and auditable, with no restore logic to get wrong.
- Atomic output means no partially-written workbook is ever observable.

**Negative**
- Disk cost: a full snapshot plus output per run. Mitigated by `--gc` pruning and by
  the fact that runs are directory-scoped.
- A user who wants the cleaned workbook *in place* must copy it themselves. This is
  the intended trade.
