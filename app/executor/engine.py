"""The deterministic executor.

The **only** component that mutates a workbook. It receives a validated
``ExecutionPlan`` — never prose, never model output, never a JEV decision — and
applies it deterministically.

Defence in depth: for every operation the executor independently

1. re-validates it against its pydantic model,
2. re-resolves its target against the workbook *as it now exists*,
3. re-evaluates deterministic policy,
4. only then mutates.

Steps 1–3 are redundant with the earlier gate by design. They catch a plan mutated
between approval and execution, and they make the executor safe to call directly
from tests or the dashboard (ADR-0005, ADR-0006).

There is no ``eval``, no ``exec``, no shell-out, and no import of the planner or
the JEV client anywhere in this package.
"""

from __future__ import annotations

from pathlib import Path

from openpyxl.workbook import Workbook

from app.contracts.config import ExcelPilotConfig
from app.contracts.enums import PolicyOutcome
from app.contracts.errors import ExcelPilotError, PolicyDenied
from app.contracts.operations import WorkbookOperation
from app.contracts.pipeline import (
    ExecutionPlan,
    ExecutionState,
    OperationResult,
    PolicyRequest,
)
from app.contracts.workbook import WorkbookInspection
from app.executor.operations import OperationContext
from app.executor.registry import handler_for
from app.policy import PolicyEngine
from app.workbook.table import resolve_target


class ExecutionError(Exception):
    """An operation failed. Carries the partial results already produced.

    Raised rather than swallowed, so a run that applied 3 of 5 operations is
    reported as failed with an accurate record of what happened, rather than
    appearing to succeed.
    """

    def __init__(self, message: str, *, state: ExecutionState) -> None:
        super().__init__(message)
        self.state = state


class Executor:
    """Applies an execution plan to an open workbook."""

    def __init__(self, config: ExcelPilotConfig | None = None) -> None:
        self.config = config or ExcelPilotConfig()
        self.policy = PolicyEngine(self.config)

    def execute(
        self,
        workbook: Workbook,
        plan: ExecutionPlan,
        inspection: WorkbookInspection,
        *,
        output_path: str | Path | None = None,
    ) -> ExecutionState:
        """Apply a plan to an open workbook.

        The workbook is mutated in place; saving is the caller's decision, which
        keeps "executed" and "saved" as separate, separately-reported facts.
        """
        state = ExecutionState(run_id=plan.run_id, status="executing")
        context = OperationContext(
            neutralize_formula_injection=self.config.output.neutralize_formula_injection
        )

        # A plan that repeats an identical mutating operation would apply the same
        # change twice. ExcelPilot does not assume idempotence.
        duplicates = plan.duplicate_operations()
        if duplicates:
            state.status = "failed"
            state.errors.append(
                "plan contains repeated identical mutating operations, which ExcelPilot "
                "will not apply twice: "
                + ", ".join(sorted({op.operation.value for op in duplicates}))
            )
            return state

        for index, operation in enumerate(plan.operations):
            # Guards run before the handler so nothing mutates if one fails, but a
            # guard failure is still recorded in the operation list. Otherwise a
            # run that failed on its first operation would report an empty
            # operation list, which reads as "nothing was attempted" rather than
            # "this was rejected".
            try:
                self._guard_operation(workbook, operation, inspection, output_path)
            except ExcelPilotError as error:
                state.operations.append(
                    OperationResult(
                        operation=operation.operation.value,
                        status="failed",
                        error=f"blocked before execution: {error}",
                        details={"guard": "policy_or_target_check"},
                    )
                )
                state.errors.append(f"{operation.operation.value}: {error}")
                state.status = "failed"
                raise ExecutionError(
                    f"operation {index + 1} of {len(plan.operations)} was blocked: {error}",
                    state=state,
                ) from error

            handler = handler_for(operation.operation)
            try:
                result = handler(workbook, operation, context)
            except Exception as error:  # noqa: BLE001 - reported, not swallowed
                failed = OperationResult(
                    operation=operation.operation.value,
                    status="failed",
                    error=f"{type(error).__name__}: {error}",
                )
                state.operations.append(failed)
                state.errors.append(f"{operation.operation.value}: {error}")
                state.status = "failed"
                raise ExecutionError(
                    f"operation {index + 1} of {len(plan.operations)} failed: {error}",
                    state=state,
                ) from error

            state.operations.append(result)
            state.total_cells_written += result.cells_written
            state.total_rows_affected += result.rows_affected + result.rows_removed
            state.total_formulas_added += result.formulas_added
            state.total_formulas_removed += result.formulas_removed
            if result.error and result.status == "failed":
                state.errors.append(f"{result.operation}: {result.error}")

        state.status = "failed" if state.errors else "applied"
        return state

    def _guard_operation(
        self,
        workbook: Workbook,
        operation: WorkbookOperation,
        inspection: WorkbookInspection,
        output_path: str | Path | None,
    ) -> None:
        """Re-validate, re-resolve, and re-check policy before mutating.

        Raises before any change if any check fails.
        """
        # 1. Re-validate. Round-tripping through the model catches a plan object
        #    that was constructed loosely or mutated in place.
        validated = type(operation).model_validate_json(operation.model_dump_json())

        # 2. Re-resolve the target against the workbook as it exists now. A sheet
        #    renamed by an earlier operation in this same plan will fail here,
        #    which is exactly the intent.
        if hasattr(validated, "target"):
            resolve_target(workbook, validated.target)

        # 3. Re-evaluate policy.
        request = self._policy_request(validated, inspection, output_path)
        decision = self.policy.evaluate(request)
        if decision.outcome is PolicyOutcome.DENY:
            raise PolicyDenied(
                "policy denied this operation at execution time: " + "; ".join(decision.reasons),
                outcome=decision.outcome,
                rule_ids=decision.rule_ids,
            )

    def _policy_request(
        self,
        operation: WorkbookOperation,
        inspection: WorkbookInspection,
        output_path: str | Path | None,
    ) -> PolicyRequest:
        """Build the policy request for a single operation."""
        from app.executor.preview import preview_operation

        # Measured, not assumed: the preview simulates the operation in memory
        # and reports what it would actually touch.
        preview = preview_operation(operation, inspection, limits=self.config.limits)
        return PolicyRequest(
            operations=[operation],
            sheet_names=list(inspection.sheet_names),
            total_rows=inspection.total_rows,
            total_cells=inspection.total_non_empty_cells,
            sensitivity_level=inspection.sensitivity.level,
            has_hidden_sheets=inspection.hidden_sheet_count > 0,
            has_vba=inspection.metadata.has_vba,
            structural_change=preview.structural_change,
            cells_affected=preview.cells_to_change,
            formulas_removed=preview.formulas_to_remove,
            output_overwrites_source=_overwrites_source(inspection, output_path),
            requested_output_path=str(output_path) if output_path else None,
            workspace_root=self.config.workspace_root,
            config_fingerprint=self.config.fingerprint(),
        )


def _overwrites_source(inspection: WorkbookInspection, output_path: str | Path | None) -> bool:
    """Whether the requested output is the source workbook.

    Compared on resolved paths so ``./a.xlsx`` and ``/abs/dir/a.xlsx`` are
    recognised as the same file.
    """
    if output_path is None:
        return False
    try:
        return Path(output_path).expanduser().resolve() == Path(inspection.path).resolve()
    except (OSError, RuntimeError):
        return False


__all__ = ["ExecutionError", "Executor"]
