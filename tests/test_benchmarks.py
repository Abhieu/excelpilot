"""Tests for the benchmark harness itself.

A benchmark that cannot fail is decoration. These tests assert the things that
would otherwise go wrong quietly:

* the harness runs every scenario in every mode
* expectations are scored the way the report describes, including the four
  distinct outcomes — in particular that a *correct* verification failure is not
  scored as a refusal
* the "source was never modified" measurement is real, not a constant
* a single repeat cannot produce a timing claim
* the live JEV probe is never reachable without the paid-call flag

These are marked ``benchmark`` and do not run in the default suite; run them with
``uv run pytest -m benchmark``.
"""

from __future__ import annotations

import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest
from benchmarks.run import (
    MODES,
    Mode,
    ModeResult,
    _aggregate,
    _expectation_met,
    _timing_comparison,
    main,
    run_benchmark,
)
from benchmarks.scenarios import SCENARIOS

pytestmark = pytest.mark.benchmark


@pytest.fixture(scope="module")
def results(tmp_path_factory: pytest.TempPathFactory) -> dict[str, object]:
    """One full pass over every scenario and mode, shared by the tests below."""
    workspace = tmp_path_factory.mktemp("bench")
    return run_benchmark(workspace=workspace, repeats=1)


class TestHarnessCoverage:
    def test_every_scenario_runs_in_every_mode(self, results: dict[str, object]) -> None:
        modes = results["modes"]  # type: ignore[index]
        assert set(modes) == {mode.name for mode in MODES}
        for name, payload in modes.items():  # type: ignore[union-attr]
            measured = {r["scenario"] for r in payload["results"]}  # type: ignore[index]
            assert measured == {s.name for s in SCENARIOS}, (
                f"mode {name} did not run every scenario"
            )

    def test_scenarios_cover_every_expectation_kind(self) -> None:
        kinds = {s.expectation for s in SCENARIOS}
        assert kinds == {"succeed", "escalate", "refuse", "verify_fails"}, (
            "a scenario kind is untested by the suite"
        )

    def test_no_scenario_expects_a_run_to_succeed_on_a_refusal_fixture(self) -> None:
        """The dangerous failure: a benchmark that rewards writing files."""
        for scenario in SCENARIOS:
            if scenario.name in {
                "ambiguous_request",
                "unsupported_capability",
                "destructive_without_key",
                "injection_in_request",
            }:
                assert scenario.expectation == "refuse", (
                    f"{scenario.name} must expect a refusal, not a success"
                )


class TestExpectationScoring:
    """The four outcomes are distinct, and conflating them hides real failures."""

    def _result(self, **kwargs: object) -> ModeResult:
        defaults: dict[str, object] = {
            "scenario": "x",
            "mode": "with_jev",
            "outcome": "succeeded",
            "policy_outcome": "allow",
        }
        return ModeResult(**{**defaults, **kwargs})  # type: ignore[arg-type]

    def _scenario(self, expectation: str) -> object:
        from benchmarks.scenarios import Scenario

        return Scenario(name="x", task="t", fixture="monthly_sales", expectation=expectation)

    def test_succeed_requires_success(self) -> None:
        scenario = self._scenario("succeed")
        assert _expectation_met(scenario, self._result()) is True  # type: ignore[arg-type]
        assert _expectation_met(scenario, self._result(outcome="failed")) is False  # type: ignore[arg-type]

    def test_refuse_accepts_a_denial(self) -> None:
        scenario = self._scenario("refuse")
        assert _expectation_met(scenario, self._result(outcome="rejected_by_policy")) is True  # type: ignore[arg-type]

    def test_refuse_rejects_a_successful_write(self) -> None:
        scenario = self._scenario("refuse")
        assert _expectation_met(scenario, self._result()) is False  # type: ignore[arg-type]

    def test_escalate_requires_a_policy_escalation(self) -> None:
        scenario = self._scenario("escalate")
        assert (  # type: ignore[arg-type]
            _expectation_met(scenario, self._result(policy_outcome="require_approval")) is True
        )
        assert _expectation_met(scenario, self._result(policy_outcome="allow")) is False  # type: ignore[arg-type]

    def test_verify_fails_needs_a_write_and_a_rejection(self) -> None:
        """The detail that separates 'stopped it' from 'checked it'."""
        scenario = self._scenario("verify_fails")
        assert (  # type: ignore[arg-type]
            _expectation_met(
                scenario,
                self._result(
                    outcome="failed",
                    output_written=True,
                    verification_passed=False,
                ),
            )
            is True
        )

    def test_verify_fails_rejects_a_run_that_wrote_nothing(self) -> None:
        """Otherwise an unrelated failure scores as though verification worked."""
        scenario = self._scenario("verify_fails")
        assert (  # type: ignore[arg-type]
            _expectation_met(
                scenario,
                self._result(
                    outcome="failed",
                    output_written=False,
                    verification_passed=False,
                ),
            )
            is False
        )

    def test_verify_fails_is_not_the_same_as_refuse(self) -> None:
        """A verification failure must not be scored as a policy refusal."""
        scenario = self._scenario("verify_fails")
        denied = self._result(
            outcome="rejected_by_policy", output_written=False, verification_passed=True
        )
        assert _expectation_met(scenario, denied) is False  # type: ignore[arg-type]

    def test_an_exception_is_never_met(self) -> None:
        for kind in ("succeed", "escalate", "refuse", "verify_fails"):
            assert (  # type: ignore[arg-type]
                _expectation_met(self._scenario(kind), self._result(outcome="error")) is False
            )


