"""Deterministic policy: the sole authority on permission.

Imports ``app.contracts`` only. It must not import the planner, the JEV client,
or the executor, and ``tests/test_architecture.py`` enforces that statically — the
rule engine physically cannot consult a model (ADR-0005).
"""

from app.policy.engine import PolicyEngine, explain
from app.policy.rules import (
    ALL_RULES,
    ESCALATION_RULES,
    HARD_DENY_RULES,
    READ_ONLY_KINDS,
    Rule,
    RuleOutcome,
    rule_catalog,
)

__all__ = [
    "ALL_RULES",
    "ESCALATION_RULES",
    "HARD_DENY_RULES",
    "READ_ONLY_KINDS",
    "PolicyEngine",
    "Rule",
    "RuleOutcome",
    "explain",
    "rule_catalog",
]
