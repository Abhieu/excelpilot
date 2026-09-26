"""Regression tests for two defects found during the release-hardening audit.

Both were found by *doing* something rather than by reading code, and neither
was covered by the existing suite. They share a theme: ExcelPilot was silently
destroying workbook content while reporting success.

**1. The source snapshot was named ``source.snapshot.xlsx`` regardless of the
source's real extension.** The executor does not open the user's file; it opens
that snapshot. The reader decides ``keep_vba`` from the extension it sees, so a
``.xlsm`` snapshotted as ``.xlsx`` was loaded with ``keep_vba=False`` and
openpyxl then dropped ``xl/vbaProject.bin`` on save. A **read-only** run —
which policy deliberately permits on a macro workbook — produced an ``.xlsm``
output containing no macros, while the source stayed untouched and every other
check passed.

**2. Nothing compared the set of OOXML parts before and after.** openpyxl drops
comments, drawings, and macro-bound shapes on save, and a cell-level diff cannot
see that. Measured on a real workbook: seven comment parts and a drawing all
vanished while the run reported ``passed`` and zero anomalies.

The second defect is what the first was found *by*, so the new part-preservation
check is also the thing that keeps the first fixed.
"""

from __future__ import annotations

import shutil
import zipfile
from pathlib import Path

import pytest
from fixtures.vba import build_macro_workbook
from fixtures.workbooks import build

from app.app import RunOrchestrator
from app.contracts.base import UntrustedText
from app.contracts.config import ExcelPilotConfig
from app.decisions import MockJevAdapter
from app.storage import FileRunStore
from app.verification.structural import BENIGN_DROPPED_PARTS, check_no_parts_lost

#: A genuine drawing part, as a shape bound to a macro would be.
_DRAWING = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<xdr:wsDr xmlns:xdr="http://schemas.openxmlformats.org/drawingml/2006/'
    'spreadsheetDrawing"><xdr:twoCellAnchor>'
    '<xdr:sp><xdr:nvSpPr><xdr:cNvPr id="2" name="SomeMacro"/>'
    "</xdr:nvSpPr></xdr:sp>"
    "</xdr:twoCellAnchor></xdr:wsDr>"
)
_DRAWING_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
    'relationships"><Relationship Id="rIdD" Type="http://schemas.openxmlformats'
    '.org/officeDocument/2006/relationships/drawing" '
    'Target="../drawings/drawing1.xml"/></Relationships>'
)


def _with_drawing(source: Path) -> Path:
    """Add a drawing part and its relationship to a workbook, in place."""
    with zipfile.ZipFile(source) as original:
        items = [(item, original.read(item.filename)) for item in original.infolist()]
    scratch = source.with_suffix(".tmp")
    with zipfile.ZipFile(scratch, "w", zipfile.ZIP_DEFLATED) as out:
        for item, data in items:
            out.writestr(item, data)
        out.writestr("xl/drawings/drawing1.xml", _DRAWING)
        out.writestr("xl/worksheets/_rels/sheet1.xml.rels", _DRAWING_RELS)
    shutil.move(scratch, source)
    return source


def _parts(path: Path) -> set[str]:
    with zipfile.ZipFile(path) as archive:
        return set(archive.namelist())


def _vba(path: Path) -> bytes | None:
    with zipfile.ZipFile(path) as archive:
        if "xl/vbaProject.bin" not in archive.namelist():
            return None
        return archive.read("xl/vbaProject.bin")


@pytest.fixture
def macro(tmp_path: Path) -> Path:
    return build_macro_workbook(tmp_path / "m.xlsm")


@pytest.fixture
def plain(tmp_path: Path) -> Path:
    return build("monthly_sales", tmp_path / "plain.xlsx", rows=20)


