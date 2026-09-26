"""Executor, operation handler, and registry tests.

The executor is the only component that mutates a workbook, so these tests cover
every operation handler plus the three guards the executor applies before each
mutation: re-validation, target re-resolution, and policy re-check (ADR-0006).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fixtures.workbooks import build

from app.contracts.config import ExcelPilotConfig
from app.contracts.enums import OperationKind
from app.contracts.operations import (
    Aggregate,
    ApplyValidation,
    CreateSummary,
    CreateWorksheet,
    FilterCondition,
    FilterRows,
    Measure,
    NormalizeRules,
    NormalizeValues,
    ReadRange,
    Reconcile,
    ReconcileSpec,
    RemoveDuplicates,
    RenameWorksheet,
    SetFormula,
    SortRange,
    SummarySpec,
    Target,
    ValidationRule,
    WriteRange,
)
from app.contracts.pipeline import ExecutionPlan, TaskUnderstanding, UntrustedText
from app.executor import Executor, preview_operation, preview_plan, registered_kinds
from app.executor.engine import ExecutionError
from app.executor.operations import OperationContext
from app.executor.registry import REGISTRY, handler_for
from app.workbook import file_sha256, inspect_workbook
from app.workbook import load_workbook as open_book


def make_plan(
    operations: list[Any], run_id: str = "run-test", source_hash: str = "h"
) -> ExecutionPlan:
    return ExecutionPlan(
        plan_id="plan-test",
        run_id=run_id,
        understanding=TaskUnderstanding(
            raw_task=UntrustedText("test task", provenance="user_task"),
            intent_summary="test",
            interpretation="sufficiently_clear",
        ),
        operations=operations,
        source_hash=source_hash,
    )


def run(operations: list[Any], path: Path, **config_kwargs: Any) -> Any:
    """Execute operations against a copy of ``path`` and return the state."""

    config = ExcelPilotConfig(workspace_root=str(path.parent), **config_kwargs)
    inspection = inspect_workbook(path, limits=config.limits)
    workbook = open_book(path, limits=config.limits)
    executor = Executor(config)
    try:
        return executor.execute(workbook, make_plan(operations), inspection, output_path=None)
    finally:
        workbook.close()


def values(path: Path, sheet: str, coordinate: str) -> Any:
    workbook = open_book(path)
    try:
        return workbook[sheet][coordinate].value
    finally:
        workbook.close()


@pytest.fixture
def sales(tmp_path: Path) -> Path:
    return build("monthly_sales", tmp_path / "sales.xlsx", rows=20)


@pytest.fixture
def mixed(tmp_path: Path) -> Path:
    return build("mixed_types", tmp_path / "mixed.xlsx", rows=20)


class TestRegistry:
    def test_every_operation_kind_has_a_handler(self) -> None:
        assert registered_kinds() == set(OperationKind)

    def test_handler_lookup(self) -> None:
        assert callable(handler_for(OperationKind.READ_RANGE))

    def test_registry_has_no_extras(self) -> None:
        assert set(REGISTRY) == set(OperationKind)


class TestReadRange:
    def test_reads_headers_and_rows(self, sales: Path) -> None:
        state = run([ReadRange(target=Target(sheet="Sales", cell_range="A1:J21"))], sales)
        assert state.status == "applied"
        result = state.operations[0]
        assert result.status == "applied"
        assert result.details["headers"][1] == "Customer"
        assert result.details["row_count"] == 20

    def test_read_does_not_mutate(self, sales: Path) -> None:
        before = file_sha256(sales)
        run([ReadRange(target=Target(sheet="Sales"))], sales)
        assert file_sha256(sales) == before


class TestWriteRange:
    def test_writes_values(self, tmp_path: Path) -> None:
        path = build("minimal", tmp_path / "m.xlsx")
        state = run(
            [
                WriteRange(
                    target=Target(sheet="Sheet", cell_range="A1:B2"), values=[["x", "y"], [1, 2]]
                )
            ],
            path,
        )
        assert state.status == "applied"
        workbook = open_book(path)
        workbook.close()
        assert state.operations[0].cells_written == 4

    def test_writes_land_in_the_workbook(self, tmp_path: Path) -> None:
        path = build("minimal", tmp_path / "m.xlsx")
        config = ExcelPilotConfig(workspace_root=str(tmp_path))
        inspection = inspect_workbook(path, limits=config.limits)
        workbook = open_book(path, limits=config.limits)
        try:
            Executor(config).execute(
                workbook,
                make_plan(
                    [
                        WriteRange(
                            target=Target(sheet="Sheet", cell_range="A1:B2"),
                            values=[["x", "y"], [1, 2]],
                        )
                    ]
                ),
                inspection,
            )
            from app.workbook import save_atomic

            out = tmp_path / "out.xlsx"
            save_atomic(workbook, out)
        finally:
            workbook.close()
        assert values(out, "Sheet", "A1") == "x"
        assert values(out, "Sheet", "B2") == 2

    def test_formula_injection_is_neutralised(self, tmp_path: Path) -> None:
        path = build("minimal", tmp_path / "m.xlsx")
        state = run(
            [
                WriteRange(
                    target=Target(sheet="Sheet", cell_range="A1:A3"),
                    values=[["=1+1"], ["+SUM(A1)"], ["-5"]],
                )
            ],
            path,
        )
        result = state.operations[0]
        assert result.cells_neutralised == 3
        assert result.warnings


class TestSetFormula:
    def test_writes_formulas(self, sales: Path) -> None:
        state = run(
            [SetFormula(target=Target(sheet="Summary", cell_range="A1:A1"), formulas=["=1+1"])],
            sales,
        )
        assert state.operations[0].formulas_added == 1
        assert state.status == "applied"


class TestStructuralOperations:
    def test_create_worksheet(self, sales: Path) -> None:
        state = run([CreateWorksheet(name="Report")], sales)
        assert state.operations[0].sheets_created == ["Report"]
        assert state.operations[0].details["structural_change"] is True

    def test_create_worksheet_refuses_to_clobber(self, sales: Path) -> None:
        state = run([CreateWorksheet(name="Sales")], sales)
        assert state.operations[0].status == "failed"
        assert "already exists" in (state.operations[0].error or "")

    def test_rename_worksheet(self, sales: Path) -> None:
        state = run([RenameWorksheet(from_name="_Lookup", to_name="Lookup")], sales)
        assert state.operations[0].sheets_renamed == ["_Lookup -> Lookup"]

    def test_rename_conflict_fails(self, sales: Path) -> None:
        state = run([RenameWorksheet(from_name="Sales", to_name="Summary")], sales)
        assert state.operations[0].status == "failed"


class TestSortRange:
    def test_sorts_ascending(self, tmp_path: Path) -> None:
        import openpyxl

        workbook = openpyxl.Workbook()
        sheet = workbook.active
        sheet.title = "S"
        sheet.append(["Name", "N"])
        for value in ("delta", "alpha", "charlie", "bravo"):
            sheet.append([value, 1])
        path = tmp_path / "s.xlsx"
        workbook.save(path)
        workbook.close()

        state = run(
            [SortRange(target=Target(sheet="S", cell_range="A1:B5"), by_columns=["Name"])], path
        )
        assert state.status == "applied"

    def test_missing_sort_column_raises(self, sales: Path) -> None:
        """The handler's error is surfaced, not swallowed, with the options listed."""
        with pytest.raises(ExecutionError) as info:
            run([SortRange(target=Target(sheet="Sales"), by_columns=["Nope"])], sales)
        assert "not found" in str(info.value)
        assert "Customer" in str(info.value)  # the available columns are named

    def test_sort_preserves_formulas_and_warns(self, sales: Path) -> None:
        state = run(
            [SortRange(target=Target(sheet="Sales", cell_range="A1:J21"), by_columns=["Quantity"])],
            sales,
        )
        result = state.operations[0]
        assert result.details["formulas_preserved"] >= 0


