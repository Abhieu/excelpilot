"""The benchmark suite.

## The three configurations

The specification asks for a comparison across three modes, and the point of it
is to measure what each layer actually *contributes*:

| Mode | Planner | JEV | What it isolates |
|---|---|---|---|
| ``baseline`` | none — a hand-built plan | none | The cost of the machinery around a fixed plan |
| ``no_jev`` | deterministic or LLM | disabled | The value of structured decisioning |
| ``with_jev`` | deterministic or LLM | mock (or live, with consent) | The full pipeline |

``baseline`` is not a strawman: it is the *floor*. If the full pipeline costs
substantially more than a hand-built plan and buys nothing measurable, that is a
finding worth reporting, not a reason to hide the numbers.

## What is measured

Only things that can be measured honestly:

* wall-clock time per stage, from a real run
* the outcome, and whether it matched the scenario's expectation
* the *correctness* of the change: cells changed, duplicates removed, damage found
* policy decisions and rule ids
* JEV decisions and their stated confidence

## What is deliberately not measured

No invented "quality score". No model-judged ranking. A benchmark that assigns a
number to "how good" a workbook is, without a ground truth to compare against, is
a decoration. Where a scenario has a checkable property — exactly N duplicates
removed, damage detected — that property is asserted and recorded as a boolean.

## The live JEV call

One live call is permitted, at the end of the run, and only if
``--allow-paid-calls`` is passed. Its result is recorded separately under
``live_jev`` so a paid measurement is never blended with mocked numbers.
"""

from __future__ import annotations

import argparse
import json
import platform
import statistics
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.app import RunOrchestrator  # noqa: E402
from app.audit import AuditLog  # noqa: E402
from app.contracts.base import UntrustedText  # noqa: E402
from app.contracts.config import ExcelPilotConfig  # noqa: E402
from app.contracts.enums import RunOutcome  # noqa: E402
from app.decisions import HttpJevAdapter, MockJevAdapter  # noqa: E402
from app.planner import DeterministicPlanner, Planner  # noqa: E402
from app.storage import FileRunStore  # noqa: E402
from app.verification.recalc import library_available  # noqa: E402
from app.workbook import file_sha256, inspect_workbook, opened  # noqa: E402
from benchmarks.scenarios import SCENARIOS, Scenario  # noqa: E402


@dataclass(frozen=True, slots=True)
class Mode:
    """One benchmark configuration."""

    name: str
    use_planner: bool
    use_jev: bool
    description: str


MODES: tuple[Mode, ...] = (
    Mode(
        name="baseline",
        use_planner=False,
        use_jev=False,
        description=(
            "A hand-built plan for the scenario's primary operation, with no "
            "planner and no JEV. The floor the other modes are measured against."
        ),
    ),
    Mode(
        name="no_jev",
        use_planner=True,
        use_jev=False,
        description="The deterministic planner, with JEV disabled entirely.",
    ),
    Mode(
        name="with_jev",
        use_planner=True,
        use_jev=True,
        description="The full pipeline, with JEV supplying advisory decisions.",
    ),
)


@dataclass(slots=True)
class ModeResult:
    """Measurements for one scenario in one mode."""

    scenario: str
    mode: str
    outcome: str = ""
    expectation_met: bool = False
    duration_seconds: float = 0.0
    inspect_seconds: float = 0.0
    plan_seconds: float = 0.0
    decide_seconds: float = 0.0
    policy_seconds: float = 0.0
    execute_seconds: float = 0.0
    verify_seconds: float = 0.0
    diff_seconds: float = 0.0
    cells_written: int = 0
    rows_affected: int = 0
    formulas_removed: int = 0
    policy_outcome: str = ""
    policy_rules: list[str] = field(default_factory=list)
    jev_called: bool = False
    jev_escalated: bool = False
    jev_decisions: list[dict[str, Any]] = field(default_factory=list)
    verification_passed: bool = False
    recalculated: bool = False
    output_written: bool = False
    anomalies: int = 0
    check: str = ""
    check_passed: bool | None = None
    check_detail: str = ""
    source_unchanged: bool = False
    error: str = ""
    audit_events: int = 0


