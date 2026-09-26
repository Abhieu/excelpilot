"""Exercise the orchestrator end to end, on a real run.

A developer smoke test that walks the whole pipeline and prints what happened at
each stage. Runs unattended: pass ``--keep`` to preserve the temporary workspace
for inspection.

Not part of the pytest suite; ``tests/test_e2e.py`` holds the real end-to-end
tests with assertions.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fixtures.workbooks import build  # noqa: E402

from app.app import RunOrchestrator  # noqa: E402
from app.contracts.base import UntrustedText  # noqa: E402
from app.contracts.config import ExcelPilotConfig  # noqa: E402
from app.decisions import MockJevAdapter  # noqa: E402

TASK = "normalise the Customer column, remove duplicate invoices, and create a summary by Region"


def main() -> int:
    workspace = Path(tempfile.mkdtemp(prefix="ep-smoke-"))
    source = build("monthly_sales", workspace / "monthly_sales.xlsx", rows=40)
    before = source.read_bytes()

    config = ExcelPilotConfig(workspace_root=str(workspace))
    orchestrator = RunOrchestrator(config, jev=MockJevAdapter("approve"))

    print("=" * 72)
    print("1. INSPECT")
    inspection = orchestrator.inspect(source)
    print(f"   sheets      : {inspection.sheet_names}")
    print(f"   rows        : {inspection.total_rows}")
    print(f"   formulas    : {inspection.total_formulas}")
    print(f"   hidden      : {inspection.hidden_sheet_count}")
    print(f"   sensitivity : {inspection.sensitivity.level}")
    print(f"   hash        : {inspection.content_hash[:16]}")

    print()
    print("2. PLAN + POLICY (no execution)")
    plan, insp, jev, policy = orchestrator.plan(source, UntrustedText(TASK, provenance="user_task"))
    print(f"   operations  : {plan.operation_kinds}")
    print(f"   intent      : {plan.understanding.intent_summary}")
    print(f"   jev         : {[f'{d.question}={d.value}' for d in (jev.decisions if jev else [])]}")
    print(f"   policy      : {policy.explain()}")

    print()
    print("3. DRY RUN")
    result = orchestrator.run(source, UntrustedText(TASK, provenance="user_task"), dry_run=True)
    print(f"   outcome     : {result.outcome.value}")
    print(f"   error       : {result.error}")
    print(f"   source hash : {result.record.source_hash[:16]}")
    print(f"   outputs     : {list((workspace).glob('*.xlsx'))}")

    print()
    print("4. FULL RUN (approved)")
    result = orchestrator.run(
        source,
        UntrustedText(TASK, provenance="user_task"),
        approve=True,
        jev=MockJevAdapter("approve"),
    )
    print(f"   outcome     : {result.outcome.value}")
    print(f"   state       : {result.state.value}")
    if result.error:
        print(f"   error       : {result.error}")
    if result.policy:
        print(f"   policy      : {result.policy.explain()}")
    if result.approval:
        print(f"   approval    : {result.approval.status.value}")
    if result.execution:
        print(f"   executed    : {result.execution.totals()}")
    if result.verification:
        print(
            f"   verified    : {result.verification.status.value} (passed={result.verification.passed})"
        )
        print(f"   recalculated: {result.verification.recalculated}")
        for anomaly in result.verification.anomalies[:4]:
            print(f"   anomaly     : [{anomaly.source.value}] {anomaly.message}")
    if result.output_path:
        print(f"   output      : {Path(result.output_path).name}")

    print()
    print("5. SAFETY: source untouched?")
    print(f"   bytes identical: {source.read_bytes() == before}")

    print()
    print("6. AUDIT TRAIL")
    run_dir = config.runs_path() / result.run_id
    audit_lines = (run_dir / "audit.jsonl").read_text().strip().splitlines()
    print(f"   events      : {len(audit_lines)}")
    for line in audit_lines[:12]:
        import json

        event = json.loads(line)
        print(f"     {event['seq']:>3} {event['actor']:<7} {event['event_type']}")

    print()
    print("7. REPORT")
    print("   " + (result.report or "(none)").replace("\n", "\n   ")[:1200])

    # Non-interactive by default. `make smoke` runs this with no terminal, and
    # an unguarded `input()` made the target fail with EOFError on every CI run
    # and every non-tty shell — a smoke test that cannot pass unattended is not
    # a smoke test. Pass --keep to preserve the workspace instead.
    keep = "--keep" in sys.argv[1:]
    if keep:
        print(f"\n   workspace kept at {workspace}")
    else:
        shutil.rmtree(workspace, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
