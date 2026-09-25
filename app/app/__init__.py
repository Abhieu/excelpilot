"""The run orchestrator: the sole composer of the pipeline.

This package is above every other layer. It is the only place that knows the
whole flow, and therefore the only place a bug could bypass a gate — which is
why ``tests/test_e2e.py`` exercises it end to end and
``tests/test_architecture.py`` confirms the layers below it stay independent.
"""

from app.app.orchestrator import (
    RunOrchestrator,
    RunResult,
    RunStateError,
    Stage,
)

__all__ = ["RunOrchestrator", "RunResult", "RunStateError", "Stage"]