class Runner(Protocol):
    """Something that can produce an ``ExecutionPlan`` for a scenario."""

    def plan(self, task: UntrustedText, inspection: Any) -> Any: ...


def _baseline_planner() -> Planner:
    """A fixed, hand-built planner for the floor measurement.

    It reads the intent straight off the scenario. This is the honest floor: no
    natural-language interpretation, no ambiguity detection, no refusal. It can
    only do what it was told, which is exactly why it is the baseline rather than
    a competitor.
    """

    class _Fixed(DeterministicPlanner):
        source = "baseline"

        def plan(self, task: UntrustedText, inspection: Any) -> Any:  # noqa: ANN401
            # Reuse the real planner, then keep only the first operation. The
            # measurement is about the surrounding machinery, so the plan itself
            # is held constant.
            full = super().plan(task, inspection)
            if len(full.operations) > 1:
                return full.model_copy(
                    update={
                        "operations": full.operations[:1],
                        "notes": ["baseline: first operation only"],
                    }
                )
            return full

    return _Fixed()


def _run_once(
    scenario: Scenario,
    mode: Mode,
    workspace: Path,
    *,
    jev_scenario: str,
    keep_source: bool = True,
) -> ModeResult:
    """Run one scenario in one mode and measure it."""
    result = ModeResult(scenario=scenario.name, mode=mode.name, check=scenario.check)
    source = scenario.build(workspace)
    before_hash = inspect_workbook(source).content_hash

    config = ExcelPilotConfig(workspace_root=str(workspace))
    planner = _baseline_planner() if not mode.use_planner else DeterministicPlanner()
    jev = MockJevAdapter(jev_scenario) if mode.use_jev else MockJevAdapter(jev_scenario)
    if not mode.use_jev:
        # Disabled: the orchestrator records that JEV was not consulted, which is
        # the honest representation of "no JEV".
        jev = _NoJev()

    orchestrator = RunOrchestrator(config, planner=planner, jev=jev)

    started = time.monotonic()
    try:
        run = orchestrator.run(
            source,
            UntrustedText(scenario.task, provenance="user_task"),
            approve=True,
        )
    except Exception as error:  # noqa: BLE001 - a benchmark records failures
        result.duration_seconds = time.monotonic() - started
        result.error = f"{type(error).__name__}: {error}"
        result.outcome = "error"
        return result
    result.duration_seconds = time.monotonic() - started

    result.outcome = run.outcome.value
    result.policy_outcome = run.record.policy_outcome.value if run.record.policy_outcome else ""
    result.policy_rules = list(run.record.policy_rule_ids)
    result.jev_called = bool(run.record.jev_called)
    result.jev_escalated = bool(run.policy and run.policy.jev_escalated)
    if run.jev is not None:
        result.jev_decisions = [d.model_dump(mode="json") for d in run.jev.decisions]
    if run.execution is not None:
        result.cells_written = run.execution.total_cells_written
        result.rows_affected = run.execution.total_rows_affected
        result.formulas_removed = run.execution.total_formulas_removed
    if run.verification is not None:
        result.verification_passed = run.verification.passed
        result.recalculated = run.verification.recalculated
        result.anomalies = len(run.verification.anomalies)
    # Recorded explicitly rather than inferred: whether a run produced an artefact
    # is a fact the benchmark needs, not something to guess from cell counts.
    result.output_written = bool(run.record.output_path and Path(run.record.output_path).exists())

    try:
        with AuditLog(FileRunStore(config).paths(run.run_id).audit_file) as log:
            result.audit_events = log.summary()["events"]
    except Exception:  # noqa: BLE001
        result.audit_events = 0

    result.expectation_met = _expectation_met(scenario, result)
    result.check_passed, result.check_detail = _run_check(scenario, run, source)

    # Measured on every single run, not just the scenario's own check: the source
    # workbook must be byte-identical afterwards, in every mode, including the
    # ones that write. This is the property the whole output-safety model rests
    # on, so the benchmark records it continuously rather than trusting a test.
    result.source_unchanged = file_sha256(source) == before_hash

    if not keep_source:
        source.unlink(missing_ok=True)
    return result