class TestSnapshotKeepsTheSourceExtension:
    """Defect 1: a hardcoded ``.xlsx`` snapshot silently stripped macros."""

    def test_a_macro_source_is_snapshotted_as_macro(self, macro: Path, tmp_path: Path) -> None:
        store = FileRunStore(ExcelPilotConfig(workspace_root=str(tmp_path)))
        snapshot = store.snapshot("run-abc123", macro)
        assert snapshot.suffix == ".xlsm", (
            "a .xlsm snapshotted as .xlsx is loaded with keep_vba=False, which "
            "strips the macro project from every output"
        )
        assert snapshot.exists()

    def test_a_plain_source_is_snapshotted_as_xlsx(self, plain: Path, tmp_path: Path) -> None:
        store = FileRunStore(ExcelPilotConfig(workspace_root=str(tmp_path)))
        assert store.snapshot("run-plain", plain).suffix == ".xlsx"

    @pytest.mark.security
    def test_a_read_only_run_preserves_the_macro_project(self, macro: Path, tmp_path: Path) -> None:
        """The defect's actual symptom: a permitted run destroyed the macros.

        A read-only operation is deliberately allowed on a macro workbook. It
        must then produce an output that still *is* a macro workbook.
        """
        before = _vba(macro)
        assert before is not None

        run = RunOrchestrator(
            ExcelPilotConfig(workspace_root=str(tmp_path)),
            jev=MockJevAdapter("approve"),
        ).run(
            macro,
            UntrustedText("read the Inventory range", provenance="user_task"),
            approve=True,
        )

        assert run.outcome.value == "succeeded", f"a permitted read-only run failed: {run.error}"
        assert run.record.output_path is not None
        output = Path(run.record.output_path)
        assert output.suffix == ".xlsm"
        assert _vba(output) == before, "the output lost the macro project"

    @pytest.mark.security
    def test_the_macro_workbook_is_never_written_to(self, macro: Path, tmp_path: Path) -> None:
        """Fixing the snapshot must not weaken the source-protection guarantee."""
        from app.workbook import file_sha256

        before = file_sha256(macro)
        RunOrchestrator(
            ExcelPilotConfig(workspace_root=str(tmp_path)),
            jev=MockJevAdapter("approve"),
        ).run(
            macro,
            UntrustedText("read the Inventory range", provenance="user_task"),
            approve=True,
        )
        assert file_sha256(macro) == before

    def test_a_run_snapshot_can_be_found_without_knowing_its_suffix(
        self, macro: Path, tmp_path: Path
    ) -> None:
        """``verify --run`` looks the snapshot up from the run id alone."""
        config = ExcelPilotConfig(workspace_root=str(tmp_path))
        store = FileRunStore(config)
        store.snapshot("run-find", macro)
        found = store.find_snapshot("run-find")
        assert found is not None
        assert found.suffix == ".xlsm"
        assert _vba(found) is not None

    def test_a_run_with_no_snapshot_reports_none(self, tmp_path: Path) -> None:
        store = FileRunStore(ExcelPilotConfig(workspace_root=str(tmp_path)))
        assert store.find_snapshot("run-absent") is None


