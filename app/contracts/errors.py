"""Exception hierarchy.

Every error ExcelPilot raises deliberately derives from :class:`ExcelPilotError`,
so a caller can distinguish "ExcelPilot said no" from "something unexpected
happened". The CLI maps these to distinct exit codes (ADR-0008).

Messages must never contain secrets, API keys, or raw credential material. The
audit redactor is a second line of defence, not a licence to be careless here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.contracts.enums import PolicyOutcome


class ExcelPilotError(Exception):
    """Base class for every deliberate ExcelPilot error."""

    #: Short machine-readable code, recorded in audit events.
    code: str = "excelpilot_error"

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details: dict[str, Any] = details or {}

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": self.details}


class WorkbookSecurityError(ExcelPilotError):
    """A workbook violated a safety limit or structural expectation.

    Raised for zip bombs, oversized archives, excessive sheet/cell counts, and
    malformed packages. Never used for ordinary "file not found".
    """

    code = "workbook_security"


class LimitExceeded(WorkbookSecurityError):
    """A configured processing limit was exceeded."""

    code = "limit_exceeded"


class TargetResolutionError(ExcelPilotError):
    """A named sheet, range, or table could not be resolved in the workbook.

    Carries what *was* found, because "sheet 'Sale' not found; available:
    ['Sales', 'Summary']" is far more actionable than a generic failure.
    """

    code = "target_resolution"

    def __init__(
        self,
        message: str,
        *,
        requested: str,
        available: list[str] | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message, details=details)
        self.requested = requested
        self.available = available or []


class UnsupportedOperationError(ExcelPilotError):
    """A requested capability is not implemented.

    Raised instead of attempting a partial or approximate implementation. A clean
    "not supported" is strictly better than a half-working one (spec section 1).
    """

    code = "unsupported_operation"

    def __init__(
        self, message: str, *, capability: str, details: dict[str, Any] | None = None
    ) -> None:
        super().__init__(message, details=details)
        self.capability = capability


class InvalidPlanError(ExcelPilotError):
    """A proposed plan failed validation.

    Raised when model or planner output cannot be validated into an
    ``ExecutionPlan``. The plan is rejected whole — never partially repaired.
    """

    code = "invalid_plan"


class InvalidDecisionError(ExcelPilotError):
    """A JEV (or other decisioning) response was malformed or inconsistent."""

    code = "invalid_decision"


class PolicyDenied(ExcelPilotError):
    """Deterministic policy refused the run.

    This is the only authority on permission. JEV and the model cannot override it
    (ADR-0005).
    """

    code = "policy_denied"

    def __init__(
        self,
        message: str,
        *,
        outcome: PolicyOutcome,
        rule_ids: list[str] | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message, details=details)
        self.outcome = outcome
        self.rule_ids = rule_ids or []


class VerificationFailed(ExcelPilotError):
    """Verification did not pass.

    The run fails even though a file may have been written. This is the mechanism
    that keeps "saved" distinct from "verified" (ADR-0011).
    """

    code = "verification_failed"


class InjectionDetected(ExcelPilotError):
    """Untrusted content matched an injection heuristic.

    Raised only where continuing would be unsafe. Most findings are advisory and
    reported through the audit trail rather than raised (ADR-0012).
    """

    code = "injection_detected"


class PaidCallBlocked(ExcelPilotError):
    """A live paid external call was attempted without explicit authorisation.

    The control is in the code path, not a warning: without
    ``--allow-paid-calls`` the call cannot be made.
    """

    code = "paid_call_blocked"

    def __init__(
        self, message: str, *, service: str, details: dict[str, Any] | None = None
    ) -> None:
        super().__init__(message, details=details)
        self.service = service


class ApprovalRequired(ExcelPilotError):
    """Policy requires human approval and none was granted."""

    code = "approval_required"


class RunStateError(ExcelPilotError):
    """An illegal run state transition was attempted."""

    code = "invalid_state_transition"


class StorageError(ExcelPilotError):
    """The run store could not satisfy a request."""

    code = "storage_error"


class ConfigError(ExcelPilotError):
    """Configuration was missing, malformed, or internally inconsistent."""

    code = "config_error"


__all__ = [
    "ApprovalRequired",
    "ConfigError",
    "ExcelPilotError",
    "InjectionDetected",
    "InvalidDecisionError",
    "InvalidPlanError",
    "LimitExceeded",
    "PaidCallBlocked",
    "PolicyDenied",
    "RunStateError",
    "StorageError",
    "TargetResolutionError",
    "UnsupportedOperationError",
    "VerificationFailed",
    "WorkbookSecurityError",
]