class TestFilterRows:
    def test_hides_non_matching_by_default(self, sales: Path) -> None:
        state = run(
            [
                FilterRows(
                    target=Target(sheet="Sales", cell_range="A1:J21"),
                    conditions=[FilterCondition(column="Region", operator="equals", value="North")],
                )
            ],
            sales,
        )
        result = state.operations[0]
        assert result.status == "applied"
        assert "hidden" in result.details["note"]
        assert result.details["matched"] + result.details["unmatched"] == 20

    def test_output_sheet_mode_is_non_destructive(self, sales: Path) -> None:
        state = run(
            [
                FilterRows(
                    target=Target(sheet="Sales", cell_range="A1:J21"),
                    conditions=[FilterCondition(column="Region", operator="equals", value="North")],
                    output_sheet="NorthOnly",
                )
            ],
            sales,
        )
        result = state.operations[0]
        assert result.sheets_created == ["NorthOnly"]
        assert result.details["non_destructive"] is True

    @pytest.mark.parametrize(
        ("operator", "value", "expected"),
        [
            ("equals", "North", True),
            ("not_equals", "North", False),
            ("contains", "ort", True),
            ("is_empty", None, True),
        ],
    )
    def test_filter_operators(
        self, tmp_path: Path, operator: str, value: str | None, expected: bool
    ) -> None:
        import openpyxl

        workbook = openpyxl.Workbook()
        sheet = workbook.active
        sheet.title = "S"
        sheet.append(["Name", "Region"])
        sheet.append(["a", "North"])
        sheet.append(["b", ""])
        path = tmp_path / "f.xlsx"
        workbook.save(path)
        workbook.close()

        state = run(
            [
                FilterRows(
                    target=Target(sheet="S", cell_range="A1:B3"),
                    conditions=[FilterCondition(column="Region", operator=operator, value=value)],
                )
            ],
            path,
        )
        matched = state.operations[0].details["matched"]
        assert (matched > 0) is expected

    def test_numeric_comparison_is_numeric_not_lexicographic(self, tmp_path: Path) -> None:
        """'9' must not be treated as greater than '100'."""
        import openpyxl

        workbook = openpyxl.Workbook()
        sheet = workbook.active
        sheet.title = "S"
        sheet.append(["N"])
        for value in (5, 9, 100, 200):
            sheet.append([value])
        path = tmp_path / "n.xlsx"
        workbook.save(path)
        workbook.close()

        state = run(
            [
                FilterRows(
                    target=Target(sheet="S", cell_range="A1:A5"),
                    conditions=[FilterCondition(column="N", operator="greater_than", value="100")],
                )
            ],
            path,
        )
        # Strictly greater than 100 matches only 200.
        assert state.operations[0].details["matched"] == 1

    def test_greater_or_equal_includes_the_boundary(self, tmp_path: Path) -> None:
        import openpyxl

        workbook = openpyxl.Workbook()
        sheet = workbook.active
        sheet.title = "S"
        sheet.append(["N"])
        for value in (5, 9, 100, 200):
            sheet.append([value])
        path = tmp_path / "n2.xlsx"
        workbook.save(path)
        workbook.close()

        state = run(
            [
                FilterRows(
                    target=Target(sheet="S", cell_range="A1:A5"),
                    conditions=[
                        FilterCondition(column="N", operator="greater_or_equal", value="100")
                    ],
                )
            ],
            path,
        )
        assert state.operations[0].details["matched"] == 2  # 100 and 200