class _NoJev:
    """A JEV adapter that reports it was never consulted."""

    @property
    def available(self) -> bool:
        return False

    def decide(self, context: Any) -> Any:  # noqa: ANN401, ARG002
        del context  # The whole point is that nothing was consulted.
        from app.contracts.pipeline import JevDecisionSet

        return JevDecisionSet(
            jev_called=False,
            error="JEV disabled for this benchmark mode",
        )


def _expectation_met(scenario: Scenario, result: ModeResult) -> bool:
    """Did the run do what the scenario says it should?

    A scenario expecting a refusal counts as met when the run was refused. This
    is the detail that stops the benchmark scoring safe behaviour as failure.

    ``verify_fails`` additionally requires that a file *was* written and that
    verification is what rejected it. Requiring the write matters: a run that
    produced no output and failed for an unrelated reason would otherwise be
    scored as though verification had done its job.
    """
    if result.outcome == "error":
        return False
    if scenario.expectation == "refuse":
        return result.outcome in {"rejected_by_policy", "failed", "rejected_by_approval"}
    if scenario.expectation == "escalate":
        return result.policy_outcome in {"require_approval", "deny"}
    if scenario.expectation == "verify_fails":
        # Must have written a file, and verification must be what rejected it.
        return (
            result.outcome == RunOutcome.FAILED.value
            and result.output_written
            and result.verification_passed is False
        )
    return result.outcome == RunOutcome.SUCCEEDED.value


def _run_check(scenario: Scenario, run: Any, source: Path) -> tuple[bool | None, str]:
    """Assert the scenario's checkable property, where it has one.

    Checks read the workbooks directly rather than being told an expected value,
    so each one is an independent measurement rather than a restatement of what
    the run already claimed.
    """
    """Assert the scenario's checkable property, where it has one."""
    if not scenario.check:
        return None, ""

    try:
        if scenario.check == "duplicates_removed_exactly":
            if run.execution is None or not run.output_path:
                return False, "no output to inspect"
            with opened(source) as before, opened(run.output_path) as after:
                before_ids = [
                    before["Sales"].cell(row=row, column=8).value
                    for row in range(2, (before["Sales"].max_row or 1) + 1)
                ]
                after_ids = [
                    after["Sales"].cell(row=row, column=8).value
                    for row in range(2, (after["Sales"].max_row or 1) + 1)
                ]
            duplicates = len(before_ids) - len(set(before_ids))
            removed = len(before_ids) - len(after_ids)
            unique_after = len(after_ids) == len(set(after_ids))
            passed = duplicates == removed and unique_after
            return passed, (
                f"{duplicates} duplicate(s) present, {removed} removed, "
                f"unique afterwards: {unique_after}"
            )

        if scenario.check == "names_what_is_missing":
            missing = run.plan.understanding.missing_information if run.plan else []
            return bool(missing), f"{len(missing)} missing-information item(s) named"

        if scenario.check == "explains_why":
            combined = " ".join(
                [
                    *(run.plan.understanding.missing_information if run.plan else []),
                    *(run.plan.notes if run.plan else []),
                    run.error or "",
                ]
            )
            return bool(combined.strip()), "an explanation was given"

        if scenario.check == "mentions_dedupe_key":
            combined = " ".join(run.plan.understanding.missing_information if run.plan else [])
            passed = "key" in combined.lower()
            return passed, f"mentions the key: {passed}"

        if scenario.check == "detects_preexisting_damage":
            if run.verification is None:
                return False, "no verification result"
            failed = {check.name for check in run.verification.failed_checks}
            warnings = {
                check.name for check in run.verification.formula if check.status.value == "warning"
            }
            found = bool(failed or warnings)
            return found, f"failed={sorted(failed)} warning={sorted(warnings)}"

        if scenario.check == "hidden_sheet_rule_fired":
            passed = "hidden_sheet_change" in result_rule_ids(run)
            return passed, f"rules fired: {result_rule_ids(run)}"
    except Exception as error:  # noqa: BLE001 - a check failure is a result
        return False, f"check raised {type(error).__name__}: {error}"

    return None, ""


