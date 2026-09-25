"""Enumerations shared across every layer.

String-valued enums (``class X(str, Enum)``) so that JSON serialisation is
readable and stable: an audit record shows ``"risk": "high"``, not
``"risk": 2"``. The values are part of the persisted contract and must not be
renamed without bumping ``CONTRACT_VERSION``.
"""

from __future__ import annotations

from enum import Enum


class _StrEnum(str, Enum):
    """String enum with a readable ``str()`` for logs and error messages."""

    def __str__(self) -> str:
        return str(self.value)


class Actor(_StrEnum):
    """Who or what produced an audit event."""

    USER = "user"
    AI = "ai"
    JEV = "jev"
    POLICY = "policy"
    SYSTEM = "system"


class RunState(_StrEnum):
    """Lifecycle of a run. Transitions are validated by the state machine."""

    CREATED = "created"
    INSPECTING = "inspecting"
    UNDERSTANDING = "understanding"
    PLANNING = "planning"
    DECIDING = "deciding"
    POLICY_CHECK = "policy_check"
    AWAITING_APPROVAL = "awaiting_approval"
    EXECUTING = "executing"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    FAILED = "failed"
    REJECTED = "rejected"
    ROLLED_BACK = "rolled_back"


class RunOutcome(_StrEnum):
    """Terminal outcome of a run."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REJECTED_BY_POLICY = "rejected_by_policy"
    REJECTED_BY_APPROVAL = "rejected_by_approval"
    DRY_RUN = "dry_run"
    ROLLED_BACK = "rolled_back"


class OperationKind(_StrEnum):
    """The closed set of workbook operations ExcelPilot can perform.

    This is deliberately small. Each member needs a typed contract, a policy rule,
    a dry-run preview and tests before it ships. See ADR-0006.
    """

    READ_RANGE = "read_range"
    WRITE_RANGE = "write_range"
    SET_FORMULA = "set_formula"
    CREATE_WORKSHEET = "create_worksheet"
    RENAME_WORKSHEET = "rename_worksheet"
    SORT_RANGE = "sort_range"
    FILTER_ROWS = "filter_rows"
    REMOVE_DUPLICATES = "remove_duplicates"
    NORMALIZE_VALUES = "normalize_values"
    APPLY_VALIDATION = "apply_validation"
    CREATE_SUMMARY = "create_summary"
    COMPARE_WORKBOOKS = "compare_workbooks"
    RECONCILE = "reconcile"

    @property
    def is_mutating(self) -> bool:
        """Whether this operation can change a workbook."""
        return self not in _READ_ONLY_OPERATIONS

    @property
    def is_destructive(self) -> bool:
        """Whether this operation removes or overwrites existing content."""
        return self in _DESTRUCTIVE_OPERATIONS

    @property
    def is_structural(self) -> bool:
        """Whether this operation changes workbook structure."""
        return self in _STRUCTURAL_OPERATIONS


_READ_ONLY_OPERATIONS = frozenset(
    {
        OperationKind.READ_RANGE,
        OperationKind.COMPARE_WORKBOOKS,
        OperationKind.RECONCILE,
    }
)

_DESTRUCTIVE_OPERATIONS = frozenset(
    {
        OperationKind.REMOVE_DUPLICATES,
        OperationKind.SET_FORMULA,
        OperationKind.WRITE_RANGE,
    }
)

_STRUCTURAL_OPERATIONS = frozenset(
    {
        OperationKind.CREATE_WORKSHEET,
        OperationKind.RENAME_WORKSHEET,
        OperationKind.REMOVE_DUPLICATES,
        OperationKind.CREATE_SUMMARY,
    }
)


class RiskLevel(_StrEnum):
    """Risk classification. Ordered low -> high by declaration order."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"

    @property
    def rank(self) -> int:
        return _RISK_RANK[self]


_RISK_RANK = {RiskLevel.LOW: 0, RiskLevel.MEDIUM: 1, RiskLevel.HIGH: 2}


class AutomationVerdict(_StrEnum):
    """JEV's view on whether a run may proceed unattended.

    Advisory only. Policy decides (ADR-0005).
    """

    YES = "yes"
    APPROVAL_REQUIRED = "approval_required"
    NO = "no"


class InterpretationVerdict(_StrEnum):
    """How clear the user's request is."""

    SUFFICIENTLY_CLEAR = "sufficiently_clear"
    AMBIGUOUS = "ambiguous"
    REQUIRES_USER_INPUT = "requires_user_input"


class PolicyOutcome(_StrEnum):
    """Deterministic policy verdict. The sole authority on permission."""

    ALLOW = "allow"
    REQUIRE_APPROVAL = "require_approval"
    DENY = "deny"


class ApprovalStatus(_StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    NOT_REQUIRED = "not_required"
    AUTO_REJECTED = "auto_rejected"


class ReconciliationStatus(_StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    WARNING = "warning"
    SKIPPED = "skipped"


class VerificationStatus(_StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    WARNING = "warning"
    SKIPPED = "skipped"


class AnomalySeverity(_StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class AnomalySource(_StrEnum):
    """Provenance of an anomaly.

    A probabilistic finding is never presented as a verified fact (spec section 21).
    """

    DETERMINISTIC = "deterministic"
    MODEL = "model"
    JEV = "jev"
    HUMAN = "human"


class AnomalyKind(_StrEnum):
    """Bounded set of anomaly types ExcelPilot detects.

    Use ``UNCLASSIFIED`` rather than inventing a new member ad hoc; new detectors
    must be added deliberately.
    """

    ROW_COUNT_CHANGE = "row_count_change"
    LARGE_VALUE_CHANGE = "large_value_change"
    NULL_RATE_INCREASE = "null_rate_increase"
    DUPLICATE_SPIKE = "duplicate_spike"
    RECONCILIATION_VARIANCE = "reconciliation_variance"
    UNEXPECTED_FORMULA_CHANGE = "unexpected_formula_change"
    STRUCTURAL_CHANGE = "structural_change"
    OUTLIER_VALUE = "outlier_value"
    PLANNED_VS_ACTUAL_DIVERGENCE = "planned_vs_actual_divergence"
    INJECTION_ATTEMPT = "injection_attempt"
    UNCLASSIFIED = "unclassified"


class JevProvider(_StrEnum):
    """Which JEV endpoint to use.

    ``AUTO`` resolves from credential *presence* only. It never probes a network
    endpoint and never falls back between providers (ADR-0004).
    """

    AUTO = "auto"
    TYPESAFE = "typesafe"
    OPENROUTER = "openrouter"
    DISABLED = "disabled"
    MOCK = "mock"
