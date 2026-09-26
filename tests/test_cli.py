"""CLI tests.

Exit codes are the contract a script depends on, so they are asserted directly:
an earlier version of ``main()`` swallowed every failure and exited 0, which a
user would never notice and a script would silently misread.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from fixtures.workbooks import build

from app.cli.exit_codes import Exit, for_result, for_run
from app.contracts.enums import ApprovalStatus, RunOutcome

REPO_ROOT = Path(__file__).resolve().parent.parent


def run_cli(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    """Invoke the CLI as a subprocess, the way a user or a script would.

    A subprocess rather than an in-process call, because the console-script
    wrapper's ``sys.exit(main())`` is part of what is being tested.
    """
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-m", "app.cli.main", *args],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
        cwd=str(cwd) if cwd else None,
        env={**_clean_env(), "PYTHONPATH": str(REPO_ROOT)},
    )


def _clean_env() -> dict[str, str]:
    import os

    return {
        key: value
        for key, value in os.environ.items()
        if "API_KEY" not in key and "TOKEN" not in key and "SECRET" not in key
    }


@pytest.fixture
def workbook(tmp_path: Path) -> Path:
    return build("monthly_sales", tmp_path / "monthly_sales.xlsx", rows=25)


class TestInspect:
    def test_reports_structure(self, workbook: Path, tmp_path: Path) -> None:
        result = run_cli("inspect", str(workbook), cwd=tmp_path)
        assert result.returncode == Exit.SUCCESS
        assert "monthly_sales.xlsx" in result.stdout
        assert "Sales" in result.stdout
        assert "_Lookup" in result.stdout

    def test_json_is_parseable(self, workbook: Path, tmp_path: Path) -> None:
        result = run_cli("inspect", str(workbook), "--json", cwd=tmp_path)
        assert result.returncode == Exit.SUCCESS
        payload = json.loads(result.stdout)
        assert payload["contract_version"] == "1"
        assert payload["command"] == "inspect"
        assert payload["workbook"]["sheets"]

    def test_missing_file_is_a_usage_error(self, tmp_path: Path) -> None:
        result = run_cli("inspect", "does-not-exist.xlsx", cwd=tmp_path)
        assert result.returncode == Exit.USAGE

    def test_does_not_modify_the_workbook(self, workbook: Path, tmp_path: Path) -> None:
        from app.workbook import file_sha256

        before = file_sha256(workbook)
        run_cli("inspect", str(workbook), cwd=tmp_path)
        assert file_sha256(workbook) == before

    def test_rejects_legacy_xls(self, tmp_path: Path) -> None:
        legacy = tmp_path / "old.xls"
        legacy.write_bytes(b"not really xls")
        result = run_cli("inspect", str(legacy), cwd=tmp_path)
        assert result.returncode != Exit.SUCCESS
        assert ".xls" in (result.stdout + result.stderr)


class TestPlan:
    def test_plans_without_writing(self, workbook: Path, tmp_path: Path) -> None:
        from app.workbook import file_sha256

        before = file_sha256(workbook)
        result = run_cli("plan", str(workbook), "-t", "normalise the Customer column", cwd=tmp_path)
        assert result.returncode == Exit.SUCCESS
        assert "normalize_values" in result.stdout
        assert file_sha256(workbook) == before

    def test_json_includes_decisions_and_policy(self, workbook: Path, tmp_path: Path) -> None:
        result = run_cli(
            "plan", str(workbook), "-t", "normalise the Customer column", "--json", cwd=tmp_path
        )
        payload = json.loads(result.stdout)
        assert payload["command"] == "plan"
        assert payload["plan"]["operations"] == ["normalize_values"]
        assert payload["jev"]["called"] is True
        assert payload["policy"]["outcome"] in {"allow", "require_approval"}

    def test_refuses_an_unsupported_request(self, workbook: Path, tmp_path: Path) -> None:
        result = run_cli(
            "plan", str(workbook), "-t", "delete all the sheets", "--json", cwd=tmp_path
        )
        payload = json.loads(result.stdout)
        assert payload["plan"]["interpretation"] == "requires_user_input"
        assert result.returncode == Exit.POLICY_DENIED

    def test_states_that_jev_is_advisory(self, workbook: Path, tmp_path: Path) -> None:
        result = run_cli("plan", str(workbook), "-t", "normalise the Customer column", cwd=tmp_path)
        # Diagnostics go to stderr, which is what keeps `--json | jq` reliable.
        assert "advisory" in result.stdout + result.stderr


class TestRun:
    def test_dry_run_writes_nothing(self, workbook: Path, tmp_path: Path) -> None:
        result = run_cli(
            "run",
            str(workbook),
            "-t",
            "normalise the Customer column",
            "--dry-run",
            cwd=tmp_path,
        )
        assert result.returncode == Exit.SUCCESS
        outputs = list(tmp_path.glob("monthly_sales__run-*.xlsx"))
        assert outputs == [], "a dry run must not write an output workbook"

    def test_requires_approval_and_does_not_default_to_it(
        self, workbook: Path, tmp_path: Path
    ) -> None:
        """The safe default: no approval given means no execution."""
        result = run_cli("run", str(workbook), "-t", "remove duplicate invoices", cwd=tmp_path)
        assert result.returncode == Exit.APPROVAL_REQUIRED
        assert list(tmp_path.glob("monthly_sales__run-*.xlsx")) == []

    def test_rejection_is_recorded(self, workbook: Path, tmp_path: Path) -> None:
        result = run_cli(
            "run", str(workbook), "-t", "remove duplicate invoices", "--reject", cwd=tmp_path
        )
        assert result.returncode == Exit.APPROVAL_REQUIRED
        assert list(tmp_path.glob("monthly_sales__run-*.xlsx")) == []

    def test_approved_run_writes_a_versioned_output_and_keeps_the_source(
        self, workbook: Path, tmp_path: Path
    ) -> None:
        from app.workbook import file_sha256

        before = file_sha256(workbook)
        result = run_cli(
            "run",
            str(workbook),
            "-t",
            "normalise the Customer column",
            "--approve",
            cwd=tmp_path,
        )
        assert result.returncode == Exit.SUCCESS, result.stdout + result.stderr
        outputs = list(tmp_path.glob("monthly_sales__run-*.xlsx"))
        assert len(outputs) == 1
        assert file_sha256(workbook) == before, "the source must be untouched"

    def test_policy_denial_exits_three(self, workbook: Path, tmp_path: Path) -> None:
        result = run_cli(
            "run", str(workbook), "-t", "delete all the sheets", "--approve", cwd=tmp_path
        )
        assert result.returncode == Exit.POLICY_DENIED

    def test_approve_and_reject_are_mutually_exclusive(
        self, workbook: Path, tmp_path: Path
    ) -> None:
        result = run_cli(
            "run",
            str(workbook),
            "-t",
            "remove duplicate invoices",
            "--approve",
            "--reject",
            cwd=tmp_path,
        )
        assert result.returncode == Exit.USAGE

    def test_json_run_document(self, workbook: Path, tmp_path: Path) -> None:
        result = run_cli(
            "run",
            str(workbook),
            "-t",
            "normalise the Customer column",
            "--approve",
            "--json",
            cwd=tmp_path,
        )
        payload = json.loads(result.stdout)
        assert payload["command"] == "run"
        assert payload["outcome"] == "succeeded"
        assert isinstance(payload["verification"]["recalculated"], bool)
        assert payload["source"]["hash"]
        assert payload["output"]["hash"]

    def test_audit_trail_is_written(self, workbook: Path, tmp_path: Path) -> None:
        result = run_cli(
            "run",
            str(workbook),
            "-t",
            "normalise the Customer column",
            "--approve",
            "--json",
            cwd=tmp_path,
        )
        run_id = json.loads(result.stdout)["run_id"]
        audit = tmp_path / ".excelpilot" / "runs" / run_id / "audit.jsonl"
        assert audit.exists()
        events = [json.loads(line) for line in audit.read_text().splitlines() if line.strip()]
        types = {event["event_type"] for event in events}
        assert "run.created" in types
        assert "workbook.inspected" in types
        assert "source.snapshot" in types
        assert "policy.evaluated" in types
        assert "run.completed" in types

    def test_paid_calls_are_blocked_by_default(self, workbook: Path, tmp_path: Path) -> None:
        """--allow-paid-calls is a gate, not a warning."""
        result = run_cli(
            "run",
            str(workbook),
            "-t",
            "normalise the Customer column",
            "--allow-paid-calls",
            "--approve",
            cwd=tmp_path,
        )
        # No credential is configured in the test environment, so the adapter
        # degrades rather than calling out. The point is that nothing was called
        # without authorisation and the run is still honest about it.
        assert result.returncode in {Exit.SUCCESS, Exit.APPROVAL_REQUIRED}
        assert "OPENROUTER" not in result.stdout


class TestDiff:
    def test_reports_no_change_for_an_identical_copy(self, workbook: Path, tmp_path: Path) -> None:
        import shutil

        copy = tmp_path / "copy.xlsx"
        shutil.copy2(workbook, copy)
        result = run_cli("diff", str(workbook), str(copy), "--json", cwd=tmp_path)
        assert result.returncode == Exit.SUCCESS
        payload = json.loads(result.stdout)
        assert payload["summary"]["cells_changed"] == 0
        assert payload["structural_change"] is False

    def test_detects_a_real_change(self, workbook: Path, tmp_path: Path) -> None:
        import openpyxl

        from app.workbook import save_atomic

        changed = tmp_path / "changed.xlsx"
        book = openpyxl.load_workbook(workbook)
        book["Sales"]["B2"] = "CHANGED"
        save_atomic(book, changed)
        book.close()

        result = run_cli("diff", str(workbook), str(changed), "--json", cwd=tmp_path)
        payload = json.loads(result.stdout)
        assert payload["summary"]["cells_changed"] == 1

    def test_json_is_stable(self, workbook: Path, tmp_path: Path) -> None:
        result = run_cli("diff", str(workbook), str(workbook), "--json", cwd=tmp_path)
        payload = json.loads(result.stdout)
        assert payload["command"] == "diff"
        assert "notes" in payload


class TestVerify:
    def test_passes_for_an_untouched_workbook(self, workbook: Path, tmp_path: Path) -> None:
        from app.verification.recalc import library_available

        result = run_cli("verify", str(workbook), "--json", cwd=tmp_path)
        assert result.returncode == Exit.SUCCESS
        payload = json.loads(result.stdout)
        assert payload["passed"] is True
        # Asserted against the real capability rather than a hard-coded False,
        # which is what let the verify-command bug go unnoticed.
        assert payload["recalculated"] is library_available()

    def test_honours_the_configured_recalculation_setting(
        self, workbook: Path, tmp_path: Path
    ) -> None:
        """``verify`` must be no weaker than the run it is checking.

        Found by running the documented commands: ``excelpilot verify`` reported
        ``recalculated: false`` on a workbook that the run itself had just
        recalculated successfully, because the command built a bare ``Verifier``
        and ignored ``verification.enable_recalculation``. A standalone check
        that under-reports is worse than one that does not run — it tells the
        operator the deployment cannot do something it can.
        """
        from app.verification.recalc import library_available

        result = run_cli("verify", str(workbook), "--json", cwd=tmp_path)
        payload = json.loads(result.stdout)
        assert payload["recalculated"] is library_available(), (
            "verify ignored the configured recalculation setting"
        )
        assert payload["static_formula_checks"] is (not library_available())

    def test_accepts_a_config_file_like_every_other_command(
        self, workbook: Path, tmp_path: Path
    ) -> None:
        """``verify`` was the only command without ``--config``."""
        config = tmp_path / "ep.toml"
        config.write_text("[verification]\nenable_recalculation = false\n", encoding="utf-8")
        result = run_cli("verify", str(workbook), "--config", str(config), "--json", cwd=tmp_path)
        assert result.returncode == Exit.SUCCESS
        assert json.loads(result.stdout)["recalculated"] is False

    def test_fails_for_an_unreadable_file(self, workbook: Path, tmp_path: Path) -> None:
        broken = tmp_path / "broken.xlsx"
        broken.write_bytes(b"this is not a workbook")
        result = run_cli("verify", str(broken), "--json", cwd=tmp_path)
        assert result.returncode == Exit.VERIFICATION_FAILED
        payload = json.loads(result.stdout)
        assert payload["passed"] is False

    def test_detects_a_removed_sheet_against_a_run_snapshot(
        self, workbook: Path, tmp_path: Path
    ) -> None:
        """A removed sheet is only detectable *against a reference*.

        With no reference, verification has nothing to compare against and a
        workbook missing a sheet looks perfectly valid. That is a real limit of
        standalone verification, stated here rather than papered over.
        """
        import openpyxl

        from app.workbook import save_atomic

        run = run_cli(
            "run",
            str(workbook),
            "-t",
            "normalise the Customer column",
            "--approve",
            "--json",
            cwd=tmp_path,
        )
        run_id = json.loads(run.stdout)["run_id"]

        damaged = tmp_path / "damaged.xlsx"
        book = openpyxl.load_workbook(workbook)
        del book["_Lookup"]
        save_atomic(book, damaged)
        book.close()

        # Against nothing: the check cannot see the loss.
        standalone = run_cli("verify", str(damaged), "--json", cwd=tmp_path)
        assert json.loads(standalone.stdout)["passed"] is True

        # Against the run's snapshot: it can.
        against_run = run_cli("verify", str(damaged), "--run", run_id, "--json", cwd=tmp_path)
        payload = json.loads(against_run.stdout)
        assert payload["passed"] is False
        assert against_run.returncode == Exit.VERIFICATION_FAILED
        failed = {check["name"] for check in payload["checks"] if check["status"] == "failed"}
        assert "structure_no_sheet_removed" in failed

    def test_unknown_run_is_not_found(self, workbook: Path, tmp_path: Path) -> None:
        result = run_cli("verify", str(workbook), "--run", "run-nope", cwd=tmp_path)
        assert result.returncode == Exit.NOT_FOUND


class TestReplay:
    def test_replay_is_read_only(self, workbook: Path, tmp_path: Path) -> None:

        from app.workbook import file_sha256

        run = run_cli(
            "run",
            str(workbook),
            "-t",
            "normalise the Customer column",
            "--approve",
            "--json",
            cwd=tmp_path,
        )
        run_id = json.loads(run.stdout)["run_id"]
        output = next(tmp_path.glob("monthly_sales__run-*.xlsx"))
        before = file_sha256(output)

        result = run_cli("replay", run_id, "--json", cwd=tmp_path)
        assert result.returncode == Exit.SUCCESS
        payload = json.loads(result.stdout)
        assert payload["run"]["run_id"] == run_id
        assert payload["audit_events"]
        assert file_sha256(output) == before, "replay must not modify anything"

    def test_replay_of_an_unknown_run(self, tmp_path: Path) -> None:
        result = run_cli("replay", "run-nope", cwd=tmp_path)
        assert result.returncode == Exit.NOT_FOUND


class TestInformationalCommands:
    def test_policy_lists_rules(self, tmp_path: Path) -> None:
        result = run_cli("policy", "--json", cwd=tmp_path)
        payload = json.loads(result.stdout)
        assert "source_never_overwritten" in payload["hard_deny_rules"]
        assert payload["thresholds"]

    def test_policy_states_the_jev_relationship(self, tmp_path: Path) -> None:
        result = run_cli("policy", cwd=tmp_path)
        # Notes are diagnostics and go to stderr, keeping stdout clean for --json.
        assert "never lower" in result.stdout + result.stderr

    def test_runs_lists_history(self, workbook: Path, tmp_path: Path) -> None:
        run_cli(
            "run", str(workbook), "-t", "normalise the Customer column", "--approve", cwd=tmp_path
        )
        result = run_cli("runs", "--json", cwd=tmp_path)
        payload = json.loads(result.stdout)
        assert len(payload["runs"]) == 1

    def test_runs_is_empty_initially(self, tmp_path: Path) -> None:
        result = run_cli("runs", "--json", cwd=tmp_path)
        assert json.loads(result.stdout)["runs"] == []

    def test_version(self, tmp_path: Path) -> None:
        result = run_cli("version", cwd=tmp_path)
        assert result.returncode == Exit.SUCCESS
        assert "excelpilot" in result.stdout

    def test_help(self, tmp_path: Path) -> None:
        result = run_cli("--help", cwd=tmp_path)
        assert result.returncode == Exit.SUCCESS
        assert "inspect" in result.stdout


class TestExitCodeMapping:
    """Unit-level checks on the mapping the subprocess tests exercise."""

    def test_succeeded_is_zero(self) -> None:
        assert for_run(RunOutcome.SUCCEEDED) == Exit.SUCCESS

    def test_dry_run_is_zero(self) -> None:
        assert for_run(RunOutcome.DRY_RUN) == Exit.SUCCESS

    def test_policy_denial_is_three(self) -> None:
        assert for_run(RunOutcome.REJECTED_BY_POLICY) == Exit.POLICY_DENIED

    def test_verification_failure_is_five_not_one(self) -> None:
        """The load-bearing distinction: written but unverified is not success."""
        assert for_run(RunOutcome.FAILED, verification_status="failed") == Exit.VERIFICATION_FAILED

    def test_plain_failure_is_one(self) -> None:
        assert for_run(RunOutcome.FAILED) == Exit.INTERNAL_ERROR

    def test_pending_approval_is_four(self) -> None:
        from app.app import RunResult
        from app.contracts.base import UntrustedText, utc_now
        from app.contracts.pipeline import RunRecord

        record = RunRecord(
            run_id="run-1",
            created_at=utc_now().isoformat(),
            raw_task=UntrustedText("x", provenance="user_task"),
            source_path="/tmp/a.xlsx",
            source_name="a.xlsx",
            source_hash="h",
        )
        result = RunResult(
            run_id="run-1",
            state=record.state,
            outcome=RunOutcome.FAILED,
            record=record,
            approval_request=object(),  # type: ignore[arg-type]
            approval=None,
        )
        assert for_result(result) == Exit.APPROVAL_REQUIRED

    def test_approved_approval_is_not_required_any_more(self) -> None:
        from app.app import RunResult
        from app.contracts.base import UntrustedText, utc_now
        from app.contracts.pipeline import ApprovalResult, RunRecord

        record = RunRecord(
            run_id="run-1",
            created_at=utc_now().isoformat(),
            raw_task=UntrustedText("x", provenance="user_task"),
            source_path="/tmp/a.xlsx",
            source_name="a.xlsx",
            source_hash="h",
            verification_status="passed",
        )
        result = RunResult(
            run_id="run-1",
            state=record.state,
            outcome=RunOutcome.SUCCEEDED,
            record=record,
            approval_request=object(),  # type: ignore[arg-type]
            approval=ApprovalResult(
                status=ApprovalStatus.APPROVED, run_id="run-1", decided_at=utc_now().isoformat()
            ),
        )
        assert for_result(result) == Exit.SUCCESS

    def test_a_missing_workbook_is_a_usage_error_not_not_found(self, tmp_path: Path) -> None:
        """Exit 7 means an unknown *run id*; a bad path argument is exit 2.

        Asserted because ``docs/cli.md`` documents this split, and the
        distinction is the kind of thing a well-meaning refactor would flatten:
        a script branching on 7 to mean "file missing" would silently stop
        working.
        """
        result = run_cli("inspect", str(tmp_path / "nope.xlsx"), cwd=tmp_path)
        assert result.returncode == Exit.USAGE
        assert result.returncode != Exit.NOT_FOUND

    def test_an_unreadable_workbook_is_also_a_usage_error(self, tmp_path: Path) -> None:
        broken = tmp_path / "broken.xlsx"
        broken.write_bytes(b"this is not a workbook")
        result = run_cli("inspect", str(broken), cwd=tmp_path)
        assert result.returncode == Exit.USAGE

    def test_a_policy_denial_at_execution_time_reports_one_but_records_the_rule(
        self, workbook: Path, tmp_path: Path
    ) -> None:
        """The two-exit-code split, and the attribution that makes it usable.

        The executor re-checks policy as defence in depth, so a denial there is
        a *failed run* (exit 1), not a planning-stage refusal (exit 3). A script
        that only reads the exit code cannot tell a security stop from an
        incidental failure — which is why the rule id has to be in the payload.

        The output path must be genuinely outside the workspace. ``run_cli`` runs
        with ``cwd=tmp_path``, so the workspace root *is* ``tmp_path``; a path
        under it would be legitimately allowed and the run would succeed.
        """
        outside = tmp_path.parent / "outside-the-workspace.xlsx"
        result = run_cli(
            "run",
            str(workbook),
            "-t",
            "normalise the Customer column",
            "--approve",
            "-o",
            str(outside),
            "--json",
            cwd=tmp_path,
        )
        payload = json.loads(result.stdout)
        assert result.returncode == Exit.INTERNAL_ERROR
        assert payload["policy"]["outcome"] == "deny"
        assert "output_within_workspace" in payload["policy"]["rule_ids"]
        assert payload["output"]["path"] is None
        assert not outside.exists(), "a denied run must not have written the file"


class TestNoUnsafeFlags:
    """The flags that would undermine the product must not exist."""

    @pytest.mark.parametrize(
        "flag", ["--force", "--no-verify", "--skip-policy", "--overwrite", "--in-place"]
    )
    def test_does_not_exist(self, flag: str, tmp_path: Path) -> None:
        result = run_cli("run", "--help", cwd=tmp_path)
        assert result.returncode == Exit.SUCCESS
        assert flag not in result.stdout