class TestSourceSafetyMeasurement:
    def test_source_is_never_modified_in_any_mode(self, results: dict[str, object]) -> None:
        for name, payload in results["modes"].items():  # type: ignore[union-attr]
            assert payload["summary"]["source_never_modified"] is True, (  # type: ignore[index]
                f"{name} modified a source workbook"
            )

    def test_every_run_records_the_measurement_rather_than_defaulting(
        self, results: dict[str, object]
    ) -> None:
        """A field that is never set is indistinguishable from one always true."""
        total = 0
        for payload in results["modes"].values():  # type: ignore[union-attr]
            for entry in payload["results"]:  # type: ignore[index]
                assert "source_unchanged" in entry
                total += 1
        assert total == len(SCENARIOS) * len(MODES)

    def test_a_modified_source_would_be_reported_as_critical(self) -> None:
        """The safety claim is falsifiable, not asserted."""
        bad = ModeResult(scenario="x", mode="with_jev", outcome="succeeded", source_unchanged=False)
        summary = _aggregate([bad])
        assert summary["source_never_modified"] is False


class TestTimingHonesty:
    def test_a_single_repeat_cannot_produce_a_timing_claim(self) -> None:
        left = {"mean_duration_seconds": 0.1, "stdev_duration_seconds": None}
        right = {"mean_duration_seconds": 0.9, "stdev_duration_seconds": None}
        finding, caveat = _timing_comparison("test", left, right)
        assert finding is None
        assert caveat is not None and "not measured" in caveat

    def test_a_difference_inside_the_spread_is_not_a_finding(self) -> None:
        left = {"mean_duration_seconds": 0.100, "stdev_duration_seconds": 0.05}
        right = {"mean_duration_seconds": 0.120, "stdev_duration_seconds": 0.05}
        finding, caveat = _timing_comparison("test", left, right)
        assert finding is None
        assert caveat is not None and "no measurable difference" in caveat

    def test_a_difference_beyond_the_spread_is_reported(self) -> None:
        left = {"mean_duration_seconds": 0.100, "stdev_duration_seconds": 0.01}
        right = {"mean_duration_seconds": 0.200, "stdev_duration_seconds": 0.01}
        finding, caveat = _timing_comparison("test", left, right)
        assert finding is not None and "cost" in finding
        assert caveat is None

    def test_a_faster_pipeline_is_reported_as_saving_time(self) -> None:
        left = {"mean_duration_seconds": 0.200, "stdev_duration_seconds": 0.01}
        right = {"mean_duration_seconds": 0.100, "stdev_duration_seconds": 0.01}
        finding, _ = _timing_comparison("test", left, right)
        assert finding is not None and "saved" in finding

    def test_missing_modes_do_not_crash_the_comparison(self) -> None:
        assert _timing_comparison("test", {}, {"mean_duration_seconds": 1.0}) == (
            None,
            None,
        )


