"""Benchmark suite for ExcelPilot.

Compares a hand-built baseline plan, the deterministic planner without JEV, and
the full pipeline with JEV, across a set of scenarios that include cases where
refusing is the correct answer. See ``run.py``.
"""

from benchmarks.scenarios import SCENARIOS, Scenario, scenarios_by_name

__all__ = ["SCENARIOS", "Scenario", "scenarios_by_name"]