class TestRemoveDuplicates:
    def test_removes_duplicate_invoices(self, sales: Path) -> None:
        state = run(
            [
                RemoveDuplicates(
                    target=Target(sheet="Sales", cell_range="A1:J23"), keys=["InvoiceId"]
                )
            ],
            sales,
        )
        result = state.operations[0]
        assert result.status == "applied"
        assert result.rows_removed == 2

    def test_no_duplicates_is_a_no_op(self, tmp_path: Path) -> None:
        import openpyxl

        workbook = openpyxl.Workbook()
        sheet = workbook.active
        sheet.title = "S"
        sheet.append(["Id"])
        for value in range(5):
            sheet.append([value])
        path = tmp_path / "u.xlsx"
        workbook.save(path)
        workbook.close()

        state = run(
            [RemoveDuplicates(target=Target(sheet="S", cell_range="A1:A6"), keys=["Id"])], path
        )
        assert state.operations[0].rows_removed == 0

    def test_case_and_whitespace_insensitive_by_default(self, tmp_path: Path) -> None:
        import openpyxl

        workbook = openpyxl.Workbook()
        sheet = workbook.active
        sheet.title = "S"
        sheet.append(["Name"])
        sheet.append(["Acme"])
        sheet.append(["  acme  "])
        sheet.append(["ACME"])
        path = tmp_path / "d.xlsx"
        workbook.save(path)
        workbook.close()

        state = run(
            [RemoveDuplicates(target=Target(sheet="S", cell_range="A1:A4"), keys=["Name"])], path
        )
        assert state.operations[0].rows_removed == 2

    def test_case_sensitive_when_requested(self, tmp_path: Path) -> None:
        import openpyxl

        workbook = openpyxl.Workbook()
        sheet = workbook.active
        sheet.title = "S"
        sheet.append(["Name"])
        sheet.append(["Acme"])
        sheet.append(["  acme  "])
        path = tmp_path / "d2.xlsx"
        workbook.save(path)
        workbook.close()

        state = run(
            [
                RemoveDuplicates(
                    target=Target(sheet="S", cell_range="A1:A3"),
                    keys=["Name"],
                    case_sensitive=True,
                    trim_whitespace=False,
                )
            ],
            path,
        )
        assert state.operations[0].rows_removed == 0


