"""Verification: structural, data, reconciliation, formula, and anomalies.

Independent of the executor. Depends on ``app.contracts`` and ``app.workbook``
only — not on the planner, the JEV client, or the CLI.
"""

from app.verification.anomalies import detect as detect_anomalies
from app.verification.anomalies import from_jev, from_model
from app.verification.formulas import (
    FormulaIssue,
    check_broken_references,
    check_column_consistency,
    collect_formulas,
    detect_formula_loss,
    detect_hardcoded_replacements,
    static_formula_checks,
    unresolvable_formulas,
)
from app.verification.structural import (
    check_data,
    check_structure,
    check_workbook_opens,
)
from app.verification.verifier import Verifier, describe

__all__ = [
    "FormulaIssue",
    "Verifier",
    "check_broken_references",
    "check_column_consistency",
    "check_data",
    "check_structure",
    "check_workbook_opens",
    "collect_formulas",
    "describe",
    "detect_anomalies",
    "detect_formula_loss",
    "detect_hardcoded_replacements",
    "from_jev",
    "from_model",
    "static_formula_checks",
    "unresolvable_formulas",
]
