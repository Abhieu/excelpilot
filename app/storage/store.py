"""Run store: one directory per run.

```
.excelpilot/runs/<run_id>/
  run.json                 # RunRecord: the run's identity and outcome
  audit.jsonl              # append-only, one AuditEvent per line
  manifest.json            # ChangeManifest (machine-readable)
  report.txt               # human-readable change report
  source.snapshot.xlsx     # byte copy of the input, taken before any work
  output/<stem>__run-<id>.xlsx
  verification.json
```

The layout exists so an engineer can reconstruct a run with ``cat`` and ``jq``,
and so a whole run can be archived or attached to a bug report (ADR-0007).

Single-writer per run, enforced by a lock file rather than by the storage layer —
concurrent runs are explicitly out of scope.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.contracts.base import utc_now
from app.contracts.config import ExcelPilotConfig
from app.contracts.errors import StorageError
from app.contracts.pipeline import RunRecord
from app.contracts.verification import ChangeManifest, VerificationResult
from app.workbook.hashing import file_sha256

#: Filenames within a run directory.
RUN_FILE = "run.json"
AUDIT_FILE = "audit.jsonl"
MANIFEST_FILE = "manifest.json"
REPORT_FILE = "report.txt"
#: Snapshot stem. The **suffix is the source's own**, not a fixed one — see
#: :func:`snapshot_file_name`.
SNAPSHOT_STEM = "source.snapshot"
VERIFICATION_FILE = "verification.json"
OUTPUT_DIR = "output"
LOCK_FILE = ".lock"


def snapshot_file_name(source: str | Path) -> str:
    """Name the source snapshot after the source's real extension.

    This used to be the constant ``"source.snapshot.xlsx"``, which silently
    destroyed macro projects.

    The executor does not open the user's file; it opens this snapshot. The
    reader decides ``keep_vba`` from the *extension it sees*, so a ``.xlsm``
    snapshotted as ``.xlsx`` is loaded with ``keep_vba=False`` and openpyxl then
    drops ``xl/vbaProject.bin`` on save. The run's output was therefore an
    ``.xlsm`` with no macros in it, while the source stayed untouched and every
    other check passed.

    Preserving the suffix keeps the reader's own rule honest: a macro-enabled
    source is snapshotted as macro-enabled.
    """
    return f"{SNAPSHOT_STEM}{Path(source).suffix.lower()}"


class RunLocked(StorageError):
    """Another process holds this run's lock."""

    code = "run_locked"


@dataclass(frozen=True, slots=True)
class RunPaths:
    """Every path in a run directory, derived from its id.

    ``snapshot_suffix`` defaults to ``.xlsx`` so an existing run directory still
    resolves; callers that know the source should pass its real suffix, because
    the extension is what the reader uses to decide ``keep_vba``.
    """

    root: Path
    snapshot_suffix: str = ".xlsx"

    @property
    def run_file(self) -> Path:
        return self.root / RUN_FILE

    @property
    def audit_file(self) -> Path:
        return self.root / AUDIT_FILE

    @property
    def manifest_file(self) -> Path:
        return self.root / MANIFEST_FILE

    @property
    def report_file(self) -> Path:
        return self.root / REPORT_FILE

    @property
    def snapshot_file(self) -> Path:
        return self.root / f"{SNAPSHOT_STEM}{self.snapshot_suffix}"

    @property
    def verification_file(self) -> Path:
        return self.root / VERIFICATION_FILE

    @property
    def output_dir(self) -> Path:
        return self.root / OUTPUT_DIR

    @property
    def lock_file(self) -> Path:
        return self.root / LOCK_FILE

    def ensure(self) -> None:
        """Create the run directory structure."""
        self.root.mkdir(parents=True, exist_ok=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)