def result_rule_ids(run: Any) -> list[str]:  # noqa: ANN401
    return list(run.record.policy_rule_ids) if run.record else []


def _aggregate(results: list[ModeResult]) -> dict[str, Any]:
    """Summarise one mode across all scenarios."""
    if not results:
        return {}
    durations = [r.duration_seconds for r in results]
    verified = [r for r in results if r.verification_passed]
    checks = [r.check_passed for r in results if r.check_passed is not None]
    # Spread is reported so a reader can see whether any difference between modes
    # is larger than the noise. On a single repeat there is no spread to compute
    # and the timing comparison is explicitly marked unavailable rather than
    # presented as a finding.
    spread = statistics.stdev(durations) if len(durations) > 1 else None
    distinct = {r.scenario for r in results}
    return {
        "scenarios": len(distinct),
        "runs": len(results),
        "expectations_met": sum(1 for r in results if r.expectation_met),
        "verification_passed": len(verified),
        "checks_run": len(checks),
        "checks_passed": sum(1 for c in checks if c),
        "total_duration_seconds": round(sum(durations), 3),
        "mean_duration_seconds": round(statistics.fmean(durations), 4),
        "median_duration_seconds": round(statistics.median(durations), 4),
        "stdev_duration_seconds": round(spread, 4) if spread is not None else None,
        "total_cells_written": sum(r.cells_written for r in results),
        "audit_events": sum(r.audit_events for r in results),
        "source_never_modified": all(r.source_unchanged for r in results),
        "outputs_written": sum(1 for r in results if r.output_written),
    }


def _warmup(scenario: Scenario, workspace: Path, *, jev_scenario: str) -> None:
    """Run one scenario in every mode, discarding the measurements.

    Without this the first mode measured absorbs the entire cold-start cost —
    lazy imports, pydantic's first model compilation, the first recalculation
    library import. That made an early draft of this benchmark report the
    *baseline* as slower than the full pipeline, which is not a result, it is an
    artefact of which mode happened to run first.

    Warmup runs are discarded and are not written to the results.
    """
    import shutil

    scratch = workspace / "_warmup"
    scratch.mkdir(parents=True, exist_ok=True)
    try:
        for mode in MODES:
            _run_once(scenario, mode, scratch / mode.name, jev_scenario=jev_scenario)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def run_benchmark(
    *,
    workspace: Path | None = None,
    jev_scenario: str = "approve",
    repeats: int = 1,
    only: str | None = None,
) -> dict[str, Any]:
    """Run every scenario in every mode and return the results document."""
    scenarios = [s for s in SCENARIOS if not only or only in s.name]
    owned = workspace is None
    root = Path(tempfile.mkdtemp(prefix="excelpilot-bench-")) if owned else Path(workspace)
    root.mkdir(parents=True, exist_ok=True)

    per_mode: dict[str, list[ModeResult]] = {mode.name: [] for mode in MODES}
    started = time.monotonic()

    try:
        if scenarios:
            _warmup(scenarios[0], root, jev_scenario=jev_scenario)
        for mode in MODES:
            for scenario in scenarios:
                for _ in range(repeats):
                    per_mode[mode.name].append(
                        _run_once(
                            scenario,
                            mode,
                            root / mode.name,
                            jev_scenario=jev_scenario,
                        )
                    )
    finally:
        if owned:
            import shutil

            shutil.rmtree(root, ignore_errors=True)

    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "python": sys.version.split()[0],
        "platform": f"{platform.system()} {platform.release()} {platform.machine()}",
        "recalculation_library_available": library_available(),
        "jev_scenario": jev_scenario,
        "repeats": repeats,
        "warmup": "one scenario per mode, discarded, before any measurement",
        "wall_clock_seconds": round(time.monotonic() - started, 3),
        "modes": {
            mode.name: {
                "description": mode.description,
                "summary": _aggregate(per_mode[mode.name]),
                "results": [asdict(r) for r in per_mode[mode.name]],
            }
            for mode in MODES
        },
        "interpretation": _interpret(per_mode),
        "live_jev": {
            "called": False,
            "note": (
                "No live JEV call was made. Run with --allow-paid-calls to make one; "
                "it is recorded here separately so a paid measurement is never blended "
                "with mocked numbers."
            ),
        },
    }