@pytest.mark.security
class TestPaidCallsAreGated:
    def test_the_default_run_makes_no_live_call(self, results: dict[str, object]) -> None:
        assert results["live_jev"]["called"] is False  # type: ignore[index]

    def test_main_without_the_flag_never_probes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def explode() -> dict[str, object]:
            raise AssertionError("a live JEV call was made without the paid flag")

        monkeypatch.setattr("benchmarks.run.live_jev_probe", explode)
        stub = {
            "python": "0.0.0",
            "platform": "test",
            "recalculation_library_available": False,
            "jev_scenario": "approve",
            "repeats": 1,
            "warmup": "",
            "wall_clock_seconds": 0.0,
            "modes": {},
            "interpretation": {"findings": [], "caveats": []},
            "live_jev": {"called": False, "note": ""},
        }
        monkeypatch.setattr("benchmarks.run.run_benchmark", lambda **_kwargs: stub)
        output = tmp_path / "results.json"
        with redirect_stdout(io.StringIO()):
            assert main(["--output", str(output), "--only", "nothing"]) == 0
        assert json.loads(output.read_text())["live_jev"]["called"] is False

    def test_the_probe_requires_explicit_opt_in(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An unauthorised paid call aborts loudly.

        It does not return ``jev_called=False``. A guard that quietly reports
        "not called" is indistinguishable from one that fired on a run where no
        call was ever needed, which is exactly the ambiguity worth eliminating.

        The credential is set here rather than assumed. Without a key the adapter
        returns early — "not set", ``jev_called: False`` — because there is
        nothing to spend, so the ``PaidCallBlocked`` branch is never reached. This
        test originally relied on the developer's environment having a key, and so
        passed locally while failing in CI, where no credential exists by design.
        A test whose result depends on ambient state is not a test.
        """
        from app.contracts.config import ExcelPilotConfig
        from app.contracts.errors import PaidCallBlocked
        from app.contracts.pipeline import DecisionContext
        from app.decisions import HttpJevAdapter

        monkeypatch.setenv("TYPESAFE_API_KEY", "present-but-never-used")
        monkeypatch.setenv("OPENROUTER_API_KEY", "present-but-never-used")

        guarded = HttpJevAdapter(ExcelPilotConfig().jev, allow_paid_calls=False)
        with pytest.raises(PaidCallBlocked):
            guarded.decide(
                DecisionContext(
                    run_id="test",
                    task_summary="t",
                    sheet_count=1,
                    sheets_affected=["S"],
                    total_rows=1,
                    cells_to_change=1,
                    operation_kinds=["set_value"],
                )
            )

    def test_without_a_credential_it_reports_not_set_rather_than_raising(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The branch CI exercises, and the one that made the test above fragile.

        With no key there is no paid call to block, so the adapter returns a
        decision set saying so. Raising here would be wrong: there was nothing
        to authorise.

        Two distinct no-credential paths exist, and both are pinned because
        they say different things. With no key at all, provider resolution yields
        ``disabled``. With a provider configured explicitly but that provider's
        key missing, the adapter names the variable it wanted.
        """
        from app.contracts.config import ExcelPilotConfig
        from app.contracts.pipeline import DecisionContext
        from app.decisions import HttpJevAdapter

        monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

        context = DecisionContext(
            run_id="test",
            task_summary="t",
            sheet_count=1,
            sheets_affected=["S"],
            total_rows=1,
            cells_to_change=1,
            operation_kinds=["set_value"],
        )

        # No credential at all: nothing is configured to spend.
        no_key = HttpJevAdapter(ExcelPilotConfig().jev, allow_paid_calls=False)
        result = no_key.decide(context)
        assert result.jev_called is False
        assert result.error is not None
        assert "no credential present" in result.error

        # A provider is configured, but its key is absent. The adapter names the
        # variable rather than failing vaguely, which is what makes a support
        # question answerable.
        named = HttpJevAdapter(
            ExcelPilotConfig.model_validate({"jev": {"provider": "typesafe", "enabled": True}}).jev,
            allow_paid_calls=False,
        )
        missing = named.decide(context)
        assert missing.jev_called is False
        assert missing.error is not None
        assert "TYPESAFE_API_KEY" in missing.error and "is not set" in missing.error

    def test_live_probe_is_never_entered_by_import(self) -> None:
        """Importing the harness must not be capable of spending money."""
        source = Path(__file__).resolve().parent.parent / "benchmarks" / "run.py"
        text = source.read_text(encoding="utf-8")
        guard = text.count("if args.allow_paid_calls:")
        assert guard == 1, "the paid call must sit behind exactly one explicit check"


class TestReportShape:
    def test_results_document_is_json_serialisable(self, results: dict[str, object]) -> None:
        text = json.dumps(results, default=str)
        assert json.loads(text)["modes"]

    def test_report_records_the_environment(self, results: dict[str, object]) -> None:
        for key in ("python", "platform", "recalculation_library_available", "warmup"):
            assert key in results, f"{key} missing from the report"

    def test_no_secret_material_reaches_the_report(self, results: dict[str, object]) -> None:
        """The benchmark writes a file a user may share. It must be shareable."""
        text = json.dumps(results, default=str)
        for marker in ("sk-", "Bearer ", "api_key", "API_KEY"):
            assert marker not in text, f"{marker!r} appears in the benchmark output"


class TestScenariosAreSane:
    def test_names_are_unique(self) -> None:
        names = [s.name for s in SCENARIOS]
        assert len(names) == len(set(names))

    def test_every_scenario_states_why_it_exists(self) -> None:
        for scenario in SCENARIOS:
            assert scenario.rationale, f"{scenario.name} has no rationale"

    def test_every_scenario_check_is_implemented(self) -> None:
        """An unimplemented check would report ``None`` and read as a pass."""
        from benchmarks.run import _run_check

        implemented = {
            "duplicates_removed_exactly",
            "names_what_is_missing",
            "explains_why",
            "mentions_dedupe_key",
            "detects_preexisting_damage",
            "hidden_sheet_rule_fired",
        }
        for scenario in SCENARIOS:
            if not scenario.check:
                continue
            assert scenario.check in implemented, (
                f"{scenario.name} names an unimplemented check {scenario.check!r}"
            )
            assert _run_check.__doc__, "sanity"

    def test_modes_are_distinct(self) -> None:
        assert len({(m.use_planner, m.use_jev) for m in MODES}) == len(MODES)
        assert all(isinstance(m, Mode) for m in MODES)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))


