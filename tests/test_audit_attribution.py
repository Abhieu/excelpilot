"""Auditability of security denials.

Found during the release-hardening audit, by attempting to write a run's output
back onto its own source workbook with ``--approve`` and an explicit
``output_path`` equal to the source.

The control worked: the run failed and the source was untouched. But the
**rule id was lost**. ``run.json`` carried ``policy_outcome: require_approval``
and an empty ``policy_rule_ids``, and ``audit.jsonl`` carried the readable reason
with no rule attached. An auditor reading the trail could see that something
stopped the run and what it said, but not *which control* stopped it — so a hard
security denial was indistinguishable from an incidental failure.

The information existed the whole time: ``PolicyDenied`` carries ``rule_ids``.
It was dropped on the floor between the guard and the run record.

These tests pin the attribution. The safety properties themselves are covered
in ``tests/test_policy.py`` and ``tests/test_workbook.py``; what is new here is
that the refusal must be *nameable* afterwards.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fixtures.workbooks import build

from app.app import RunOrchestrator
from app.audit import AuditLog
from app.contracts.base import UntrustedText
from app.contracts.config import ExcelPilotConfig
from app.contracts.enums import PolicyOutcome
from app.decisions import MockJevAdapter
from app.storage import FileRunStore
from app.workbook import file_sha256


@pytest.fixture
def setup(tmp_path: Path) -> tuple[Path, ExcelPilotConfig]:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    source = build("monthly_sales", workspace / "sales.xlsx", rows=20)
    return source, ExcelPilotConfig(workspace_root=str(workspace))


def _attempt_source_overwrite(source: Path, config: ExcelPilotConfig):
    """Ask for the output to be the source, with approval granted."""
    return RunOrchestrator(config, jev=MockJevAdapter("approve")).run(
        source,
        UntrustedText("normalise the Customer column", provenance="user_task"),
        approve=True,
        output_path=source,
    )


@pytest.mark.security
class TestExecutionTimeDenialIsAttributable:
    """A guard denial must name the rule that fired, in the run record."""

    def test_the_source_is_still_protected(self, setup: tuple[Path, ExcelPilotConfig]) -> None:
        """The property that prompted the fix; assert it directly."""
        source, config = setup
        before = file_sha256(source)
        run = _attempt_source_overwrite(source, config)

        assert run.outcome.value == "failed"
        assert file_sha256(source) == before, "the source was modified"

    def test_the_run_record_names_the_rule(self, setup: tuple[Path, ExcelPilotConfig]) -> None:
        source, config = setup
        run = _attempt_source_overwrite(source, config)

        assert "source_never_overwritten" in run.record.policy_rule_ids, (
            "a hard-deny security refusal reached the run record with no rule id; "
            f"got {list(run.record.policy_rule_ids)}"
        )

    def test_the_record_outcome_reflects_the_denial(
        self, setup: tuple[Path, ExcelPilotConfig]
    ) -> None:
        """The run was not merely awaiting approval; it was denied.

        Recording ``require_approval`` next to a failure is misleading: it
        suggests the run stopped at a gate a human could pass, when in fact a
        non-configurable control refused it.
        """
        source, config = setup
        run = _attempt_source_overwrite(source, config)

        assert run.record.policy_outcome is not None
        assert run.record.policy_outcome == PolicyOutcome.DENY

    def test_the_audit_trail_names_the_rule(self, setup: tuple[Path, ExcelPilotConfig]) -> None:
        source, config = setup
        run = _attempt_source_overwrite(source, config)

        audit_file = FileRunStore(config).paths(run.run_id).audit_file
        with AuditLog(audit_file) as log:
            assert log.summary()["events"] > 0

        contents = audit_file.read_text(encoding="utf-8")
        assert "source_never_overwritten" in contents, (
            "the audit trail records that a run failed but not which control stopped it"
        )

    def test_the_failure_event_carries_the_rule(self, setup: tuple[Path, ExcelPilotConfig]) -> None:
        """Not merely present somewhere — attached to the failure itself."""
        source, config = setup
        run = _attempt_source_overwrite(source, config)

        audit_file = FileRunStore(config).paths(run.run_id).audit_file
        failures = [
            json.loads(line)
            for line in audit_file.read_text(encoding="utf-8").splitlines()
            if json.loads(line)["event_type"] == "execution.failed"
        ]
        assert failures, "no execution.failed event was written"
        assert "source_never_overwritten" in failures[0]["payload"].get("denied_by_rules", [])

    def test_the_serialised_record_carries_the_rule(
        self, setup: tuple[Path, ExcelPilotConfig]
    ) -> None:
        """``run.json`` on disk, not just the in-memory object."""
        source, config = setup
        run = _attempt_source_overwrite(source, config)

        stored = FileRunStore(config).paths(run.run_id).run_file
        payload = json.loads(stored.read_text(encoding="utf-8"))
        assert "source_never_overwritten" in payload["policy_rule_ids"]

    def test_the_rule_survives_replay(self, setup: tuple[Path, ExcelPilotConfig]) -> None:
        """Replay reads the stored record; the attribution must still be there.

        Replay is how an auditor answers "what happened six months ago", so a
        rule id that reached only the in-memory object but not the store would
        still be lost to them.
        """
        source, config = setup
        run = _attempt_source_overwrite(source, config)

        # The same call `excelpilot replay` makes.
        replayed = FileRunStore(config).read_run(run.run_id)
        assert "source_never_overwritten" in replayed.policy_rule_ids


@pytest.mark.security
class TestUnrelatedDenialsAreNotOverclaimed:
    """The fix must not attach rule ids that did not fire.

    A guard can also fail on target resolution rather than policy, and a
    non-security failure must not be dressed up as a named security refusal.
    """

    def test_a_plain_failure_reports_no_rules(self, setup: tuple[Path, ExcelPilotConfig]) -> None:
        source, config = setup
        run = RunOrchestrator(config, jev=MockJevAdapter("approve")).run(
            source,
            UntrustedText("normalise the Customer column", provenance="user_task"),
            approve=True,
            dry_run=True,
        )
        # A dry run is not a denial, so nothing should be invented.
        assert (
            run.record.policy_rule_ids == []
            or "source_never_overwritten" not in run.record.policy_rule_ids
        )

    def test_a_successful_run_records_no_denial(self, setup: tuple[Path, ExcelPilotConfig]) -> None:
        source, config = setup
        run = RunOrchestrator(config, jev=MockJevAdapter("approve")).run(
            source,
            UntrustedText("normalise the Customer column", provenance="user_task"),
            approve=True,
        )
        assert "source_never_overwritten" not in run.record.policy_rule_ids
