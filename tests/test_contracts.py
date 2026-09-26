"""Contract layer tests.

Covers JSON round-tripping, rejection of unexpected fields, and the invariants
that make the trust boundary work — in particular that ``JevDecision`` is
structurally incapable of expressing a workbook mutation (ADR-0004).
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from app.contracts import (
    CONTRACT_VERSION,
    Anomaly,
    AnomalyKind,
    AnomalySeverity,
    AnomalySource,
    ApprovalRequest,
    ApprovalResult,
    ApprovalStatus,
    CreateWorksheet,
    DecisionContext,
    DecisionQuestion,
    DryRunPreview,
    ExcelPilotConfig,
    ExecutionPlan,
    ExecutionState,
    FilterCondition,
    FilterRows,
    JevDecision,
    JevDecisionSet,
    NormalizeRules,
    NormalizeValues,
    OperationResult,
    PolicyDecision,
    PolicyOutcome,
    PolicyRequest,
    ReadRange,
    ReconcileSpec,
    RemoveDuplicates,
    RenameWorksheet,
    RiskLevel,
    RunRecord,
    SetFormula,
    SheetMetadata,
    SortRange,
    Target,
    TaskUnderstanding,
    UntrustedText,
    VerificationResult,
    VerificationStatus,
    WorkbookInspection,
    WriteRange,
    column_index,
    column_letter,
    new_run_id,
    parse_a1,
    utc_now,
)
from app.contracts.workbook import _parse_a1_range


@pytest.mark.security
class TestUntrustedText:
    def test_truncates_at_cap(self) -> None:
        text = UntrustedText("x" * 5_000, provenance="workbook_cell", max_chars=100)
        assert len(text) == 100
        assert text.truncated is True

    def test_preserves_short_text(self) -> None:
        text = UntrustedText("hello", provenance="workbook_cell", max_chars=100)
        assert text.text == "hello"
        assert text.truncated is False

    def test_not_equal_to_plain_string(self) -> None:
        """The whole point: untrusted content is not a str."""
        text = UntrustedText("hello", provenance="workbook_cell")
        assert text != "hello"
        assert text != UntrustedText("hello", provenance="sheet_name")

    def test_redacted_preview_is_bounded(self) -> None:
        text = UntrustedText("a" * 10_000, provenance="workbook_cell")
        assert len(text.to_redacted(limit=50)) < 120
        assert "workbook_cell" in text.to_redacted()


class TestCellReferences:
    @pytest.mark.parametrize(
        ("index", "letters"),
        [
            (1, "A"),
            (26, "Z"),
            (27, "AA"),
            (52, "AZ"),
            (53, "BA"),
            (702, "ZZ"),
            (703, "AAA"),
            (16384, "XFD"),
        ],
    )
    def test_column_letter_roundtrip(self, index: int, letters: str) -> None:
        assert column_letter(index) == letters
        assert column_index(letters) == index

    def test_parse_a1(self) -> None:
        assert parse_a1("A1") == (1, 1)
        assert parse_a1("$B$7") == (7, 2)
        assert parse_a1("AA10") == (10, 27)

    @pytest.mark.parametrize("bad", ["", "1A", "A", "abc", "A0", "$$"])
    def test_parse_a1_rejects_garbage(self, bad: str) -> None:
        with pytest.raises(ValueError):
            parse_a1(bad)

    def test_parse_range(self) -> None:
        assert _parse_a1_range("A1:F100") == (1, 1, 6, 100)
        assert _parse_a1_range("B2") == (2, 2, 2, 2)


class TestSheetMetadata:
    @pytest.fixture
    def sheet(self) -> SheetMetadata:
        return SheetMetadata(
            name="Sales",
            index=0,
            max_row=101,
            max_column=6,
            dimensions="A1:F101",
            non_empty_cells=600,
            formula_count=100,
            header_row=["Date", "Customer", "Region", "Product", "Amount", "InvoiceId"],
        )

    def test_column_index_exact(self, sheet: SheetMetadata) -> None:
        assert sheet.column_index("Customer") == 2
        assert sheet.column_index("customer") == 2
        assert sheet.column_index("Nope") is None

    def test_find_header_is_tolerant(self, sheet: SheetMetadata) -> None:
        """Real workbooks vary in naming; the planner needs to cope."""
        assert sheet.find_header("Amount", "Total") == 5
        assert sheet.find_header("InvoiceId", "Invoice ID") == 6

    def test_visibility_and_protection(self) -> None:
        hidden = SheetMetadata(
            name="X", index=1, state="veryHidden", max_row=0, max_column=0, dimensions="A1"
        )
        assert hidden.is_visible is False
        assert hidden.is_protected is True
        assert SheetMetadata(name="Y", state="hidden").is_visible is False
        assert SheetMetadata(name="Z", state="hidden").is_protected is False


class TestOperationContracts:
    def test_extra_field_is_rejected(self) -> None:
        """A hallucinated field must fail loudly, not be ignored."""
        with pytest.raises(ValidationError) as info:
            NormalizeValues.model_validate(
                {
                    "operation": "normalize_values",
                    "target": {"sheet": "Sales"},
                    "rules": {"trim_whitespace": True},
                    "colours": ["red"],
                }
            )
        assert "colours" in str(info.value)

    def test_write_range_rejects_ragged_rows(self) -> None:
        with pytest.raises(ValidationError, match="rectangular"):
            WriteRange(
                target=Target(sheet="S"),
                values=[[1, 2], [3]],
            )

    def test_set_formula_requires_equals(self) -> None:
        with pytest.raises(ValidationError, match="must start with"):
            SetFormula(target=Target(sheet="S"), formulas=["SUM(A1:A2)"])

    def test_sheet_name_rejects_excel_forbidden_chars(self) -> None:
        for bad in ["a/b", "a:b", "a*b", "a?b", "a[b", "a]b"]:
            with pytest.raises(ValidationError):
                CreateWorksheet(name=bad)

    def test_sheet_name_length_limit(self) -> None:
        with pytest.raises(ValidationError):
            CreateWorksheet(name="x" * 32)

    def test_rename_same_sheet_rejected(self) -> None:
        with pytest.raises(ValidationError, match="same sheet"):
            RenameWorksheet(from_name="Sales", to_name="sales")

    def test_sort_descending_length_must_match(self) -> None:
        with pytest.raises(ValidationError, match="same length"):
            SortRange(target=Target(sheet="S"), by_columns=["A", "B"], descending=[True])

    def test_filter_condition_value_requirements(self) -> None:
        with pytest.raises(ValidationError, match="requires a value"):
            FilterCondition(column="A", operator="equals")
        # empty checks legitimately need no value
        assert FilterCondition(column="A", operator="is_empty").value is None

    def test_normalize_rules_accepts_only_enumerated_values(self) -> None:
        with pytest.raises(ValidationError):
            NormalizeRules(case="sentence")  # type: ignore[arg-type]

    def test_remove_duplicates_defaults_to_whole_row(self) -> None:
        op = RemoveDuplicates(target=Target(sheet="S"))
        assert op.keys == []
        assert op.keep == "first"


class TestExecutionPlan:
    def _plan(self, operations: list[object] | None = None) -> ExecutionPlan:
        return ExecutionPlan(
            plan_id="plan-1",
            run_id="run-1",
            understanding=TaskUnderstanding(
                raw_task=UntrustedText("normalise names", provenance="user_task"),
                intent_summary="Normalise the customer column",
                interpretation="sufficiently_clear",
            ),
            operations=operations
            or [
                NormalizeValues(
                    target=Target(sheet="Sales"), rules=NormalizeRules(trim_whitespace=True)
                )
            ],
            source_hash="abc123",
        )

    def test_operation_kinds(self) -> None:
        plan = self._plan(
            [CreateWorksheet(name="Summary"), ReadRange(target=Target(sheet="Sales"))]
        )
        assert plan.operation_kinds == ["create_worksheet", "read_range"]
        assert plan.is_mutating is True

    def test_read_only_plan_is_not_mutating(self) -> None:
        plan = self._plan([ReadRange(target=Target(sheet="Sales"))])
        assert plan.is_mutating is False

    def test_detects_duplicate_mutating_operations(self) -> None:
        """The executor refuses these; the plan must be able to name them."""
        dup = NormalizeValues(
            target=Target(sheet="Sales"), rules=NormalizeRules(trim_whitespace=True)
        )
        plan = self._plan([dup, dup, ReadRange(target=Target(sheet="Sales"))])
        assert len(plan.duplicate_operations()) == 1

    def test_ambiguous_understanding_must_explain_itself(self) -> None:
        with pytest.raises(ValidationError, match="missing"):
            TaskUnderstanding(
                raw_task=UntrustedText("fix it", provenance="user_task"),
                intent_summary="do something",
                interpretation="ambiguous",
                missing_information=[],
            )

    def test_json_roundtrip(self) -> None:
        plan = self._plan()
        restored = ExecutionPlan.model_validate_json(plan.model_dump_json())
        assert restored.plan_id == plan.plan_id
        assert restored.operations[0].operation.value == "normalize_values"


class TestJevContracts:
    def test_jev_decision_has_no_mutation_field(self) -> None:
        """The structural guarantee behind 'JEV must not mutate workbooks'.

        If a field capable of expressing a workbook change were ever added to
        JevDecision, this test fails and the ADR has to be revisited deliberately.
        """
        assert set(JevDecision.model_fields) == {
            "question",
            "value",
            "status",
            "probability",
            "margin",
            "confidence",
        }

    def test_jev_decision_rejects_extra_fields(self) -> None:
        with pytest.raises(ValidationError):
            JevDecision.model_validate(
                {
                    "question": "automation",
                    "value": "yes",
                    "status": "selected",
                    "operation": "delete_all_sheets",
                }
            )

    def test_jev_cannot_ever_escalate_downward(self) -> None:
        """JEV can only raise scrutiny. There is no de-escalation path."""
        confident_yes = JevDecisionSet(
            decisions=[
                JevDecision(
                    question="automation",
                    value="yes",
                    status="selected",
                    probability=0.99,
                    margin=0.9,
                ),
                JevDecision(
                    question="risk", value="low", status="selected", probability=0.99, margin=0.9
                ),
            ],
            jev_called=True,
        )
        assert confident_yes.escalates is False

        high_risk = JevDecisionSet(
            decisions=[
                JevDecision(question="risk", value="high", status="selected", probability=0.99)
            ],
            jev_called=True,
        )
        assert high_risk.escalates is True

        needs_review = JevDecisionSet(
            decisions=[JevDecision(question="automation", value="yes", status="needs_review")],
            jev_called=True,
        )
        assert needs_review.escalates is True

    def test_disabled_jev_never_escalates(self) -> None:
        empty = JevDecisionSet(jev_called=False)
        assert empty.escalates is False
        assert empty.any_needs_review is False

    @pytest.mark.parametrize("bad_type", ["boolean", "ranking", "text"])
    def test_decision_question_rejects_unknown_type(self, bad_type: str) -> None:
        with pytest.raises(ValidationError):
            DecisionQuestion(id="q", type=bad_type, instructions="x", criteria={"a": "1", "b": "2"})

    def test_choice_question_requires_min_criteria(self) -> None:
        with pytest.raises(ValidationError, match="2-255"):
            DecisionQuestion(id="q", type="choice", instructions="x", criteria={"only": "1"})

    def test_score_question_requires_list(self) -> None:
        with pytest.raises(ValidationError, match="list"):
            DecisionQuestion(id="q", type="score", instructions="x", criteria={"a": "1", "b": "2"})

    def test_decision_context_bounds(self) -> None:
        ctx = DecisionContext(run_id="run-1", task_summary="clean", cells_to_change=100)
        assert ctx.cells_to_change == 100
        with pytest.raises(ValidationError):
            DecisionContext(run_id="run-1", task_summary="x", cells_to_change=-1)


class TestPolicyContracts:
    def test_outcome_helpers(self) -> None:
        assert PolicyDecision(outcome=PolicyOutcome.DENY).denied is True
        assert PolicyDecision(outcome=PolicyOutcome.ALLOW).allowed is True
        assert PolicyDecision(outcome=PolicyOutcome.REQUIRE_APPROVAL).requires_approval is True

    def test_explain_names_the_rules(self) -> None:
        decision = PolicyDecision(
            outcome=PolicyOutcome.REQUIRE_APPROVAL, rule_ids=["bulk_change", "formula_removal"]
        )
        assert "bulk_change" in decision.explain()

    def test_request_accepts_full_plan(self) -> None:
        request = PolicyRequest(
            operations=[NormalizeValues(target=Target(sheet="Sales"), rules=NormalizeRules())],
            sheet_names=["Sales"],
            cells_affected=50,
        )
        assert request.cells_affected == 50


class TestReconcileSpec:
    def test_requires_something_to_compare(self) -> None:
        with pytest.raises(ValidationError, match="compares nothing"):
            ReconcileSpec(
                name="grand total",
                actual={"sheet": "Sales", "column": "Amount", "aggregation": "sum"},
            )

    def test_tolerance_alone_is_sufficient(self) -> None:
        spec = ReconcileSpec(
            name="grand total",
            actual={"sheet": "Sales", "column": "Amount", "aggregation": "sum"},
            tolerance=0.01,
        )
        assert spec.tolerance == 0.01


class TestRunRecordAndEvents:
    def test_run_record_roundtrip(self) -> None:
        record = RunRecord(
            run_id=new_run_id(),
            created_at=utc_now().isoformat(),
            raw_task=UntrustedText("clean the data", provenance="user_task"),
            source_path="/tmp/a.xlsx",
            source_name="a.xlsx",
            source_hash="deadbeef",
        )
        restored = RunRecord.model_validate_json(record.model_dump_json())
        assert restored.run_id == record.run_id
        assert restored.raw_task.text == "clean the data"

    def test_operation_result_totals(self) -> None:
        state = ExecutionState(
            run_id="run-1",
            status="applied",
            operations=[
                OperationResult(operation="write_range", status="applied", cells_written=10)
            ],
            total_cells_written=10,
        )
        assert state.applied is True
        assert state.totals()["cells_written"] == 10

    def test_approval_result(self) -> None:
        result = ApprovalResult(status=ApprovalStatus.APPROVED, run_id="run-1", decided_at="now")
        assert result.approved is True

    def test_anomaly_records_its_source(self) -> None:
        anomaly = Anomaly(
            kind=AnomalyKind.DUPLICATE_SPIKE,
            severity=AnomalySeverity.WARNING,
            source=AnomalySource.DETERMINISTIC,
            message="duplicate rate rose from 0.01 to 0.19",
            detector="duplicate_rate_delta",
        )
        assert anomaly.to_summary()["source"] == "deterministic"

    def test_audit_event_is_immutable(self) -> None:
        from app.contracts import AuditEvent

        event = AuditEvent(
            seq=0, run_id="r", timestamp="t", actor="system", event_type="run.created"
        )
        with pytest.raises(ValidationError):
            event.seq = 1  # type: ignore[misc]


class TestVerificationContract:
    def test_static_is_the_default_and_is_the_weak_claim(self) -> None:
        """The default must be the honest, weaker claim.

        Not a claim that recalculation is impossible — it is available as the
        optional `recalc` extra. The point is that a run which did not evaluate
        anything must say so rather than defaulting to the stronger claim.
        """
        result = VerificationResult(run_id="run-1")
        assert result.recalculated is False
        assert result.static_formula_checks is True

    def test_evaluated_and_static_are_mutually_exclusive(self) -> None:
        """A consumer can never read a static result as an evaluated one.

        The two flags describe opposite things about the same result, and
        exactly one must be true. If both could be true, a static-only verdict
        would be presentable as though formulas had been evaluated.
        """
        evaluated = VerificationResult(run_id="r", recalculated=True, static_formula_checks=False)
        assert evaluated.recalculated is not evaluated.static_formula_checks

        static = VerificationResult(run_id="r")
        assert static.recalculated is not static.static_formula_checks

    def test_a_result_cannot_be_edited_after_construction(self) -> None:
        """Frozen, so a verifier cannot revise its own verdict after the fact."""
        result = VerificationResult(run_id="run-1")
        with pytest.raises(ValidationError):
            result.recalculated = True  # type: ignore[misc]

    def test_passed_reflects_status(self) -> None:
        assert VerificationResult(run_id="r", status=VerificationStatus.PASSED).passed is True
        assert VerificationResult(run_id="r", status=VerificationStatus.FAILED).passed is False


class TestConfigContract:
    def test_defaults_are_safe(self) -> None:
        config = ExcelPilotConfig()
        assert config.output.never_overwrite_source is True
        assert config.allow_paid_calls is False
        assert config.audit_enabled is True
        assert config.output.neutralize_formula_injection is True

    def test_fingerprint_is_stable_and_root_independent(self) -> None:
        a = ExcelPilotConfig(workspace_root="/tmp/one")
        b = ExcelPilotConfig(workspace_root="/tmp/two")
        assert a.fingerprint() == b.fingerprint()

    def test_fingerprint_changes_with_policy(self) -> None:
        a = ExcelPilotConfig()
        b = ExcelPilotConfig()
        b = b.model_copy(
            update={"policy": a.policy.model_copy(update={"cell_change_approval_threshold": 10})}
        )
        assert a.fingerprint() != b.fingerprint()

    def test_disabled_jev_provider_disables_jev(self) -> None:
        config = ExcelPilotConfig.model_validate({"jev": {"provider": "disabled"}})
        assert config.jev.enabled is False

    def test_unknown_config_key_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ExcelPilotConfig.model_validate({"nonsense_key": 1})


class TestRunId:
    def test_format_and_uniqueness(self) -> None:
        first, second = new_run_id(), new_run_id()
        assert first.startswith("run-")
        assert first != second
        assert len(first) == len("run-") + 16

    def test_filesystem_safe(self) -> None:
        run_id = new_run_id()
        assert all(char.isalnum() or char == "-" for char in run_id)


class TestInspectionContract:
    def test_sheet_lookup_is_case_insensitive(self) -> None:
        inspection = WorkbookInspection(
            path="/tmp/a.xlsx",
            file_name="a.xlsx",
            file_size_bytes=100,
            content_hash="h",
            extension="xlsx",
            sheet_names=["Sales"],
            sheets=[
                SheetMetadata(name="Sales", index=0, max_row=10, max_column=2, dimensions="A1:B10")
            ],
        )
        assert inspection.sheet("sales") is not None
        assert inspection.sheet("SALES") is not None
        assert inspection.sheet("missing") is None
        assert inspection.summary()["sheets"][0]["name"] == "Sales"


class TestDryRunPreview:
    def test_holds_measured_numbers(self) -> None:
        preview = DryRunPreview(
            run_id="run-1",
            workbook_name="a.xlsx",
            source_path="/tmp/a.xlsx",
            proposed_output_path="/tmp/a__run-1.xlsx",
            cells_to_change=23_418,
            sheets_affected=3,
            formulas_to_add=14,
            risk=RiskLevel.MEDIUM,
        )
        payload = json.loads(preview.model_dump_json())
        assert payload["cells_to_change"] == 23_418
        assert payload["risk"] == "medium"


class TestApprovalRequest:
    def test_carries_everything_an_operator_needs(self) -> None:
        request = ApprovalRequest(
            run_id="run-1",
            workbook_name="a.xlsx",
            source_path="/tmp/a.xlsx",
            proposed_output_path="/tmp/out.xlsx",
            intent_summary="clean the data",
            operation_kinds=["remove_duplicates"],
            cells_to_change=100,
            risk=RiskLevel.MEDIUM,
            policy_explanation="policy: require_approval via bulk_change",
            verification_plan=["structural_check", "reconciliation"],
            created_at="now",
        )
        assert request.verification_plan
        assert request.risk is RiskLevel.MEDIUM


class TestFilterRowsOperation:
    def test_output_sheet_mode_is_non_destructive_by_default(self) -> None:
        op = FilterRows(
            target=Target(sheet="Sales"),
            conditions=[FilterCondition(column="Region", operator="equals", value="North")],
        )
        assert op.hide_non_matching is True
        assert op.output_sheet is None


def test_contract_version_exported() -> None:
    assert CONTRACT_VERSION == "1"
