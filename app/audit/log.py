"""Append-only audit log.

One JSON object per line in ``audit.jsonl``. Chosen over SQLite or a single JSON
blob because, for an operations tool whose selling point is inspectability,
*being able to read it with ``cat`` and ``jq``* is a real feature. Append-only
writes are also crash-safe: a truncated final line costs at most that line, and
``seq`` gaps are detectable (ADR-0007).

Every payload passes through :class:`~app.audit.redaction.Redactor` **before**
serialisation, so a secret never reaches disk.
"""

from __future__ import annotations

import contextlib
import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from app.audit.redaction import Redactor
from app.contracts.base import utc_now
from app.contracts.enums import Actor
from app.contracts.verification import AuditEvent


class AuditLog:
    """Append-only JSONL audit log for one run."""

    __slots__ = ("_path", "_redactor", "_seq", "_fh")

    def __init__(self, path: Path, *, redactor: Redactor | None = None) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._redactor = redactor or Redactor()
        # Continue an existing log rather than overwriting it: a re-run against
        # the same run id must not erase the history of the first attempt.
        self._seq = self._last_seq()
        self._fh = self._path.open("a", encoding="utf-8")

    @property
    def path(self) -> Path:
        return self._path

    @property
    def seq(self) -> int:
        return self._seq

    def _last_seq(self) -> int:
        """Highest sequence number already present, so numbering stays monotonic."""
        if not self._path.exists():
            return 0
        highest = 0
        for event in self.read():
            highest = max(highest, event.seq)
        return highest

    def emit(
        self,
        actor: Actor,
        event_type: str,
        payload: dict[str, Any] | None = None,
    ) -> AuditEvent:
        """Append one event, redacting the payload first.

        ``fsync`` after each write. That is slower than buffering, and it is the
        right trade for an audit trail: if the process dies, the record of what it
        did must already be on disk.
        """
        self._seq += 1
        event = AuditEvent(
            seq=self._seq,
            run_id=str((payload or {}).get("run_id", "")) or "unknown",
            timestamp=utc_now().isoformat(),
            actor=actor,
            event_type=event_type,
            payload=self._redactor.redact(payload or {}),
        )
        self._fh.write(event.model_dump_json() + "\n")
        self._fh.flush()
        # fsync is unavailable on some filesystems; the flush above still
        # guarantees the data reached the OS.
        with contextlib.suppress(OSError):
            os.fsync(self._fh.fileno())
        return event

    def read(self) -> Iterator[AuditEvent]:
        """Yield every readable event, skipping any corrupt trailing line.

        A partially-written last line is expected after a crash. Skipping it is
        correct: the alternative is refusing to read the audit trail at all,
        which would be far worse.
        """
        if not self._path.exists():
            return
        with self._path.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield AuditEvent.model_validate_json(line)
                except (ValueError, json.JSONDecodeError):
                    continue

    def close(self) -> None:
        """Flush and close. Idempotent."""
        if not self._fh.closed:
            self._fh.flush()
            self._fh.close()

    def __enter__(self) -> AuditLog:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def to_json(self) -> list[dict[str, Any]]:
        """The whole log as a list of dicts, for ``--json`` output."""
        return [event.model_dump(mode="json") for event in self.read()]

    def summary(self) -> dict[str, Any]:
        """Counts by event type and actor, for a quick audit overview."""
        by_type: dict[str, int] = {}
        by_actor: dict[str, int] = {}
        for event in self.read():
            by_type[event.event_type] = by_type.get(event.event_type, 0) + 1
            by_actor[str(event.actor)] = by_actor.get(str(event.actor), 0) + 1
        return {
            "events": sum(by_type.values()),
            "by_type": dict(sorted(by_type.items())),
            "by_actor": dict(sorted(by_actor.items())),
        }


#: Event type constants. Centralised so a typo is a NameError at import time
#: rather than a silently missing audit record.
class EventType:
    """Canonical audit event types."""

    RUN_CREATED = "run.created"
    RUN_STATE_CHANGED = "run.state_changed"
    RUN_COMPLETED = "run.completed"
    RUN_FAILED = "run.failed"
    WORKBOOK_INSPECTED = "workbook.inspected"
    SOURCE_SNAPSHOT = "source.snapshot"
    OUTPUT_WRITTEN = "output.written"
    TASK_PARSED = "task.parsed"
    PLAN_BUILT = "plan.built"
    PLAN_REJECTED = "plan.rejected"
    JEV_REQUESTED = "jev.requested"
    JEV_DECIDED = "jev.decided"
    JEV_UNAVAILABLE = "jev.unavailable"
    POLICY_EVALUATED = "policy.evaluated"
    APPROVAL_REQUESTED = "approval.requested"
    APPROVAL_GRANTED = "approval.granted"
    APPROVAL_REJECTED = "approval.rejected"
    DRY_RUN_COMPLETED = "dry_run.completed"
    EXECUTION_STARTED = "execution.started"
    EXECUTION_OPERATION = "execution.operation"
    EXECUTION_COMPLETED = "execution.completed"
    EXECUTION_FAILED = "execution.failed"
    VERIFICATION_COMPLETED = "verification.completed"
    RECONCILIATION_COMPLETED = "reconciliation.completed"
    ANOMALY_DETECTED = "anomaly.detected"
    INJECTION_DETECTED = "injection.detected"
    DIFF_COMPUTED = "diff.computed"
    FORMULA_INJECTION_NEUTRALISED = "safety.formula_injection_neutralised"
    ROLLBACK_INITIATED = "rollback.initiated"
    ROLLBACK_COMPLETED = "rollback.completed"
    ROLLBACK_REFUSED = "rollback.refused"
    REPLAY = "replay.executed"
    PAID_CALL_BLOCKED = "safety.paid_call_blocked"


__all__ = ["AuditLog", "EventType"]