class FileRunStore:
    """Filesystem implementation of the run store."""

    def __init__(self, config: ExcelPilotConfig | None = None) -> None:
        self.config = config or ExcelPilotConfig()
        self.runs_root = config.runs_path() if config else self.config.runs_path()

    # -- lifecycle ---------------------------------------------------------

    def paths(self, run_id: str, *, source: str | Path | None = None) -> RunPaths:
        """Paths for a run id. Does not create anything.

        Pass ``source`` so the snapshot keeps the source's real extension; the
        reader derives ``keep_vba`` from it. Omitting it falls back to ``.xlsx``,
        which is correct only for a non-macro source.
        """
        if not run_id or "/" in run_id or "\\" in run_id or run_id.startswith("."):
            # A run id becomes a directory name, so it must not be able to escape.
            raise StorageError(f"invalid run id: {run_id!r}")
        suffix = Path(source).suffix.lower() if source else ".xlsx"
        return RunPaths(root=self.runs_root / run_id, snapshot_suffix=suffix)

    def create(self, run_id: str) -> RunPaths:
        """Create a run directory, failing if one already exists."""
        paths = self.paths(run_id)
        if paths.root.exists():
            raise StorageError(f"run {run_id} already exists at {paths.root}")
        paths.ensure()
        return paths

    def ensure(self, run_id: str, *, source: str | Path | None = None) -> RunPaths:
        """Get (creating if needed) the directory for a run id.

        Pass ``source`` so the snapshot path keeps the source's real extension.
        """
        paths = self.paths(run_id, source=source)
        paths.ensure()
        return paths

    def find_snapshot(self, run_id: str) -> Path | None:
        """Locate a run's source snapshot regardless of the suffix it was saved with.

        The snapshot keeps the source's extension, which is not known from the
        run id alone. The run directory is authoritative, so glob for the stem
        rather than assuming ``.xlsx``.

        Returns ``None`` when the run has no snapshot, so callers can report that
        rather than silently verifying against nothing.
        """
        paths = self.paths(run_id)
        preferred = paths.snapshot_file
        if preferred.exists():
            return preferred
        matches = sorted(paths.root.glob(f"{SNAPSHOT_STEM}.*"))
        return matches[0] if matches else None

    def exists(self, run_id: str) -> bool:
        try:
            return self.paths(run_id).run_file.exists()
        except StorageError:
            return False

    @contextlib.contextmanager
    def lock(self, run_id: str) -> Iterator[RunPaths]:
        """Hold an exclusive lock on a run directory.

        ExcelPilot executes one run at a time per workspace. The lock is
        advisory and best-effort: it prevents accidental concurrent clobbering
        and says so on stderr, rather than pretending to be a transaction.
        """
        paths = self.ensure(run_id)
        try:
            descriptor = os.open(paths.lock_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            raise RunLocked(
                f"run {run_id} is locked by another process; "
                f"if that is wrong, remove {paths.lock_file}"
            ) from None
        try:
            os.write(descriptor, str(os.getpid()).encode())
            os.close(descriptor)
            yield paths
        finally:
            with contextlib.suppress(OSError):
                paths.lock_file.unlink()

    # -- source preservation ----------------------------------------------

    def snapshot(self, run_id: str, source: Path) -> Path:
        """Take a byte-exact copy of the source before any work.

        A *copy*, not a re-save: re-serialising changes the bytes and drops the
        shared-string cache (ADR-0009), so a re-saved "snapshot" would not be the
        user's original (ADR-0010).
        """
        source = Path(source)
        destination = self.ensure(run_id, source=source).snapshot_file
        shutil.copy2(source, destination)
        return destination

    # -- run record --------------------------------------------------------

    def write_run(self, record: RunRecord) -> Path:
        """Persist the run record atomically."""
        paths = self.ensure(record.run_id)
        _write_json_atomic(paths.run_file, record.model_dump(mode="json"))
        return paths.run_file

    def read_run(self, run_id: str) -> RunRecord:
        """Load a run record, or raise a clear error."""
        paths = self.paths(run_id)
        if not paths.run_file.exists():
            raise StorageError(
                f"no run record for {run_id!r}; available runs: "
                f"{', '.join(self.list_runs()) or '(none)'}",
                details={"run_id": run_id},
            )
        try:
            return RunRecord.model_validate_json(paths.run_file.read_text(encoding="utf-8"))
        except (ValueError, json.JSONDecodeError) as error:
            raise StorageError(f"run record for {run_id!r} is corrupt: {error}") from error

    def update_run(self, record: RunRecord) -> RunRecord:
        """Write an updated record, stamping ``completed_at`` when it is terminal."""
        if record.completed_at is None and record.outcome is not None:
            record = record.model_copy(update={"completed_at": utc_now().isoformat()})
        self.write_run(record)
        return record

    # -- artefacts ---------------------------------------------------------

    def write_manifest(self, manifest: ChangeManifest) -> Path:
        paths = self.ensure(manifest.run_id)
        _write_json_atomic(paths.manifest_file, manifest.model_dump(mode="json"))
        paths.report_file.write_text(manifest.to_text() + "\n", encoding="utf-8")
        return paths.manifest_file

    def read_manifest(self, run_id: str) -> ChangeManifest | None:
        paths = self.paths(run_id)
        if not paths.manifest_file.exists():
            return None
        try:
            return ChangeManifest.model_validate_json(
                paths.manifest_file.read_text(encoding="utf-8")
            )
        except (ValueError, json.JSONDecodeError):
            return None

    def write_verification(self, result: VerificationResult) -> Path:
        paths = self.ensure(result.run_id)
        _write_json_atomic(paths.verification_file, result.model_dump(mode="json"))
        return paths.verification_file

    def read_verification(self, run_id: str) -> VerificationResult | None:
        paths = self.paths(run_id)
        if not paths.verification_file.exists():
            return None
        try:
            return VerificationResult.model_validate_json(
                paths.verification_file.read_text(encoding="utf-8")
            )
        except (ValueError, json.JSONDecodeError):
            return None

    def output_path(self, run_id: str, name: str) -> Path:
        """Path for a run's output artefact, inside the run directory."""
        paths = self.ensure(run_id)
        return paths.output_dir / name

    def record_output(self, path: Path) -> dict[str, Any]:
        """Hash a produced artefact so it can be verified later."""
        return {
            "path": str(path),
            "hash": file_sha256(path),
            "size_bytes": path.stat().st_size,
            "recorded_at": utc_now().isoformat(),
        }

    # -- discovery ---------------------------------------------------------

    def list_runs(self, *, limit: int | None = None) -> list[str]:
        """Run ids, most recently modified first."""
        if not self.runs_root.exists():
            return []
        records: list[tuple[float, str]] = []
        for child in self.runs_root.iterdir():
            if not child.is_dir():
                continue
            run_file = child / RUN_FILE
            if not run_file.exists():
                continue
            try:
                records.append((run_file.stat().st_mtime, child.name))
            except OSError:
                continue
        records.sort(reverse=True)
        ids = [run_id for _, run_id in records]
        return ids[:limit] if limit else ids

    def recent(self, *, limit: int = 20) -> list[RunRecord]:
        """Load the most recent run records, skipping any that fail to parse."""
        results: list[RunRecord] = []
        for run_id in self.list_runs(limit=limit * 2):
            try:
                results.append(self.read_run(run_id))
            except StorageError:
                continue
            if len(results) >= limit:
                break
        return results

    def prune(self, *, keep: int = 50) -> list[str]:
        """Delete the oldest run directories, keeping the newest ``keep``.

        Returns the ids removed. Run directories accumulate by design (they are
        the audit record), so an explicit prune is provided rather than automatic
        deletion, which would be a surprising thing to do to an audit trail.
        """
        ids = self.list_runs()
        if len(ids) <= keep:
            return []
        removed: list[str] = []
        for run_id in ids[keep:]:
            try:
                shutil.rmtree(self.runs_root / run_id)
                removed.append(run_id)
            except OSError:
                continue
        return removed


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    """Write JSON atomically, so a reader never sees a partial document."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        temporary.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8"
        )
        temporary.replace(path)
    except Exception:
        with contextlib.suppress(OSError):
            temporary.unlink()
        raise


__all__ = ["FileRunStore", "RunLocked", "RunPaths"]
