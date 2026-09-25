"""Configuration contracts.

Configuration is explicit and inspectable. The effective configuration is
fingerprinted into every run record, so a run can always be interpreted against
the settings that produced it (ADR-0005).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from pydantic import Field, field_validator, model_validator

from app.contracts.base import ContractModel
from app.contracts.enums import JevProvider, RiskLevel


class LimitsConfig(ContractModel):
    """Processing limits. These are security controls, not tuning knobs.

    Sized to admit real workbooks — the fixture set includes a 37,883x120 sheet —
    while rejecting pathological ones. Exceeding a limit raises ``LimitExceeded``
    rather than allocating unboundedly.
    """

    max_file_size_bytes: int = Field(default=256 * 1024 * 1024, ge=1_024)
    max_compression_ratio: int = Field(
        default=200,
        ge=1,
        description="Zip-bomb defence: uncompressed_size / compressed_size.",
    )
    max_sheets: int = Field(default=512, ge=1)
    max_rows_per_sheet: int = Field(default=1_048_576, ge=1)
    max_columns_per_sheet: int = Field(default=16_384, ge=1)
    max_total_cells: int = Field(default=20_000_000, ge=1)
    max_formula_count: int = Field(default=2_000_000, ge=0)
    max_untrusted_chars: int = Field(default=4_000, ge=100)


class PolicyThresholds(ContractModel):
    """Tunable escalation thresholds.

    Note what is *not* here: there is no setting that can disable a hard-deny
    rule. A configuration file can raise a threshold; it cannot grant permission
    the deny set forbids (ADR-0005).
    """

    cell_change_approval_threshold: int = Field(default=1_000, ge=0)
    row_change_approval_threshold: int = Field(default=500, ge=0)
    formula_removal_requires_approval: bool = True
    structural_change_requires_approval: bool = True
    hidden_sheet_change_requires_approval: bool = True
    restricted_data_requires_approval: bool = True
    ambiguous_task_requires_approval: bool = True
    max_operations_per_plan: int = Field(default=50, ge=1)
    deny_cells_affected_above: int = Field(
        default=20_000_000,
        ge=1,
        description="Hard deny above this. Not disableable via config.",
    )

    def require_approval_for_risk(self, risk: RiskLevel) -> bool:
        return risk in {RiskLevel.MEDIUM, RiskLevel.HIGH}


class JevConfig(ContractModel):
    provider: JevProvider = JevProvider.AUTO
    model: str | None = None
    timeout_seconds: float = Field(default=30.0, gt=0, le=300)
    min_probability: float = Field(
        default=0.8,
        ge=0.5,
        le=1,
        description=(
            "JEV's own uncalibrated starting point. Not a validated threshold for "
            "ExcelPilot's use; see docs/adr/0004-jev-integration.md."
        ),
    )
    min_margin: float = Field(default=0.15, ge=0, le=1)
    review_labels: list[str] = Field(
        default_factory=lambda: [
            "other",
            "unknown",
            "abstain",
            "review",
            "ask_user",
            "wait",
            "none",
            "defer",
            "insufficient_evidence",
        ],
    )
    enabled: bool = True

    @model_validator(mode="after")
    def _disabled_provider_is_disabled(self) -> JevConfig:
        if self.provider is JevProvider.DISABLED:
            object.__setattr__(self, "enabled", False)
        return self


class ModelConfig(ContractModel):
    """LLM provider configuration.

    The deterministic planner is the default and needs none of this. These
    settings only matter when an operator opts into LLM planning.
    """

    provider: str = Field(default="openai_compatible")
    base_url: str | None = None
    model: str | None = None
    api_key_env: str = Field(
        default="OPENAI_API_KEY",
        description="Name of the environment variable holding the key. Never the key itself.",
    )
    timeout_seconds: float = Field(default=60.0, gt=0, le=300)
    max_tokens: int = Field(default=4_000, ge=256, le=200_000)
    temperature: float = Field(default=0.0, ge=0, le=2)
    enabled: bool = False


class ReconciliationConfig(ContractModel):
    default_tolerance: float = Field(default=0.0, ge=0)
    default_relative_tolerance: float = Field(default=0.0, ge=0, le=1)
    fail_run_on_variance: bool = True


class OutputConfig(ContractModel):
    versioned_outputs: bool = True
    never_overwrite_source: bool = Field(
        default=True,
        description=(
            "Hard-coded safe default. There is deliberately no configuration "
            "value that permits writing to the source workbook (ADR-0010)."
        ),
    )
    atomic_write: bool = True
    keep_snapshot: bool = True
    neutralize_formula_injection: bool = True


class VerificationConfig(ContractModel):
    """Verification options.

    ``enable_recalculation`` only ever *adds* evidence: when the optional
    ``formulas`` library is installed, verification additionally evaluates the
    workbook's formulas and reports a real recalculation. With it absent, checks
    remain static and ``recalculated`` is False. Either way the result states
    which happened.
    """

    enable_recalculation: bool = True
    max_recalculation_cells: int = Field(
        default=500_000,
        ge=1_000,
        description="Above this formula count, recalculation is skipped and the reason stated.",
    )
    recalculation_timeout_seconds: float = Field(default=120.0, gt=0, le=1_800)


class AnomalyConfig(ContractModel):
    row_count_change_ratio: float = Field(default=0.10, ge=0, le=1)
    null_rate_increase: float = Field(default=0.05, ge=0, le=1)
    duplicate_rate_increase: float = Field(default=0.05, ge=0, le=1)
    large_value_change_ratio: float = Field(default=0.50, ge=0)
    outlier_z_score: float = Field(default=3.5, gt=0)
    planned_vs_actual_divergence: float = Field(default=0.25, ge=0)


class ExcelPilotConfig(ContractModel):
    """Top-level configuration."""

    workspace_root: str = Field(default=".")
    runs_dir: str = Field(default=".excelpilot")
    limits: LimitsConfig = Field(default_factory=LimitsConfig)
    policy: PolicyThresholds = Field(default_factory=PolicyThresholds)
    jev: JevConfig = Field(default_factory=JevConfig)
    model: ModelConfig = Field(default_factory=ModelConfig)
    reconciliation: ReconciliationConfig = Field(default_factory=ReconciliationConfig)
    output: OutputConfig = Field(default_factory=OutputConfig)
    anomaly: AnomalyConfig = Field(default_factory=AnomalyConfig)
    verification: VerificationConfig = Field(default_factory=VerificationConfig)
    allow_paid_calls: bool = Field(
        default=False,
        description="Master switch for live JEV/LLM calls. Also gated by --allow-paid-calls.",
    )
    audit_enabled: bool = True

    @field_validator("workspace_root")
    @classmethod
    def _absolute_root(cls, value: str) -> str:
        return str(Path(value).expanduser().resolve())

    def fingerprint(self) -> str:
        """Stable hash of the effective configuration.

        Recorded in every run so a run can be interpreted against the settings
        that produced it. Excludes the workspace path so the same settings in two
        locations compare equal.
        """
        payload = self.model_dump(mode="json", exclude={"workspace_root", "runs_dir"})
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()[:16]

    def runs_path(self) -> Path:
        return Path(self.workspace_root) / self.runs_dir / "runs"

    @classmethod
    def load(cls, path: str | Path | None) -> ExcelPilotConfig:
        """Load configuration from a TOML file, falling back to defaults.

        Unknown keys are rejected rather than ignored, so a typo in a config file
        surfaces immediately instead of silently leaving a default in place.
        """
        if path is None:
            return cls()
        config_path = Path(path).expanduser()
        if not config_path.is_file():
            from app.contracts.errors import ConfigError

            raise ConfigError(f"config file not found: {config_path}")
        try:
            import tomllib

            data: dict[str, Any] = tomllib.loads(config_path.read_text(encoding="utf-8"))
        except Exception as error:  # noqa: BLE001 - surfaced as ConfigError
            from app.contracts.errors import ConfigError

            raise ConfigError(f"could not parse {config_path}: {error}") from error
        try:
            return cls.model_validate(data)
        except Exception as error:  # noqa: BLE001 - surfaced as ConfigError
            from app.contracts.errors import ConfigError

            raise ConfigError(f"invalid configuration in {config_path}: {error}") from error


__all__ = [
    "AnomalyConfig",
    "ExcelPilotConfig",
    "JevConfig",
    "LimitsConfig",
    "ModelConfig",
    "OutputConfig",
    "PolicyThresholds",
    "ReconciliationConfig",
    "VerificationConfig",
]