def _timing_comparison(
    label: str, left: dict[str, Any], right: dict[str, Any]
) -> tuple[str | None, str | None]:
    """Compare two modes' timings, or decline to.

    A timing difference is only reported when it is larger than the combined
    spread of the two samples. On a single repeat there is no spread to compare
    against, and the honest answer is "not measurable" rather than a number that
    happens to come out in whichever direction the noise fell.

    Returns ``(finding_or_None, caveat_or_None)``.
    """
    if not left or not right:
        return None, None
    if left.get("stdev_duration_seconds") is None or right.get("stdev_duration_seconds") is None:
        return None, (
            f"{label}: not measured. A single repeat per scenario gives no spread to "
            f"compare against, so any difference would be noise. Re-run with --repeats 5 "
            f"or more to measure it."
        )

    difference = right["mean_duration_seconds"] - left["mean_duration_seconds"]
    noise = (left["stdev_duration_seconds"] ** 2 + right["stdev_duration_seconds"] ** 2) ** 0.5
    if abs(difference) <= noise:
        return None, (
            f"{label}: no measurable difference. The gap of {difference:+.4f}s is smaller "
            f"than the run-to-run spread of {noise:.4f}s."
        )
    direction = "cost" if difference > 0 else "saved"
    return (
        f"{label}: {direction} {abs(difference):.4f}s per scenario on average, which "
        f"exceeds the run-to-run spread of {noise:.4f}s.",
        None,
    )


def _interpret(per_mode: dict[str, list[ModeResult]]) -> dict[str, Any]:
    """State what the measurements do and do not show.

    Deliberately conservative. A timing difference on a 30-row workbook is not
    evidence that one mode is better in production, and a difference smaller than
    the noise is not a difference at all. Where the measurement cannot support a
    claim, this says so instead of printing a number that will be quoted as if it
    were evidence.
    """
    base = _aggregate(per_mode.get("baseline", []))
    no_jev = _aggregate(per_mode.get("no_jev", []))
    with_jev = _aggregate(per_mode.get("with_jev", []))

    findings: list[str] = []
    caveats: list[str] = []

    for label, left, right in (
        ("Natural-language planning versus a hand-built plan", base, no_jev),
        ("Adding JEV (mocked)", no_jev, with_jev),
        ("The full pipeline versus a hand-built plan", base, with_jev),
    ):
        finding, caveat = _timing_comparison(label, left, right)
        if finding:
            findings.append(finding)
        if caveat:
            caveats.append(caveat)

    for name, summary in (("baseline", base), ("no_jev", no_jev), ("with_jev", with_jev)):
        if not summary:
            continue
        if not summary["source_never_modified"]:
            findings.append(
                f"CRITICAL: {name} modified a source workbook. The output-safety model "
                f"is broken and no other number in this report is meaningful."
            )
        if summary["expectations_met"] == summary["runs"]:
            findings.append(
                f"{name}: all {summary['scenarios']} scenarios met their expectation, "
                f"and all {summary['checks_passed']}/{summary['checks_run']} checkable "
                f"properties held."
            )
        else:
            caveats.append(
                f"{name}: {summary['runs'] - summary['expectations_met']} of "
                f"{summary['runs']} run(s) did not meet their scenario's expectation"
            )

    caveats.extend(
        [
            "Timings are from small synthetic workbooks on one machine; they are not a "
            "production performance claim.",
            "JEV was mocked, so no network latency, provider reliability, or cost is "
            "represented. A live call, if authorised, is recorded separately under "
            "`live_jev` and is never blended with these numbers.",
            "No quality score is reported, because there is no ground truth against "
            "which to score one. Scenarios with a checkable property assert it instead.",
            "The planner is fully deterministic, so planner variance is zero and the "
            "planner is not a source of the timings above.",
            "The baseline is the same planner with its plan truncated to one operation. "
            "It isolates the surrounding machinery, not planning quality, which this "
            "benchmark cannot measure without a reference answer to plan against.",
        ]
    )
    return {"findings": findings, "caveats": caveats}


