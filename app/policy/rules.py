"""Deterministic policy rules.

A pure function of typed facts — no network, no model, no clock, no randomness
(ADR-0005). Each rule is a small named predicate so a decision can cite the
rules that produced it and a test can exercise it in isolation.

Rule classes, evaluated in this order, deny winning over approve:

1. **Hard deny** — cannot be overridden by configuration or by any other signal.
2. **Escalation** — sets ``require_approval``. Sticky: a later allow cannot clear it.
3. **Default** — ``allow`` for read-only work.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.contracts.config import PolicyThresholds
from app.contracts.enums import PolicyOutcome
from app.contracts.operations import (
    Target,
)
from app.contracts.pipeline import PolicyRequest

#: Operations that may run without approval when nothing else escalates.
#: Deliberately conservative: anything not listed is treated as mutating.
READ_ONLY_KINDS = frozenset({"read_range", "compare_workbooks", "reconcile"})

#: Sheet-name fragments that indicate a workbook's internal state. Touching one is
#: escalated because the operator almost never means to edit it.
PROTECTED_SHEET_MARKERS = (
    "_audit",
    "_lists",
    "_lookup",
    "_config",
    "_internal",
    "_state",
    "_meta",
    "veryhidden",
)


@dataclass(frozen=True, slots=True)
class RuleOutcome:
    """What a single rule decided."""

    outcome: PolicyOutcome
    reason: str
    facts: dict[str, Any]


@dataclass(frozen=True, slots=True)
class Rule:
    """A named policy rule."""

    rule_id: str
    kind: str  # "deny" | "escalate"
    description: str
    evaluate: Callable[[PolicyRequest, PolicyThresholds], RuleOutcome | None]

    def __call__(self, request: PolicyRequest, thresholds: PolicyThresholds) -> RuleOutcome | None:
        return self.evaluate(request, thresholds)


def _target_sheet(target: Target) -> str:
    return target.sheet


def _is_protected_sheet(name: str) -> bool:
    lowered = name.strip().lower()
    return any(marker in lowered for marker in PROTECTED_SHEET_MARKERS)


# --------------------------------------------------------------------------
# Hard deny rules. Not configurable by design.
# --------------------------------------------------------------------------


def _deny_output_overwrites_source(
    request: PolicyRequest, thresholds: PolicyThresholds
) -> RuleOutcome | None:
    """Writing to the source path is never permitted.

    Hard-coded rather than configurable. There is no config value, flag, or
    environment variable that turns this off (ADR-0010).
    """
    if not request.output_overwrites_source:
        return None
    return RuleOutcome(
        PolicyOutcome.DENY,
        "the requested output path is the source workbook; ExcelPilot never writes to the source",
        {"output_path": request.requested_output_path},
    )


def _deny_output_escapes_workspace(
    request: PolicyRequest, thresholds: PolicyThresholds
) -> RuleOutcome | None:
    """Output must stay inside the configured workspace root."""
    output = request.requested_output_path
    root = request.workspace_root
    if not output or not root:
        return None
    from pathlib import Path

    try:
        resolved = Path(output).expanduser().resolve()
        root_resolved = Path(root).expanduser().resolve()
    except (OSError, RuntimeError):
        return RuleOutcome(
            PolicyOutcome.DENY,
            "the requested output path could not be resolved",
            {"output_path": output},
        )
    if not resolved.is_relative_to(root_resolved):
        return RuleOutcome(
            PolicyOutcome.DENY,
            f"output path {resolved} is outside the workspace root {root_resolved}",
            {"output_path": str(resolved), "workspace_root": str(root_resolved)},
        )
    return None


def _deny_scale_exceeded(
    request: PolicyRequest, thresholds: PolicyThresholds
) -> RuleOutcome | None:
    """Above the hard cell ceiling, refuse rather than attempt the work."""
    if request.cells_affected > thresholds.deny_cells_affected_above:
        return RuleOutcome(
            PolicyOutcome.DENY,
            f"{request.cells_affected:,} cells affected exceeds the hard limit of "
            f"{thresholds.deny_cells_affected_above:,}",
            {
                "cells_affected": request.cells_affected,
                "limit": thresholds.deny_cells_affected_above,
            },
        )
    return None


def _deny_too_many_operations(
    request: PolicyRequest, thresholds: PolicyThresholds
) -> RuleOutcome | None:
    if len(request.operations) > thresholds.max_operations_per_plan:
        return RuleOutcome(
            PolicyOutcome.DENY,
            f"plan has {len(request.operations)} operations, above the maximum of "
            f"{thresholds.max_operations_per_plan}",
            {"operations": len(request.operations), "limit": thresholds.max_operations_per_plan},
        )
    return None


def _deny_vba_workbook(request: PolicyRequest, thresholds: PolicyThresholds) -> RuleOutcome | None:
    """Refuse to mutate a macro-enabled workbook.

    openpyxl preserves the VBA project but cannot reason about it, so a mutation
    to a ``.xlsm`` risks producing a workbook whose macros no longer match its
    sheets. Reading is fine; writing is refused and reported clearly rather than
    silently producing something ExcelPilot cannot vouch for.
    """
    if not request.has_vba:
        return None
    mutating = [op for op in request.operations if op.operation.value not in READ_ONLY_KINDS]
    if not mutating:
        return None
    return RuleOutcome(
        PolicyOutcome.DENY,
        "workbook contains a VBA macro project; ExcelPilot can read it but will not write to it",
        {"has_vba": True, "mutating_operations": len(mutating)},
    )


def _deny_ambiguous_task(
    request: PolicyRequest, thresholds: PolicyThresholds
) -> RuleOutcome | None:
    """An under-specified request must not be guessed at.

    Refusing is the honest response. A planner that guesses produces a plausible
    workbook that nobody asked for.
    """
    if not request.ambiguity_signals:
        return None
    return RuleOutcome(
        PolicyOutcome.DENY,
        "the request could not be resolved to a specific operation set: "
        + "; ".join(request.ambiguity_signals[:5]),
        {"ambiguity_signals": request.ambiguity_signals},
    )


# --------------------------------------------------------------------------
# Escalation rules.
# --------------------------------------------------------------------------


def _escalate_bulk_change(
    request: PolicyRequest, thresholds: PolicyThresholds
) -> RuleOutcome | None:
    if request.cells_affected <= thresholds.cell_change_approval_threshold:
        return None
    return RuleOutcome(
        PolicyOutcome.REQUIRE_APPROVAL,
        f"{request.cells_affected:,} cells would change, above the approval threshold of "
        f"{thresholds.cell_change_approval_threshold:,}",
        {
            "cells_affected": request.cells_affected,
            "threshold": thresholds.cell_change_approval_threshold,
        },
    )


def _escalate_formula_removal(
    request: PolicyRequest, thresholds: PolicyThresholds
) -> RuleOutcome | None:
    if not thresholds.formula_removal_requires_approval or request.formulas_removed <= 0:
        return None
    return RuleOutcome(
        PolicyOutcome.REQUIRE_APPROVAL,
        f"{request.formulas_removed} formula(s) would be removed or overwritten",
        {"formulas_removed": request.formulas_removed},
    )


def _escalate_structural_change(
    request: PolicyRequest, thresholds: PolicyThresholds
) -> RuleOutcome | None:
    if not thresholds.structural_change_requires_approval or not request.structural_change:
        return None
    return RuleOutcome(
        PolicyOutcome.REQUIRE_APPROVAL,
        "the plan changes workbook structure (sheets added, removed, or renamed)",
        {"structural_change": True},
    )


def _escalate_destructive_operations(
    request: PolicyRequest, thresholds: PolicyThresholds
) -> RuleOutcome | None:
    """Operations that discard data always require a human."""
    destructive = [
        op.operation.value
        for op in request.operations
        if op.operation.value in {"remove_duplicates", "filter_rows"}
    ]
    if not destructive:
        return None
    return RuleOutcome(
        PolicyOutcome.REQUIRE_APPROVAL,
        f"destructive operation(s) present: {', '.join(sorted(set(destructive)))}",
        {"destructive_operations": sorted(set(destructive))},
    )


def _escalate_hidden_sheet_change(
    request: PolicyRequest, thresholds: PolicyThresholds
) -> RuleOutcome | None:
    if not thresholds.hidden_sheet_change_requires_approval or not request.has_hidden_sheets:
        return None
    touched = sorted(
        {
            _target_sheet(op.target)
            for op in request.operations
            if hasattr(op, "target")
            and op.operation.value not in READ_ONLY_KINDS
            and _is_protected_sheet(_target_sheet(op.target))
        }
    )
    if not touched:
        return None
    return RuleOutcome(
        PolicyOutcome.REQUIRE_APPROVAL,
        f"the plan writes to hidden or internal sheet(s): {', '.join(touched)}",
        {"sheets": touched},
    )


def _escalate_restricted_data(
    request: PolicyRequest, thresholds: PolicyThresholds
) -> RuleOutcome | None:
    if not thresholds.restricted_data_requires_approval:
        return None
    if request.sensitivity_level not in {"confidential", "restricted"}:
        return None
    return RuleOutcome(
        PolicyOutcome.REQUIRE_APPROVAL,
        f"workbook contains {request.sensitivity_level} data "
        "(detected from column headers); a human should confirm",
        {"sensitivity": request.sensitivity_level},
    )


def _escalate_ambiguity(request: PolicyRequest, thresholds: PolicyThresholds) -> RuleOutcome | None:
    """Ambiguity that did not already cause a hard deny still warrants review."""
    if not request.ambiguity_signals or not thresholds.ambiguous_task_requires_approval:
        return None
    return RuleOutcome(
        PolicyOutcome.REQUIRE_APPROVAL,
        "the request is ambiguous: " + "; ".join(request.ambiguity_signals[:5]),
        {"ambiguity_signals": request.ambiguity_signals},
    )


#: Evaluated in order. Deny rules first, so a denial is never masked by an
#: escalation, and escalations accumulate.
HARD_DENY_RULES: tuple[Rule, ...] = (
    Rule(
        "source_never_overwritten",
        "deny",
        "The source workbook is never a write target.",
        _deny_output_overwrites_source,
    ),
    Rule(
        "output_within_workspace",
        "deny",
        "Output must resolve inside the workspace root.",
        _deny_output_escapes_workspace,
    ),
    Rule(
        "cell_ceiling",
        "deny",
        "Refuse work above the hard cell ceiling.",
        _deny_scale_exceeded,
    ),
    Rule(
        "operation_ceiling",
        "deny",
        "Refuse plans with too many operations.",
        _deny_too_many_operations,
    ),
    Rule(
        "vba_read_only",
        "deny",
        "Macro-enabled workbooks may be read but not written.",
        _deny_vba_workbook,
    ),
    Rule(
        "no_guessing",
        "deny",
        "An unresolved request is refused rather than guessed at.",
        _deny_ambiguous_task,
    ),
)

ESCALATION_RULES: tuple[Rule, ...] = (
    Rule(
        "bulk_change",
        "escalate",
        "Large cell counts require approval.",
        _escalate_bulk_change,
    ),
    Rule(
        "formula_removal",
        "escalate",
        "Removing or overwriting formulas requires approval.",
        _escalate_formula_removal,
    ),
    Rule(
        "structural_change",
        "escalate",
        "Structural changes require approval.",
        _escalate_structural_change,
    ),
    Rule(
        "destructive_operation",
        "escalate",
        "Data-discarding operations require approval.",
        _escalate_destructive_operations,
    ),
    Rule(
        "hidden_sheet_change",
        "escalate",
        "Writing to hidden or internal sheets requires approval.",
        _escalate_hidden_sheet_change,
    ),
    Rule(
        "restricted_data",
        "escalate",
        "Confidential or restricted data requires approval.",
        _escalate_restricted_data,
    ),
    Rule(
        "ambiguous_task",
        "escalate",
        "An ambiguous request requires approval.",
        _escalate_ambiguity,
    ),
)

ALL_RULES: tuple[Rule, ...] = HARD_DENY_RULES + ESCALATION_RULES


def rule_catalog() -> list[dict[str, Any]]:
    """Machine-readable rule list, for ``excelpilot policy explain``."""
    return [
        {
            "id": rule.rule_id,
            "kind": rule.kind,
            "description": rule.description,
        }
        for rule in ALL_RULES
    ]


__all__ = [
    "ALL_RULES",
    "ESCALATION_RULES",
    "HARD_DENY_RULES",
    "PROTECTED_SHEET_MARKERS",
    "READ_ONLY_KINDS",
    "Rule",
    "RuleOutcome",
    "rule_catalog",
]