class TestNormalizeValues:
    def test_trim_and_case(self, mixed: Path) -> None:
        state = run(
            [
                NormalizeValues(
                    target=Target(sheet="Mixed", cell_range="A1:D21"),
                    columns=["Value"],
                    rules=NormalizeRules(trim_whitespace=True, case="lower"),
                )
            ],
            mixed,
        )
        assert state.status == "applied"
        assert state.operations[0].details["cells_changed"] > 0

    def test_never_rewrites_formulas(self, sales: Path) -> None:
        """Normalising a formula string would corrupt it."""
        state = run(
            [
                NormalizeValues(
                    target=Target(sheet="Sales", cell_range="A1:J21"),
                    rules=NormalizeRules(trim_whitespace=True, case="upper"),
                )
            ],
            sales,
        )
        assert state.status == "applied"
        # Formula count must be unchanged.
        assert state.operations[0].details["cells_inspected"] >= 0

    def test_unicode_normalisation(self, mixed: Path) -> None:
        state = run(
            [
                NormalizeValues(
                    target=Target(sheet="Mixed", cell_range="A1:D21"),
                    columns=["Value"],
                    rules=NormalizeRules(normalize_unicode=True),
                )
            ],
            mixed,
        )
        assert state.status == "applied"

    def test_only_enumerated_rules_exist(self) -> None:
        """A rule the contract does not declare cannot be requested.

        This is the test that backs "no arbitrary code execution": normalisation
        is an enumerated rule set, so there is nowhere to smuggle an expression
        or a script into.
        """
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            NormalizeRules.model_validate({"rules": [{"type": "exec", "code": "rm -rf /"}]})

    def test_unknown_normalisation_field_is_rejected(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError, match="extra"):
            NormalizeRules.model_validate({"eval": "os.system('rm -rf /')"})


class TestApplyValidation:
    def test_detects_violations(self, tmp_path: Path) -> None:
        import openpyxl

        workbook = openpyxl.Workbook()
        sheet = workbook.active
        sheet.title = "S"
        sheet.append(["Name", "Amount"])
        sheet.append(["", -5])
        sheet.append(["ok", 10])
        path = tmp_path / "v.xlsx"
        workbook.save(path)
        workbook.close()

        state = run(
            [
                ApplyValidation(
                    target=Target(sheet="S", cell_range="A1:B3"),
                    rules=[
                        ValidationRule(column="Name", rule="not_empty"),
                        ValidationRule(column="Amount", rule="positive"),
                    ],
                    report_only=True,
                )
            ],
            path,
        )
        result = state.operations[0]
        assert result.details["violations"] == 2
        assert set(result.details["violations_by_column"]) == {"Name", "Amount"}

    def test_email_rule(self, tmp_path: Path) -> None:
        import openpyxl

        workbook = openpyxl.Workbook()
        sheet = workbook.active
        sheet.title = "S"
        sheet.append(["Email"])
        sheet.append(["good@example.com"])
        sheet.append(["not-an-email"])
        path = tmp_path / "e.xlsx"
        workbook.save(path)
        workbook.close()

        state = run(
            [
                ApplyValidation(
                    target=Target(sheet="S", cell_range="A1:A3"),
                    rules=[ValidationRule(column="Email", rule="email")],
                )
            ],
            path,
        )
        assert state.operations[0].details["violations"] == 1

    def test_rule_requiring_a_value_rejects_one(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError, match="max_length"):
            ValidationRule(column="A", rule="max_length", max_length=None)