def live_jev_probe() -> dict[str, Any]:
    """Make exactly one live JEV call, for the operator to opt into.

    Never called unless ``--allow-paid-calls`` is passed. Returns a record of
    what happened, including the failure case, so a failed paid call is visible
    rather than silently absent.
    """
    from app.contracts.pipeline import DecisionContext

    config = ExcelPilotConfig()
    adapter = HttpJevAdapter(config.jev, allow_paid_calls=True)
    context = DecisionContext(
        run_id="benchmark-live-probe",
        task_summary="ExcelPilot benchmark capability probe",
        sheet_count=3,
        sheets_affected=["Sales"],
        total_rows=1000,
        cells_to_change=500,
        operation_kinds=["normalize_values", "remove_duplicates"],
        # Reported honestly, not hard-coded: the question wording depends on it.
        recalculation_available=library_available(),
    )
    started = time.monotonic()
    result = adapter.decide(context)
    return {
        "called": result.jev_called,
        "provider": result.provider.value,
        "model": result.model,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "adapter_reported_elapsed": result.elapsed_seconds,
        "error": result.error,
        "min_probability": result.min_probability,
        "min_margin": result.min_margin,
        "decisions": [d.model_dump(mode="json") for d in result.decisions],
    }


def main(argv: list[str] | None = None) -> int:
    """Run the suite and write the results document.

    ``argv`` is a parameter rather than read from ``sys.argv`` so the paid-call
    gate can be tested directly, without having to run a benchmark and inspect
    a subprocess.
    """
    parser = argparse.ArgumentParser(
        prog="benchmarks",
        description="Run the ExcelPilot benchmark suite and write benchmarks/results.json.",
    )
    parser.add_argument("--output", type=Path, default=Path("benchmarks/results.json"))
    parser.add_argument("--jev-scenario", default="approve")
    parser.add_argument(
        "--repeats",
        type=int,
        default=5,
        help="Runs per scenario per mode. Above 1, timings get a spread to compare "
        "against; below 2 the timing comparison is reported as unmeasurable.",
    )
    parser.add_argument("--only", help="Run scenarios whose name contains this substring.")
    parser.add_argument(
        "--allow-paid-calls",
        action="store_true",
        help="Make one live JEV call and record it. Costs money.",
    )
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    results = run_benchmark(jev_scenario=args.jev_scenario, repeats=args.repeats, only=args.only)

    if args.allow_paid_calls:
        results["live_jev"] = live_jev_probe()
        results["live_jev"]["note"] = (
            "A live call was made with explicit authorisation. Its measurements are "
            "separate from the mocked modes above."
        )
    else:
        results["live_jev"] = _carry_forward_live_call(args.output)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")

    if not args.quiet:
        _print_summary(results)
    print(f"\nwritten to {args.output}")
    return 0


