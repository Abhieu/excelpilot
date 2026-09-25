"""ExcelPilot command line.

The primary interface, and the one that must be scriptable. Every command
supports ``--json`` and emits a stable, versioned document on stdout; human
output goes to stdout and diagnostics to stderr (ADR-0008).

There is deliberately **no** ``--force``, ``--no-verify``, or ``--skip-policy``.
The whole product rests on verification being non-optional, so a flag that
disables it would contradict the reason the tool exists. There is also no way to
write to the source workbook.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Annotated, Any

import typer

from app import __version__
from app.app import RunOrchestrator
from app.audit import FileRunStore
from app.cli import output
from app.cli.exit_codes import Exit, for_result
from app.contracts.base import UntrustedText
from app.contracts.config import ExcelPilotConfig
from app.contracts.enums import RunOutcome
from app.contracts.errors import (
    ConfigError,
    ExcelPilotError,
    PaidCallBlocked,
    PolicyDenied,
    StorageError,
    WorkbookSecurityError,
)
from app.decisions import HttpJevAdapter, MockJevAdapter
from app.diff import diff_paths
from app.planner import build_planner
from app.policy import explain

app = typer.Typer(
    name="excelpilot",
    help=(
        "Controlled AI Excel operations engine. Inspects, plans, decides, "
        "authorises, executes, verifies, and audits workbook changes."
    ),
    add_completion=False,
    no_args_is_help=True,
    rich_markup_mode="rich",
)

#: Marker so a test can tell an interactive approval from a scripted one.
AUTO_APPROVAL_ENV = "EXCELPILOT_AUTO_APPROVE"


# --------------------------------------------------------------------------
# Shared options
# --------------------------------------------------------------------------


def _load_config(config_path: str | None, *, workspace: str | None = None) -> ExcelPilotConfig:
    """Load configuration, applying the workspace override.

    The workspace root is resolved through the path sandbox, so a relative
    ``--output`` cannot escape it.
    """
    if config_path:
        loaded = ExcelPilotConfig.load(config_path)
    else:
        loaded = ExcelPilotConfig()
    if workspace:
        loaded = loaded.model_copy(update={"workspace_root": workspace})
    return loaded


def _build_orchestrator(
    config: ExcelPilotConfig,
    *,
    allow_paid_calls: bool,
    jev_scenario: str = "approve",
    use_mock_jev: bool = True,
) -> RunOrchestrator:
    """Assemble an orchestrator with the requested adapters.

    JEV is mocked unless the operator both asks for real decisions and passes
    ``--allow-paid-calls``. A live call is a paid action, so the gate is in the
    code path rather than a warning.
    """
    effective = config.model_copy(
        update={"allow_paid_calls": allow_paid_calls or config.allow_paid_calls}
    )
    planner = build_planner(effective.model, allow_paid_calls=allow_paid_calls)

    jev: Any
    if use_mock_jev and not allow_paid_calls:
        jev = MockJevAdapter(jev_scenario)
    else:
        jev = HttpJevAdapter(effective.jev, allow_paid_calls=effective.allow_paid_calls)
    return RunOrchestrator(effective, planner=planner, jev=jev)


def _workbook(value: str) -> Path:
    path = Path(value).expanduser()
    if not path.exists():
        raise typer.BadParameter(f"workbook not found: {path}")
    return path


ConfigOption = Annotated[
    str | None, typer.Option("--config", "-c", help="Path to a TOML configuration file.")
]
WorkspaceOption = Annotated[
    str | None, typer.Option("--workspace", "-w", help="Workspace root; paths are confined to it.")
]
JsonOption = Annotated[
    bool, typer.Option("--json", help="Emit a versioned JSON document on stdout.")
]
VerboseOption = Annotated[bool, typer.Option("--verbose", "-v", help="More detail on stderr.")]


# --------------------------------------------------------------------------
# inspect
# --------------------------------------------------------------------------


@app.command()
def inspect(
    workbook: Annotated[str, typer.Argument(help="Path to the .xlsx or .xlsm file.")],
    config: ConfigOption = None,
    workspace: WorkspaceOption = None,
    as_json: JsonOption = False,
) -> None:
    """Inspect a workbook. Read-only: the file is never modified."""
    try:
        effective = _load_config(config, workspace=workspace)
        result = _build_orchestrator(effective, allow_paid_calls=False, use_mock_jev=True).inspect(
            _workbook(workbook)
        )
    except ExcelPilotError as error:
        output.error(str(error))
        raise typer.Exit(code=_code_for(error)) from error

    if as_json:
        output.emit_json({"command": "inspect", "workbook": result.summary()})
        return

    output.heading(f"Workbook: {result.file_name}")
    output.line()
    output.key_values(
        [
            ("path", result.path),
            ("size", f"{result.file_size_bytes:,} bytes"),
            ("sha256", result.content_hash[:32] + "..."),
            ("sheets", len(result.sheet_names)),
            ("hidden sheets", result.hidden_sheet_count),
            ("total rows", f"{result.total_rows:,}"),
            ("formulas", f"{result.total_formulas:,}"),
            ("non-empty cells", f"{result.total_non_empty_cells:,}"),
            ("tables", len(result.tables)),
            ("defined names", len(result.defined_names)),
            ("sensitivity", result.sensitivity.level),
            ("has macros", result.metadata.has_vba),
            ("external links", result.metadata.has_external_links),
        ]
    )
    output.line()
    output.table(
        "Sheets",
        ["#", "Name", "State", "Rows", "Cols", "Formulas", "Tables"],
        [
            [
                sheet.index,
                sheet.name,
                sheet.state,
                f"{sheet.max_row:,}",
                sheet.max_column,
                f"{sheet.formula_count:,}",
                ", ".join(sheet.table_names) or "-",
            ]
            for sheet in result.sheets
        ],
    )
    if result.tables:
        output.table(
            "Tables",
            ["Name", "Range", "Rows", "Columns"],
            [[t.name, t.ref, t.row_count, t.column_count] for t in result.tables],
        )
    if result.sensitivity.matched_signals:
        output.line()
        output.warn(f"sensitivity signals: {', '.join(result.sensitivity.matched_signals)}")


# --------------------------------------------------------------------------
# plan
# --------------------------------------------------------------------------


@app.command()
def plan(
    workbook: Annotated[str, typer.Argument(help="Path to the workbook.")],
    task: Annotated[str, typer.Option("--task", "-t", help="What to do, in plain language.")],
    config: ConfigOption = None,
    workspace: WorkspaceOption = None,
    as_json: JsonOption = False,
    verbose: VerboseOption = False,
) -> None:
    """Plan a change without executing it. Costs nothing and writes nothing."""
    try:
        effective = _load_config(config, workspace=workspace)
        orchestrator = _build_orchestrator(effective, allow_paid_calls=False)
        planned, inspection, jev, policy = orchestrator.plan(
            _workbook(workbook), UntrustedText(task, provenance="user_task")
        )
    except ExcelPilotError as error:
        output.error(str(error))
        raise typer.Exit(code=_code_for(error)) from error

    if as_json:
        output.emit_json(
            {
                "command": "plan",
                "workbook": inspection.summary(),
                "plan": {
                    "plan_id": planned.plan_id,
                    "intent_summary": planned.understanding.intent_summary,
                    "interpretation": planned.understanding.interpretation.value,
                    "missing_information": planned.understanding.missing_information,
                    "operations": planned.operation_kinds,
                    "notes": planned.notes,
                    "planner_source": planned.planner_source,
                },
                "jev": {
                    "called": bool(jev and jev.jev_called),
                    "provider": jev.provider.value if jev else None,
                    "decisions": [
                        d.model_dump(mode="json") for d in (jev.decisions if jev else [])
                    ],
                    "error": jev.error if jev else None,
                },
                "policy": {
                    "outcome": policy.outcome.value,
                    "rule_ids": policy.rule_ids,
                    "reasons": policy.reasons,
                    "jev_escalated": policy.jev_escalated,
                },
            }
        )
        raise typer.Exit(code=Exit.POLICY_DENIED if policy.denied else Exit.SUCCESS)

    output.heading("Plan")
    output.line()
    output.key_values(
        [
            ("intent", planned.understanding.intent_summary),
            ("interpretation", str(planned.understanding.interpretation)),
            ("planner", planned.planner_source),
            ("operations", ", ".join(planned.operation_kinds)),
        ]
    )
    if planned.understanding.missing_information:
        output.line()
        output.warn("this request is not specific enough to act on:")
        for item in planned.understanding.missing_information:
            output.field("", item)
    if planned.notes:
        output.line()
        for note in planned.notes:
            output.field("note", note)

    output.line()
    output.heading("JEV")
    if jev and jev.jev_called:
        output.table(
            "Decisions",
            ["Question", "Value", "Status", "Probability", "Margin"],
            [
                [
                    d.question,
                    d.value,
                    d.status,
                    f"{d.probability:.2f}" if d.probability is not None else "-",
                    f"{d.margin:.2f}" if d.margin is not None else "-",
                ]
                for d in jev.decisions
            ],
        )
        output.note("JEV is advisory. It can make a run more cautious; it can never authorise one.")
    else:
        output.field("status", jev.error if jev and jev.error else "not consulted")

    output.line()
    output.heading("Policy")
    output.field("outcome", str(policy.outcome))
    for reason in policy.reasons:
        output.field("", reason)
    if policy.rule_ids:
        output.field("rules", ", ".join(policy.rule_ids))

    if verbose:
        output.line()
        output.heading("Operations")
        for op in planned.operations:
            output.field(op.operation.value, op.model_dump(exclude={"operation"}))

    raise typer.Exit(code=Exit.POLICY_DENIED if policy.denied else Exit.SUCCESS)


# --------------------------------------------------------------------------
# run
# --------------------------------------------------------------------------


@app.command()
def run(
    workbook: Annotated[str, typer.Argument(help="Path to the workbook.")],
    task: Annotated[str, typer.Option("--task", "-t", help="What to do, in plain language.")],
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Measure the change without writing anything.")
    ] = False,
    output_path: Annotated[
        str | None,
        typer.Option("--output", "-o", help="Output path. Must be inside the workspace."),
    ] = None,
    config: ConfigOption = None,
    workspace: WorkspaceOption = None,
    run_id: Annotated[
        str | None, typer.Option("--run-id", help="Use a specific run id (for replay and testing).")
    ] = None,
    approve: Annotated[
        bool,
        typer.Option(
            "--approve",
            help="Approve a run that policy escalated. Review the dry run first.",
        ),
    ] = False,
    reject: Annotated[
        bool, typer.Option("--reject", help="Reject a run that policy escalated.")
    ] = False,
    allow_paid_calls: Annotated[
        bool,
        typer.Option(
            "--allow-paid-calls",
            help="Permit live JEV/LLM calls, which cost money. Off by default.",
        ),
    ] = False,
    jev_scenario: Annotated[
        str,
        typer.Option("--jev-scenario", help="Mock JEV scenario for offline runs."),
    ] = "approve",
    as_json: JsonOption = False,
    verbose: VerboseOption = False,
) -> None:
    """Run a change end to end: plan, decide, authorise, execute, verify, audit.

    The source workbook is never written to. Output is versioned and written
    atomically. A run that writes a file but fails verification exits 5, not 0.
    """
    if approve and reject:
        output.error("--approve and --reject are mutually exclusive")
        raise typer.Exit(code=Exit.USAGE)

    try:
        effective = _load_config(config, workspace=workspace)
        orchestrator = _build_orchestrator(
            effective,
            allow_paid_calls=allow_paid_calls,
            jev_scenario=jev_scenario,
            use_mock_jev=not allow_paid_calls,
        )
        source = _workbook(workbook)
        destination = Path(output_path).expanduser() if output_path else None
        if destination is not None:
            destination = destination.resolve()

        result = orchestrator.run(
            source,
            UntrustedText(task, provenance="user_task"),
            dry_run=dry_run,
            output_path=destination,
            run_id=run_id,
            approve=approve,
            reject=reject,
        )
    except PaidCallBlocked as error:
        output.error(str(error))
        output.note("Live calls cost money. Re-run with --allow-paid-calls to permit this one.")
        raise typer.Exit(code=Exit.PAID_CALL_BLOCKED) from error
    except ExcelPilotError as error:
        output.error(str(error))
        raise typer.Exit(code=_code_for(error)) from error

    if as_json:
        output.emit_json({"command": "run", **result.to_json_dict()})
        raise typer.Exit(code=for_result(result))

    _render_run(result, verbose=verbose, dry_run=dry_run)
    raise typer.Exit(code=for_result(result))


def _render_run(result: Any, *, verbose: bool, dry_run: bool) -> None:
    """Human-readable run report."""
    if dry_run:
        output.heading("DRY RUN")
        output.line()
        output.key_values(
            [
                ("run id", result.run_id),
                ("workbook", result.record.source_name),
                ("intent", result.record.intent_summary or "-"),
                ("operations", ", ".join(result.plan.operation_kinds) if result.plan else "-"),
                ("policy", str(result.policy.outcome) if result.policy else "-"),
                (
                    "approval required",
                    "YES" if result.policy and result.policy.requires_approval else "no",
                ),
            ]
        )
        output.line()
        output.note("nothing was written and nothing was changed")

    if result.approval_request is not None:
        request = result.approval_request
        output.line()
        output.heading("APPROVAL REQUIRED")
        output.key_values(
            [
                ("workbook", request.workbook_name),
                ("intent", request.intent_summary),
                ("operations", ", ".join(request.operation_kinds)),
                ("sheets", ", ".join(request.sheets_affected) or "-"),
                ("ranges", ", ".join(request.ranges_affected[:5]) or "-"),
                ("cells to change", f"{request.cells_to_change:,}"),
                ("formulas added", f"{request.formulas_to_add:,}"),
                ("formulas removed", f"{request.formulas_to_remove:,}"),
                ("records removed", f"{request.records_removed:,}"),
                ("structural change", "yes" if request.structural_change else "no"),
                ("risk", str(request.risk).upper()),
                ("JEV", request.jev_summary),
                ("policy", request.policy_explanation),
                ("proposed output", request.proposed_output_path),
                ("verification", ", ".join(request.verification_plan) or "-"),
            ]
        )
        for warning in request.warnings:
            output.warn(warning)
        output.line()
        if result.outcome is RunOutcome.FAILED:
            output.error(result.error or "the run stopped at the approval gate")
            output.note("review the dry run, then re-run with --approve")

    if result.execution is not None and not dry_run:
        output.line()
        output.heading("EXECUTED")
        output.key_values(
            [
                ("operations", result.execution.totals()["operations"]),
                ("cells written", f"{result.execution.total_cells_written:,}"),
                ("rows affected", f"{result.execution.total_rows_affected:,}"),
                ("formulas added", f"{result.execution.total_formulas_added:,}"),
                ("formulas removed", f"{result.execution.total_formulas_removed:,}"),
            ]
        )

    if result.output_path and not dry_run:
        output.line()
        output.key_values(
            [
                ("output", result.output_path),
                ("sha256", (result.record.output_hash or "")[:32] + "..."),
            ]
        )
        output.success("the source workbook was not modified")

    if result.verification is not None and not dry_run:
        output.line()
        output.verification_banner(result.verification)

    if result.report and verbose and not dry_run:
        output.line()
        output.heading("CHANGE REPORT")
        output.line(result.report)

    output.line()
    if result.outcome is RunOutcome.SUCCEEDED:
        output.success(f"run {result.run_id} completed and verified")
    elif result.outcome is RunOutcome.DRY_RUN:
        output.success(f"dry run {result.run_id} complete; nothing was written")
    elif result.outcome is RunOutcome.REJECTED_BY_POLICY:
        output.error(f"run {result.run_id} denied by policy")
    elif result.outcome is RunOutcome.REJECTED_BY_APPROVAL:
        output.error(f"run {result.run_id} rejected by the operator")
    else:
        output.error(f"run {result.run_id} failed: {result.error}")

    output.note(f"run directory: {FileRunStore().paths(result.run_id).root}")


# --------------------------------------------------------------------------
# diff
# --------------------------------------------------------------------------


@app.command(name="diff")
def diff_command(
    before: Annotated[str, typer.Argument(help="The original workbook.")],
    after: Annotated[str, typer.Argument(help="The changed workbook.")],
    as_json: JsonOption = False,
) -> None:
    """Compare two workbooks by content, not by bytes."""
    try:
        difference = diff_paths(_workbook(before), _workbook(after))
    except ExcelPilotError as error:
        output.error(str(error))
        raise typer.Exit(code=_code_for(error)) from error

    from app.diff import summarise

    if as_json:
        output.emit_json(
            {
                "command": "diff",
                "before": {"path": before, "hash": difference.before_hash},
                "after": {"path": after, "hash": difference.after_hash},
                "summary": difference.summary(),
                "sheets": [s.model_dump(mode="json") for s in difference.sheet_diffs],
                "cell_changes": [c.model_dump(mode="json") for c in difference.cell_changes],
                "cell_changes_truncated": difference.cell_changes_truncated,
                "structural_change": difference.structural_change,
                "notes": difference.notes,
            }
        )
        return

    output.heading("Diff")
    output.line()
    output.key_values(
        [
            ("before", before),
            ("after", after),
            ("identical", "yes" if difference.is_empty else "no"),
            ("structural change", "yes" if difference.structural_change else "no"),
        ]
    )
    output.line()
    output.line(summarise(difference))
    for note in difference.notes:
        output.note(note)


# --------------------------------------------------------------------------
# verify
# --------------------------------------------------------------------------


@app.command()
def verify(
    workbook: Annotated[str, typer.Argument(help="Path to the workbook to verify.")],
    run_id: Annotated[
        str | None,
        typer.Option("--run", help="Run id, to compare against that run's source snapshot."),
    ] = None,
    as_json: JsonOption = False,
    workspace: WorkspaceOption = None,
) -> None:
    """Verify a workbook on its own, or against a run's source snapshot.

    Verification is static: formulas are checked for presence, pattern, and
    reference integrity, never recalculated. ExcelPilot cannot evaluate Excel
    formulas, and never claims to.
    """
    from app.verification import Verifier, describe

    try:
        effective = _load_config(None, workspace=workspace)
        store = FileRunStore(effective)
        before = store.paths(run_id).snapshot_file if run_id else None
        if run_id and (before is None or not before.exists()):
            output.error(f"no source snapshot for run {run_id!r}")
            raise typer.Exit(code=Exit.NOT_FOUND)
        result = Verifier(effective.anomaly).verify(
            run_id or "adhoc", _workbook(workbook), before_path=before
        )
    except ExcelPilotError as error:
        output.error(str(error))
        raise typer.Exit(code=_code_for(error)) from error

    if as_json:
        output.emit_json(
            {
                "command": "verify",
                "run_id": run_id,
                "workbook": workbook,
                "status": result.status.value,
                "passed": result.passed,
                "recalculated": result.recalculated,
                "static_formula_checks": result.static_formula_checks,
                "checks": [c.model_dump(mode="json") for c in result.all_checks],
                "anomalies": [a.to_summary() for a in result.anomalies],
                "notes": result.notes,
            }
        )
        raise typer.Exit(code=Exit.SUCCESS if result.passed else Exit.VERIFICATION_FAILED)

    output.line(describe(result))
    raise typer.Exit(code=Exit.SUCCESS if result.passed else Exit.VERIFICATION_FAILED)


# --------------------------------------------------------------------------
# replay
# --------------------------------------------------------------------------


@app.command()
def replay(
    run_id: Annotated[str, typer.Argument(help="Run id to reconstruct.")],
    as_json: JsonOption = False,
    workspace: WorkspaceOption = None,
) -> None:
    """Reconstruct what a run did. Read-only: re-executes nothing by default.

    Replay never overwrites a workbook and never mutates the original run's
    artefacts. A replay that does execute gets a new run id.
    """
    try:
        effective = _load_config(None, workspace=workspace)
        store = FileRunStore(effective)
        record = store.read_run(run_id)
        manifest = store.read_manifest(run_id)
        verification = store.read_verification(run_id)
        from app.audit import AuditLog

        with AuditLog(store.paths(run_id).audit_file) as log:
            events = log.to_json()
    except StorageError as error:
        output.error(str(error))
        raise typer.Exit(code=Exit.NOT_FOUND) from error
    except ExcelPilotError as error:
        output.error(str(error))
        raise typer.Exit(code=_code_for(error)) from error

    if as_json:
        output.emit_json(
            {
                "command": "replay",
                "run": record.model_dump(mode="json"),
                "manifest": manifest.model_dump(mode="json") if manifest else None,
                "verification": verification.model_dump(mode="json") if verification else None,
                "audit_events": events,
            }
        )
        return

    output.heading(f"Replay: {run_id}")
    output.note("read-only: nothing was re-executed and nothing was modified")
    output.line()
    output.key_values(
        [
            ("outcome", str(record.outcome) if record.outcome else "-"),
            ("state", str(record.state)),
            ("created", record.created_at),
            ("duration", f"{record.duration_seconds:.2f}s" if record.duration_seconds else "-"),
            ("task", record.raw_task.to_redacted(160)),
            ("intent", record.intent_summary or "-"),
            ("source", f"{record.source_name} ({record.source_hash[:16]}...)"),
            ("output", record.output_path or "-"),
            ("policy", str(record.policy_outcome) if record.policy_outcome else "-"),
            ("approval", str(record.approval_status)),
            ("JEV", f"{record.jev_provider.value} (called={record.jev_called})"),
            ("verification", str(record.verification_status or "-")),
        ]
    )

    if verification is not None:
        output.line()
        output.heading("Verification")
        output.field("status", verification.status.value)
        output.field("recalculated", verification.recalculated)
        output.field("static formula checks", verification.static_formula_checks)

    if manifest is not None:
        output.line()
        output.heading("Changes")
        output.line(manifest.to_text())

    output.line()
    output.heading("Audit trail")
    output.table(
        "Events",
        ["#", "Actor", "Event", "Summary"],
        [
            [
                event["seq"],
                event["actor"],
                event["event_type"],
                str(event.get("payload", {}).get("run_id", ""))[:40],
            ]
            for event in events
        ],
    )


# --------------------------------------------------------------------------
# runs, policy, gc
# --------------------------------------------------------------------------


@app.command()
def runs(
    limit: Annotated[int, typer.Option("--limit", "-n", help="How many runs to show.")] = 20,
    as_json: JsonOption = False,
    workspace: WorkspaceOption = None,
) -> None:
    """List recent runs."""
    effective = _load_config(None, workspace=workspace)
    store = FileRunStore(effective)
    records = store.recent(limit=limit)

    if as_json:
        output.emit_json({"command": "runs", "runs": [r.model_dump(mode="json") for r in records]})
        return

    if not records:
        output.note("no runs recorded yet")
        return

    output.table(
        "Runs",
        ["Run", "Outcome", "Duration", "Workbook", "Task", "Output"],
        [
            [
                record.run_id,
                str(record.outcome or "-"),
                f"{record.duration_seconds:.1f}s" if record.duration_seconds else "-",
                record.source_name,
                (record.intent_summary or record.raw_task.to_redacted(40))[:40],
                Path(record.output_path).name if record.output_path else "-",
            ]
            for record in records
        ],
    )


@app.command()
def policy(
    as_json: JsonOption = False,
    config: ConfigOption = None,
    workspace: WorkspaceOption = None,
) -> None:
    """Show the effective policy: rules, thresholds, and what cannot be changed."""
    effective = _load_config(config, workspace=workspace)
    payload = explain(effective)

    if as_json:
        output.emit_json({"command": "policy", **payload})
        return

    output.heading("Policy")
    output.field("config fingerprint", payload["config_fingerprint"])
    output.line()
    output.line("Hard deny rules (cannot be disabled by configuration):")
    for rule_id in payload["hard_deny_rules"]:
        output.field("", rule_id)
    output.line()
    output.line("Escalation rules (set require_approval when they fire):")
    for rule_id in payload["escalation_rules"]:
        output.field("", rule_id)
    output.line()
    output.heading("Thresholds")
    output.table(
        "Setting",
        ["Name", "Value"],
        [[key, value] for key, value in sorted(payload["thresholds"].items())],
    )
    output.line()
    for note in payload["notes"]:
        output.note(note)


@app.command()
def gc(
    keep: Annotated[int, typer.Option("--keep", help="How many runs to keep.")] = 50,
    workspace: WorkspaceOption = None,
) -> None:
    """Delete the oldest run directories, keeping the newest ``--keep``.

    Run directories are the audit record, so this is explicit rather than
    automatic. Deleting a run removes its audit trail.
    """
    effective = _load_config(None, workspace=workspace)
    store = FileRunStore(effective)
    removed = store.prune(keep=keep)
    if not removed:
        output.note(f"nothing to prune; {len(store.list_runs())} run(s) present")
        return
    for run_id in removed:
        output.line(f"removed {run_id}")
    output.success(f"pruned {len(removed)} run director{'y' if len(removed) == 1 else 'ies'}")


@app.command()
def version() -> None:
    """Print the version."""
    output.line(f"excelpilot {__version__}")


# --------------------------------------------------------------------------
# Error mapping
# --------------------------------------------------------------------------


def _code_for(error: ExcelPilotError) -> Exit:
    """Map a typed error to an exit code."""
    if isinstance(error, PolicyDenied):
        return Exit.POLICY_DENIED
    if isinstance(error, PaidCallBlocked):
        return Exit.PAID_CALL_BLOCKED
    if isinstance(error, StorageError):
        return Exit.NOT_FOUND
    if isinstance(error, (ConfigError, WorkbookSecurityError)):
        return Exit.USAGE
    return Exit.INTERNAL_ERROR


def main() -> int:
    """Console-script entry point.

    ``standalone_mode=True`` is deliberate. It lets typer handle usage errors,
    ``--help``, and ``--version`` natively, all of which exit via ``SystemExit``
    with the right code. In non-standalone mode typer 0.27 *returns* a command's
    exit code instead of raising ``typer.Exit``, which an earlier version of this
    function missed — so every failure silently exited 0, and a script could not
    tell success from refusal.

    Unexpected exceptions are still converted to a clean message rather than a
    traceback, because a CLI should not leak a stack trace at an operator.
    """
    try:
        app(standalone_mode=True)
    except SystemExit:
        raise
    except typer.Abort:
        output.error("aborted")
        return int(Exit.USAGE)
    except ExcelPilotError as error:  # pragma: no cover - defensive
        output.error(str(error))
        return int(_code_for(error))
    except Exception as error:  # noqa: BLE001 - the CLI must not traceback at users
        output.error(f"unexpected error: {type(error).__name__}: {error}")
        if "--verbose" in sys.argv or "-v" in sys.argv:
            raise
        return int(Exit.INTERNAL_ERROR)
    return int(Exit.SUCCESS)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = ["Exit", "app", "main"]