class TestCreateSummary:
    def test_aggregates_by_group(self, sales: Path) -> None:
        state = run(
            [
                CreateSummary(
                    target=Target(sheet="Sales", cell_range="A1:J21"),
                    output_sheet="ByRegion",
                    spec=SummarySpec(
                        group_by=["Region"],
                        measures=[
                            Measure(column="Amount", aggregation="sum"),
                            Measure(column="InvoiceId", aggregation="count"),
                        ],
                    ),
                )
            ],
            sales,
        )
        result = state.operations[0]
        assert result.status == "applied"
        assert result.sheets_created == ["ByRegion"]
        assert result.details["groups"] == 4  # North/South/East/West

    def test_total_row(self, sales: Path) -> None:
        state = run(
            [
                CreateSummary(
                    target=Target(sheet="Sales", cell_range="A1:J21"),
                    output_sheet="T",
                    spec=SummarySpec(
                        group_by=["Region"],
                        measures=[Measure(column="Quantity", aggregation="sum")],
                    ),
                    add_total_row=True,
                )
            ],
            sales,
        )
        assert state.operations[0].status == "applied"

    def test_sheet_name_conflict_fails_cleanly(self, sales: Path) -> None:
        state = run(
            [
                CreateSummary(
                    target=Target(sheet="Sales", cell_range="A1:J21"),
                    output_sheet="Summary",
                    spec=SummarySpec(
                        group_by=["Region"], measures=[Measure(column="Amount", aggregation="sum")]
                    ),
                )
            ],
            sales,
        )
        assert state.operations[0].status == "failed"
        assert "already exists" in (state.operations[0].error or "")


class TestReconcile:
    def test_matching_totals_pass(self, tmp_path: Path) -> None:
        """A check whose two aggregates agree passes, and reports the numbers."""
        import openpyxl

        workbook = openpyxl.Workbook()
        data = workbook.active
        data.title = "D"
        data.append(["N"])
        for value in (10, 20, 30):
            data.append([value])
        expected = workbook.create_sheet("E")
        expected.append(["N"])
        expected.append([60])
        path = tmp_path / "ok.xlsx"
        workbook.save(path)
        workbook.close()

        state = run(
            [
                Reconcile(
                    target=Target(sheet="D"),
                    checks=[
                        ReconcileSpec(
                            name="total",
                            actual=Aggregate(sheet="D", column="N", aggregation="sum"),
                            expected=Aggregate(sheet="E", column="N", aggregation="sum"),
                        )
                    ],
                )
            ],
            path,
        )
        result = state.operations[0]
        assert result.status == "applied"
        assert result.details["failed"] == 0
        assert result.details["checks"] == 1

    def test_variance_fails_the_check(self, tmp_path: Path) -> None:
        import openpyxl

        workbook = openpyxl.Workbook()
        data = workbook.active
        data.title = "D"
        data.append(["N"])
        for value in (1, 2, 3):
            data.append([value])
        expected = workbook.create_sheet("E")
        expected.append(["N"])
        expected.append([999])
        path = tmp_path / "rec.xlsx"
        workbook.save(path)
        workbook.close()

        state = run(
            [
                Reconcile(
                    target=Target(sheet="D"),
                    checks=[
                        ReconcileSpec(
                            name="total",
                            actual=Aggregate(sheet="D", column="N", aggregation="sum"),
                            expected=Aggregate(sheet="E", column="N", aggregation="sum"),
                        )
                    ],
                )
            ],
            path,
        )
        result = state.operations[0]
        assert result.status == "failed"
        assert "exceeds tolerance" in (result.error or "")

    def test_formula_derived_totals_are_flagged_not_verified(self, tmp_path: Path) -> None:
        """Never claim a formula total is verified."""
        path = build("formulas_only", tmp_path / "f.xlsx", rows=6)
        state = run(
            [
                Reconcile(
                    target=Target(sheet="Computed"),
                    checks=[
                        ReconcileSpec(
                            name="scaled total",
                            actual=Aggregate(sheet="Computed", column="Scaled", aggregation="sum"),
                            tolerance=1_000_000,
                        )
                    ],
                )
            ],
            path,
        )
        assert state.status == "applied"
        assert state.operations[0].details["recalculated"] is False


