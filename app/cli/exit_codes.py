"""Exit codes.

Distinct codes are the point. A script must be able to tell "policy said no" from
"the output was written but verification failed" from "a paid call was blocked",
without parsing text (ADR-0008).
"""

from __future__ import annotations

from enum import IntEnum
from typing import Any

from app.contracts.enums import ApprovalStatus, RunOutcome, VerificationStatus


class Exit(IntEnum):
    """Process exit codes."""

    SUCCESS = 0
    INTERNAL_ERROR = 1
    USAGE = 2
    POLICY_DENIED = 3
    APPROVAL_REQUIRED = 4
    VERIFICATION_FAILED = 5
    PAID_CALL_BLOCKED = 6
    NOT_FOUND = 7


def for_run(outcome: RunOutcome, *, verification_status: str | None = None) -> Exit:
    """Map a run outcome to an exit code.

    ``VERIFICATION_FAILED`` is the load-bearing one: a file was written, but the
    outcome was not established. Returning ``SUCCESS`` there would make "saved"
    indistinguishable from "verified", which is the distinction the whole
    verification layer exists to preserve.
    """
    if outcome in {RunOutcome.SUCCEEDED, RunOutcome.DRY_RUN, RunOutcome.ROLLED_BACK}:
        return Exit.SUCCESS
    if outcome is RunOutcome.REJECTED_BY_POLICY:
        return Exit.POLICY_DENIED
    if outcome is RunOutcome.REJECTED_BY_APPROVAL:
        return Exit.APPROVAL_REQUIRED
    # FAILED. Distinguish "written but not verified" from a plain failure, so a
    # script can tell the two apart without parsing text. The trailing else is a
    # safety net: a newly added outcome should never be reported as success.
    if outcome is RunOutcome.FAILED and verification_status == VerificationStatus.FAILED.value:
        return Exit.VERIFICATION_FAILED
    return Exit.INTERNAL_ERROR


def for_approval(status: ApprovalStatus) -> Exit:
    """Exit code for a standalone approval decision.

    Anything other than an explicit approval is reported as
    ``APPROVAL_REQUIRED``: a pending decision and a rejection both mean "this did
    not proceed, and the operator has not authorised it".
    """
    return Exit.SUCCESS if status is ApprovalStatus.APPROVED else Exit.APPROVAL_REQUIRED


def for_result(result: Any) -> Exit:
    """Exit code for a completed run, considering where it stopped.

    A run that needed approval and did not get it has ``outcome == FAILED``,
    because nothing happened — but the *reason* is the approval gate, not an
    error. Reporting it as ``INTERNAL_ERROR`` would tell an operator (or a script)
    that something broke, when the correct action is simply to review and approve.
    """
    approval = getattr(result, "approval", None)
    outcome = getattr(result, "outcome", None)
    record = getattr(result, "record", None)
    verification_status = getattr(record, "verification_status", None)

    if outcome is None:
        return Exit.INTERNAL_ERROR

    # A dry run that stops at the approval gate is the expected, useful outcome:
    # it told the operator exactly what needs approving. Only a *real* run that
    # lacked approval is reported as a failure.
    if getattr(result, "approval_request", None) is not None and outcome is not RunOutcome.DRY_RUN:
        status = getattr(approval, "status", None)
        if status is not ApprovalStatus.APPROVED:
            return Exit.APPROVAL_REQUIRED
    return for_run(outcome, verification_status=verification_status)


__all__ = ["Exit", "for_approval", "for_result", "for_run"]