class TestLiveCallIsNotLostOnRerun:
    """A routine re-run must never discard the record of a paid call.

    Found the hard way: running ``make bench`` overwrote ``live_jev`` with
    ``{"called": false}``, destroying the only evidence that the one authorised
    live JEV call had ever happened. Since such a call costs money and cannot be
    repeated without fresh authorisation, the record is not reproducible — losing
    it is losing the result permanently.
    """

    def _results(self, tmp_path: Path, live: dict[str, object]) -> Path:
        path = tmp_path / "results.json"
        path.write_text(json.dumps({"modes": {}, "live_jev": live}), encoding="utf-8")
        return path

    def test_a_recorded_call_is_carried_forward_verbatim(self, tmp_path: Path) -> None:
        from benchmarks.run import _carry_forward_live_call

        original = {
            "called": True,
            "succeeded": True,
            "elapsed_seconds": 1.404,
            "decisions": [{"question": "risk", "value": "medium"}],
        }
        path = self._results(tmp_path, original)
        carried = _carry_forward_live_call(path)

        assert carried["called"] is True
        assert carried["carried_forward"] is True
        assert carried["elapsed_seconds"] == 1.404
        assert carried["decisions"] == original["decisions"]

    def test_a_carried_record_is_labelled_so_it_cannot_masquerade(self, tmp_path: Path) -> None:
        from benchmarks.run import _carry_forward_live_call

        path = self._results(tmp_path, {"called": True, "elapsed_seconds": 1.0})
        carried = _carry_forward_live_call(path)
        assert "earlier run" in carried["note"]

    def test_nothing_is_invented_when_no_call_was_recorded(self, tmp_path: Path) -> None:
        from benchmarks.run import _carry_forward_live_call

        path = self._results(tmp_path, {"called": False, "note": "never ran"})
        carried = _carry_forward_live_call(path)
        assert carried["called"] is False
        assert "carried_forward" not in carried

    def test_a_missing_or_corrupt_file_is_not_fatal(self, tmp_path: Path) -> None:
        from benchmarks.run import _carry_forward_live_call

        assert _carry_forward_live_call(tmp_path / "nope.json")["called"] is False
        corrupt = tmp_path / "bad.json"
        corrupt.write_text("{not json", encoding="utf-8")
        assert _carry_forward_live_call(corrupt)["called"] is False

    def test_main_preserves_the_record_without_the_paid_flag(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import io
        from contextlib import redirect_stdout

        from benchmarks.run import main

        original = {"called": True, "elapsed_seconds": 1.404, "decisions": []}
        path = tmp_path / "results.json"
        path.write_text(
            json.dumps(
                {
                    "python": "0",
                    "platform": "t",
                    "recalculation_library_available": False,
                    "jev_scenario": "approve",
                    "repeats": 1,
                    "warmup": "",
                    "wall_clock_seconds": 0.0,
                    "modes": {},
                    "interpretation": {"findings": [], "caveats": []},
                    "live_jev": original,
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(
            "benchmarks.run.run_benchmark", lambda **_kw: json.loads(path.read_text())
        )
        with redirect_stdout(io.StringIO()):
            assert main(["--output", str(path), "--only", "nothing"]) == 0
        assert json.loads(path.read_text())["live_jev"]["elapsed_seconds"] == 1.404