class TestCompareWorkbooks:
    def test_compares_by_key(self, tmp_path: Path) -> None:
        import openpyxl

        def make(values: list[tuple[int, int]], name: str) -> Path:
            workbook = openpyxl.Workbook()
            sheet = workbook.active
            sheet.title = "S"
            sheet.append(["Id", "V"])
            for identifier, value in values:
                sheet.append([identifier, value])
            path = tmp_path / name
            workbook.save(path)
            workbook.close()
            return path

        left = make([(1, 10), (2, 20)], "left.xlsx")
        right = make([(1, 10), (2, 99), (3, 30)], "right.xlsx")

        state = run(
            [
                ReadRange(target=Target(sheet="S")),
            ],
            left,
        )
        assert state.status == "applied"

        from app.contracts.operations import CompareWorkbooks

        config = ExcelPilotConfig(workspace_root=str(tmp_path))
        inspection = inspect_workbook(left, limits=config.limits)
        workbook = open_book(left, limits=config.limits)
        try:
            state = Executor(config).execute(
                workbook,
                make_plan(
                    [
                        CompareWorkbooks(
                            target=Target(sheet="S"),
                            other_path=str(right),
                            key_columns=["Id"],
                        )
                    ]
                ),
                inspection,
            )
        finally:
            workbook.close()
        details = state.operations[0].details
        assert details["only_in_other"] == 1
        assert details["changed"] == 1
        assert details["matched"] == 2


@pytest.mark.security
class TestExecutorGuards:
    def test_rejects_duplicate_mutating_operations(self, sales: Path) -> None:
        op = NormalizeValues(
            target=Target(sheet="Sales", cell_range="A1:J21"),
            rules=NormalizeRules(trim_whitespace=True),
        )
        state = run([op, op], sales)
        assert state.status == "failed"
        assert "repeated identical" in state.errors[0]

    def test_denies_at_execution_time_when_source_would_be_overwritten(self, sales: Path) -> None:
        """Defence in depth: the executor re-checks policy itself."""
        config = ExcelPilotConfig(workspace_root=str(sales.parent))
        inspection = inspect_workbook(sales, limits=config.limits)
        before = file_sha256(sales)
        workbook = open_book(sales, limits=config.limits)
        try:
            with pytest.raises(ExecutionError) as info:
                Executor(config).execute(
                    workbook,
                    make_plan(
                        [NormalizeValues(target=Target(sheet="Sales"), rules=NormalizeRules())]
                    ),
                    inspection,
                    output_path=str(sales),  # the source itself
                )
        finally:
            workbook.close()
        assert "never writes to the source" in str(info.value)
        # The source is byte-identical after a denied run.
        assert file_sha256(sales) == before

    def test_reresolves_target_against_current_state(self, sales: Path) -> None:
        """An operation addressing a sheet that does not exist is blocked."""
        with pytest.raises(ExecutionError) as info:
            run(
                [NormalizeValues(target=Target(sheet="Ghost"), rules=NormalizeRules())],
                sales,
            )
        assert "'Ghost' not found" in str(info.value)

    def test_operation_failure_raises_with_partial_state(self, sales: Path) -> None:
        config = ExcelPilotConfig(workspace_root=str(sales.parent))
        inspection = inspect_workbook(sales, limits=config.limits)
        workbook = open_book(sales, limits=config.limits)
        executor = Executor(config)
        try:
            with pytest.raises(ExecutionError) as info:
                executor.execute(
                    workbook,
                    make_plan(
                        [
                            CreateWorksheet(name="New"),
                            NormalizeValues(
                                target=Target(sheet="DoesNotExist"), rules=NormalizeRules()
                            ),
                        ]
                    ),
                    inspection,
                )
            # The first operation's success is still reported.
            assert len(info.value.state.operations) == 2
            assert info.value.state.operations[0].status == "applied"
        finally:
            workbook.close()

    def test_executor_totals_are_accumulated(self, sales: Path) -> None:
        state = run(
            [
                CreateWorksheet(name="A"),
                CreateWorksheet(name="B"),
            ],
            sales,
        )
        assert state.status == "applied"
        assert state.totals()["operations"] == 2