class TestLostPartsAreDetected:
    """Defect 2: dropped OOXML parts were invisible to every check."""

    def test_a_lost_drawing_is_reported(self, plain: Path, tmp_path: Path) -> None:
        source = _with_drawing(plain)
        assert "xl/drawings/drawing1.xml" in _parts(source)

        output = tmp_path / "out.xlsx"
        from app.workbook.reader import load_workbook, save_atomic

        workbook = load_workbook(source)
        save_atomic(workbook, output)
        workbook.close()

        result = check_no_parts_lost(source, output)
        assert result.status.value == "failed"
        assert "xl/drawings/drawing1.xml" in result.details["lost_parts"]

    def test_the_message_names_the_parts_and_says_the_source_is_intact(
        self, plain: Path, tmp_path: Path
    ) -> None:
        source = _with_drawing(plain)
        output = tmp_path / "out.xlsx"
        from app.workbook.reader import load_workbook, save_atomic

        workbook = load_workbook(source)
        save_atomic(workbook, output)
        workbook.close()

        message = check_no_parts_lost(source, output).message
        assert "xl/drawings/drawing1.xml" in message
        assert "source workbook is unmodified" in message
        assert "discard it" in message, "the operator must be told what to do"

    def test_the_shared_string_cache_is_not_counted_as_a_loss(
        self, plain: Path, tmp_path: Path
    ) -> None:
        """Otherwise every run would fail, which is how a check stops being read."""
        from app.workbook.reader import load_workbook, save_atomic

        with zipfile.ZipFile(plain) as archive:
            has_cache = "xl/sharedStrings.xml" in archive.namelist()
        output = tmp_path / "out.xlsx"
        workbook = load_workbook(plain)
        save_atomic(workbook, output)
        workbook.close()

        result = check_no_parts_lost(plain, output)
        assert "xl/sharedStrings.xml" not in result.details.get("lost_parts", [])
        if has_cache and "xl/sharedStrings.xml" not in _parts(output):
            assert result.status.value == "passed", (
                "the shared-string cache is a value-preserving rebuild, not a loss"
            )

    def test_the_benign_allowlist_is_explicit_and_narrow(self) -> None:
        """Two caches, and only two."""
        assert frozenset({"xl/sharedStrings.xml", "xl/calcChain.xml"}) == BENIGN_DROPPED_PARTS

    def test_without_a_reference_the_check_does_not_claim_an_all_clear(self, plain: Path) -> None:
        """No source, no comparison — and it must say so rather than pass silently.

        A standalone ``verify`` with no ``--run`` has nothing to compare against.
        Reporting that as "parts preserved" would be a false all-clear.
        """
        result = check_no_parts_lost(None, plain)  # type: ignore[arg-type]
        assert result.status.value == "passed"
        assert result.details["checked"] is False
        assert "not an all-clear" in result.message

    def test_a_missing_reference_is_treated_as_no_reference(
        self, plain: Path, tmp_path: Path
    ) -> None:
        result = check_no_parts_lost(tmp_path / "does-not-exist.xlsx", plain)
        assert result.details["checked"] is False

    @pytest.mark.security
    def test_a_run_on_a_workbook_with_a_drawing_fails_verification(
        self, plain: Path, tmp_path: Path
    ) -> None:
        """The end-to-end symptom: succeeded + passed while losing content.

        Before the check existed this run reported ``succeeded``, ``passed``,
        zero anomalies and ``structural_change: false`` — while silently
        discarding the drawing. A user would have taken that as a clean bill of
        health.
        """
        source = _with_drawing(plain)
        workspace = tmp_path / "ws"
        workspace.mkdir()
        shutil.copy2(source, workspace / "withdrawing.xlsx")

        run = RunOrchestrator(
            ExcelPilotConfig(workspace_root=str(workspace)),
            jev=MockJevAdapter("approve"),
        ).run(
            workspace / "withdrawing.xlsx",
            UntrustedText("normalise the Customer column on Sales", provenance="user_task"),
            approve=True,
        )

        assert run.verification is not None
        assert run.verification.passed is False, "a run that lost workbook content reported success"
        assert run.outcome.value == "failed"

    def test_a_normal_workbook_still_passes_the_new_check(
        self, plain: Path, tmp_path: Path
    ) -> None:
        """The check must not turn ordinary runs into failures."""
        workspace = tmp_path / "ws"
        workspace.mkdir()
        shutil.copy2(plain, workspace / "s.xlsx")

        run = RunOrchestrator(
            ExcelPilotConfig(workspace_root=str(workspace)),
            jev=MockJevAdapter("approve"),
        ).run(
            workspace / "s.xlsx",
            UntrustedText("normalise the Customer column on Sales", provenance="user_task"),
            approve=True,
        )
        assert run.outcome.value == "succeeded"
        parts_check = next(
            c for c in run.verification.structural if c.name == "ooxml_parts_preserved"
        )
        assert parts_check.status.value == "passed"
        assert parts_check.details["checked"] is True