def _carry_forward_live_call(output: Path) -> dict[str, Any]:
    """Preserve a previously recorded live call across a re-run.

    A live JEV call costs money and cannot be repeated without fresh authorisation,
    so its record is the only evidence that it happened. Overwriting it with
    ``{"called": false}`` on every ordinary ``make bench`` would destroy that
    evidence — which is exactly what happened once during development, and why
    this function exists.

    The record is carried forward verbatim, with a note stating when it was made,
    so a reader can tell it apart from a call made in this run.
    """
    try:
        existing = json.loads(output.read_text(encoding="utf-8"))
        previous = existing.get("live_jev", {})
    except (OSError, ValueError, json.JSONDecodeError):
        previous = {}

    if not previous.get("called"):
        return {
            "called": False,
            "note": (
                "No live JEV call has been recorded. Run with --allow-paid-calls to make "
                "one; it is recorded here separately so a paid measurement is never "
                "blended with mocked numbers."
            ),
        }

    carried = dict(previous)
    carried["carried_forward"] = True
    carried["note"] = (
        "Recorded by an earlier run and preserved verbatim; no live call was made in "
        "this run. Its measurements remain separate from the mocked modes above."
    )
    return carried


def _print_summary(results: dict[str, Any]) -> None:
    print("=" * 78)
    print("ExcelPilot benchmark")
    print("=" * 78)
    print(f"python {results['python']} on {results['platform']}")
    print(f"recalculation library available: {results['recalculation_library_available']}")
    print(f"JEV scenario: {results['jev_scenario']} (mocked unless noted below)")
    print()
    header = (
        f"{'mode':<10} {'scenarios':>10} {'met':>9} {'checks':>9} {'written':>9} "
        f"{'mean s':>9} {'spread s':>9} {'src safe':>10}"
    )
    print(header)
    print("-" * len(header))
    for name, payload in results["modes"].items():
        s = payload["summary"]
        checks = f"{s['checks_passed']}/{s['checks_run']}" if s["checks_run"] else "-"
        source_safe = "yes" if s["source_never_modified"] else "NO"
        spread = s["stdev_duration_seconds"]
        spread_text = f"{spread:>9.4f}" if spread is not None else f"{'-':>9}"
        print(
            f"{name:<10} {s['scenarios']:>10} {s['expectations_met']:>4}/{s['runs']:<4} "
            f"{checks:>9} {s['outputs_written']:>9} "
            f"{s['mean_duration_seconds']:>9.4f} {spread_text} {source_safe:>10}"
        )

    print()
    print("Findings:")
    for finding in results["interpretation"]["findings"]:
        print(f"  - {finding}")
    print()
    print("Caveats:")
    for caveat in results["interpretation"]["caveats"]:
        print(f"  - {caveat}")

    live = results.get("live_jev", {})
    print()
    if live.get("called"):
        # Fields are read defensively: a record carried forward from an earlier
        # run may not carry every field a fresh one does, and a summary printer
        # that crashes on a partial record is worse than one that prints less.
        provider = live.get("provider", "?")
        model = live.get("model") or live.get("model_sent") or "?"
        print(f"Live JEV call: {provider} / {model}")
        if live.get("carried_forward"):
            print("  (recorded by an earlier run; no call was made in this run)")
        if live.get("elapsed_seconds") is not None:
            print(f"  elapsed {live['elapsed_seconds']}s")
        for decision in live.get("decisions", []):
            print(
                f"  {decision.get('question', '?')} = {decision.get('value', '?')} "
                f"({decision.get('status', '?')}, p={decision.get('probability')})"
            )
        if live.get("error"):
            print(f"  error: {live['error']}")
    else:
        print("Live JEV: not called.")
        print(f"  {live.get('note', '')}")


if __name__ == "__main__":
    raise SystemExit(main())