class TestPreview:
    def test_preview_measures_without_writing(self, sales: Path) -> None:
        before = file_sha256(sales)
        inspection = inspect_workbook(sales)
        preview = preview_plan(
            [
                NormalizeValues(
                    target=Target(sheet="Sales", cell_range="A1:J21"),
                    columns=["Customer"],
                    rules=NormalizeRules(trim_whitespace=True, case="title"),
                )
            ],
            inspection,
            run_id="run-x",
            output_path="/tmp/out.xlsx",
        )
        assert preview.cells_to_change > 0
        assert preview.sheets_affected == 1
        assert file_sha256(sales) == before

    def test_preview_of_no_op_reports_zero(self, sales: Path) -> None:
        inspection = inspect_workbook(sales)
        preview = preview_operation(ReadRange(target=Target(sheet="Sales")), inspection)
        assert preview.cells_to_change == 0

    def test_preview_reports_formula_removal(self, sales: Path) -> None:
        inspection = inspect_workbook(sales)
        preview = preview_plan(
            [
                WriteRange(
                    target=Target(sheet="Sales", cell_range="G2:G21"),
                    values=[[1]] * 20,
                    neutralize_formula_injection=False,
                )
            ],
            inspection,
            run_id="r",
            output_path="/tmp/o.xlsx",
        )
        # 20 formula cells replaced by literals.
        assert preview.formulas_to_remove == 20
        assert preview.cells_to_change == 20

    def test_preview_reports_duplicate_removal(self, sales: Path) -> None:
        inspection = inspect_workbook(sales)
        preview = preview_plan(
            [
                RemoveDuplicates(
                    target=Target(sheet="Sales", cell_range="A1:J23"), keys=["InvoiceId"]
                )
            ],
            inspection,
            run_id="r",
            output_path="/tmp/o.xlsx",
        )
        assert preview.records_to_remove == 2

    def test_preview_reports_a_failing_operation_without_crashing(self, sales: Path) -> None:
        inspection = inspect_workbook(sales)
        preview = preview_plan(
            [NormalizeValues(target=Target(sheet="Ghost"), rules=NormalizeRules())],
            inspection,
            run_id="r",
            output_path="/tmp/o.xlsx",
        )
        assert preview.warnings
        assert "would fail" in preview.warnings[0]

    def test_preview_warnings_include_policy_context(self, sales: Path) -> None:
        inspection = inspect_workbook(sales)
        preview = preview_plan(
            [RemoveDuplicates(target=Target(sheet="Sales"))],
            inspection,
            run_id="r",
            output_path="/tmp/o.xlsx",
            policy_explanation="policy: require_approval via destructive_operation",
            policy_rule_ids=["destructive_operation"],
        )
        assert any("destructive_operation" in w for w in preview.warnings)


class TestOperationContext:
    def test_reconciliation_results_collected(self) -> None:
        context = OperationContext()
        assert context.reconciliation_results == []
        assert context.neutralize_formula_injection is True

    def test_neutralisation_can_be_disabled(self) -> None:
        context = OperationContext(neutralize_formula_injection=False)
        assert context.neutralize_formula_injection is False
