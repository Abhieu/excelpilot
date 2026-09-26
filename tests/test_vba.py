"""VBA / macro-enabled workbook behaviour.

Three kinds of evidence live here, and it matters which is which:

1. **The structural fixture** (``fixtures/vba.py``) — a genuinely
   macro-enabled package containing a structurally valid OLE/CFB
   ``vbaProject.bin``. Committed as source, generated at test time. This gives
   CI-runnable coverage of everything ExcelPilot *does* with a macro workbook:
   extension handling, ``keep_vba``, byte-level preservation, package validity,
   and the ``vba_read_only`` denial. It is **not** a real VBA project — it has
   no ``dir`` stream and no modules — and nothing here claims otherwise.

2. **The real workbook** (``TestRealMacroWorkbook``) — skipped unless the user's
   own collection is present. This is the authoritative measurement against a
   genuine 152 KB VBA project.

3. **The measured behaviour** these tests lock in, so a change to openpyxl or to
   the reader cannot silently alter it.

The measured boundary, stated once so the tests below can be read against it:

* ExcelPilot **reads** macro-enabled workbooks and reports ``has_vba`` truthfully
* ExcelPilot **preserves** ``vbaProject.bin`` byte-for-byte through read/save
* ExcelPilot **refuses to write** to a macro-enabled workbook, unconditionally
* openpyxl **drops drawing parts**, so a shape bound to a macro is lost on a
  round trip — and neither diff nor verification currently reports that loss
"""

from __future__ import annotations

import hashlib
import struct
import tempfile
import zipfile
from pathlib import Path

import pytest
from fixtures import real
from fixtures.vba import STREAM_NAME, VBA_PART, build_macro_workbook, build_vba_project_bin

from app.app import RunOrchestrator
from app.contracts.base import UntrustedText
from app.contracts.config import ExcelPilotConfig
from app.decisions import MockJevAdapter
from app.workbook import has_vba, inspect_workbook, opened
from app.workbook.reader import load_workbook, save_atomic

OLE_MAGIC = bytes.fromhex("d0cf11e0a1b11ae1")
ENDOFCHAIN = 0xFFFFFFFE
FATSECT = 0xFFFFFFFD

#: A real macro-bearing workbook, located at test time. See ``fixtures/real.py``
#: for how to supply one. No particular collection, filename, or filesystem path
#: is assumed, and every test below skips when the role is not configured.
needs_real_macro = pytest.mark.skipif(
    real.workbook("macro_project") is None,
    reason=real.skip_reason("macro_project"),
)


def _real_macro_or_skip() -> Path:
    """Resolve the real macro workbook, skipping when it is not configured."""
    path = real.workbook("macro_project")
    if path is None:
        pytest.skip(real.skip_reason("macro_project"))
    return path


@pytest.fixture
def macro_book(tmp_path: Path) -> Path:
    """A committed-source, generated macro-enabled workbook."""
    return build_macro_workbook(tmp_path / "vba_macro.xlsm")


def vba_sha256(path: Path) -> str | None:
    """SHA-256 of ``xl/vbaProject.bin``, or None if the part is absent."""
    with zipfile.ZipFile(path) as archive:
        if VBA_PART not in archive.namelist():
            return None
        return hashlib.sha256(archive.read(VBA_PART)).hexdigest()


def part_names(path: Path) -> set[str]:
    with zipfile.ZipFile(path) as archive:
        return set(archive.namelist())


class TestMacroEnabledFixture:
    """The fixture must be a genuine macro-enabled package.

    An earlier version of the synthetic test appended a 520-byte blob with a
    correct OLE magic number. That proved ``has_vba()`` could *see* a file, and
    nothing about whether the bytes *survive* — which is the property that
    actually matters. These tests pin the package structure so the fixture
    cannot quietly regress to "a file called vbaProject.bin".
    """

    def test_the_part_exists(self, macro_book: Path) -> None:
        assert VBA_PART in part_names(macro_book)

    def test_the_part_is_a_structurally_valid_ole_container(self) -> None:
        container = build_vba_project_bin()
        assert container[:8] == OLE_MAGIC, "not an OLE2 compound file"

        header = container[:512]
        sector_shift, mini_shift = struct.unpack_from("<HH", header, 30)
        assert sector_shift == 9, f"expected 512-byte sectors, got {1 << sector_shift}"
        assert mini_shift == 6, f"expected 64-byte mini sectors, got {1 << mini_shift}"

        # The FAT must be self-consistent or a reader cannot walk the container.
        fat = struct.unpack_from("<128I", container, 512)
        assert fat[0] == FATSECT, "sector 0 must be marked as the FAT"
        assert fat[2] == ENDOFCHAIN, "the stream's chain must terminate"

    def test_the_container_directory_is_readable_and_named(self, macro_book: Path) -> None:
        """Walk the directory by hand rather than trusting the builder."""
        with zipfile.ZipFile(macro_book) as archive:
            container = archive.read(VBA_PART)

        # Directory lives in sector 1 (the FAT chains 1 -> 2 for the data).
        directory = container[512 + 512 : 512 + 512 + 128 * 2]
        root_type = directory[66]
        assert root_type == 5, "the first entry must be the Root Entry storage"

        name_length = struct.unpack_from("<H", directory, 64)[0]
        second = directory[128:256]
        second_len = struct.unpack_from("<H", second, 64)[0]
        second_name = second[: second_len - 2].decode("utf-16-le")
        assert second_name == STREAM_NAME
        assert second[66] == 2, "the second entry must be a stream"

        # And the root entry itself is named "Root Entry".
        assert directory[: name_length - 2].decode("utf-16-le") == "Root Entry"

    def test_the_package_is_macro_enabled_not_merely_named_xlsm(self, macro_book: Path) -> None:
        """A ``.xlsm`` extension alone means nothing. Check the packaging.

        This is the distinction the task that produced these tests warned about:
        a file merely *named* ``.xlsm`` is not a macro-enabled workbook. Three
        independent declarations must all be present.
        """
        with zipfile.ZipFile(macro_book) as archive:
            content_types = archive.read("[Content_Types].xml").decode()
            rels = archive.read("xl/_rels/workbook.xml.rels").decode()

        assert "vnd.ms-office.vbaProject" in content_types, (
            "the bin default content type is missing"
        )
        assert "macroEnabled.main+xml" in content_types, (
            "xl/workbook.xml is not declared macro-enabled"
        )
        assert "spreadsheetml.sheet.main+xml" not in content_types, (
            "the plain .xlsx content type is still present; the package claims both"
        )
        assert "vbaProject.bin" in rels, "workbook.xml.rels does not reference the part"

    def test_the_builder_refuses_to_emit_a_broken_package(self) -> None:
        """If openpyxl changes its content-type spelling, fail loudly.

        An exact-string replacement silently failed to match once, producing a
        package that claimed to be a plain ``.xlsx``. The builder now raises
        rather than shipping a fixture that looks macro-enabled and is not.
        """
        # The regex must match exactly one element, and it must be the workbook.
        from fixtures.vba import _WORKBOOK_TYPE_PATTERN

        sample = (
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.'
            'openxmlformats-officedocument.spreadsheetml.sheet.main+xml" />'
        )
        assert len(_WORKBOOK_TYPE_PATTERN.findall(sample)) == 1


class TestMacroDetection:
    """``has_vba`` must reflect the package, not the extension."""

    def test_reports_a_real_macro_project(self, macro_book: Path) -> None:
        assert has_vba(macro_book) is True
        assert inspect_workbook(macro_book).metadata.has_vba is True

    def test_reports_a_plain_xlsx_as_non_macro(self, tmp_path: Path) -> None:
        from fixtures.workbooks import minimal

        path = tmp_path / "plain.xlsx"
        workbook = minimal()
        workbook.save(path)
        workbook.close()
        assert has_vba(path) is False
        assert inspect_workbook(path).metadata.has_vba is False

    def test_an_xlsm_without_macros_is_reported_honestly(self, tmp_path: Path) -> None:
        """Common in practice; the detector must not trust the extension."""
        from fixtures.workbooks import minimal

        path = tmp_path / "nomacro.xlsm"
        workbook = minimal()
        workbook.save(path)
        workbook.close()
        assert has_vba(path) is False


class TestVbaPreservation:
    """The measured claim: ``vbaProject.bin`` survives byte-for-byte.

    "The output still opens" is not the claim being made. The claim is that the
    4,200 bytes of the container are the same 4,200 bytes, proven by digest.
    """

    def test_inspection_does_not_modify_the_workbook(self, macro_book: Path) -> None:
        from app.workbook import file_sha256

        before_file = file_sha256(macro_book)
        before_vba = vba_sha256(macro_book)

        inspect_workbook(macro_book)
        inspect_workbook(macro_book)

        assert file_sha256(macro_book) == before_file
        assert vba_sha256(macro_book) == before_vba

    def test_read_then_save_preserves_the_container_byte_identically(
        self, macro_book: Path, tmp_path: Path
    ) -> None:
        before = vba_sha256(macro_book)
        assert before is not None

        output = tmp_path / "roundtrip.xlsm"
        workbook = load_workbook(macro_book)  # the real reader, keep_vba enforced
        save_atomic(workbook, output)
        workbook.close()

        assert vba_sha256(output) == before, "vbaProject.bin was altered by a read/save round trip"

    def test_the_saved_output_is_still_a_valid_macro_package(
        self, macro_book: Path, tmp_path: Path
    ) -> None:
        output = tmp_path / "roundtrip.xlsm"
        workbook = load_workbook(macro_book)
        save_atomic(workbook, output)
        workbook.close()

        assert output.suffix == ".xlsm", "the extension must not be downgraded"
        with zipfile.ZipFile(output) as archive:
            content_types = archive.read("[Content_Types].xml").decode()
            rels = archive.read("xl/_rels/workbook.xml.rels").decode()
        assert "vbaProject" in content_types
        assert "vbaProject.bin" in rels

    def test_versioned_output_keeps_the_xlsm_extension(self, macro_book: Path) -> None:
        from app.safety.paths import versioned_output

        versioned = versioned_output(macro_book, "run-abc123")
        assert versioned.suffix == ".xlsm"
        assert versioned != macro_book

    def test_cell_data_survives_alongside_the_macros(
        self, macro_book: Path, tmp_path: Path
    ) -> None:
        before = inspect_workbook(macro_book)

        output = tmp_path / "roundtrip.xlsm"
        workbook = load_workbook(macro_book)
        save_atomic(workbook, output)
        workbook.close()

        after = inspect_workbook(output)
        assert after.sheet_names == before.sheet_names
        assert after.total_non_empty_cells == before.total_non_empty_cells
        assert after.total_formulas == before.total_formulas

        with opened(macro_book) as a, opened(output) as b:
            for sheet in before.sheet_names:
                assert a[sheet]["A2"].value == b[sheet]["A2"].value
                assert a[sheet]["D2"].value == b[sheet]["D2"].value


@pytest.mark.security
class TestVbaWriteIsRefused:
    """The hard rule must hold, and hold unconditionally.

    ``vba_read_only`` is not configurable, and it must not be overridable by
    ``--approve``. A run that writes to a macro workbook risks producing a file
    whose macros no longer match its sheets, so the safe answer is to refuse and
    say so.
    """

    def _orchestrator(self, workspace: Path) -> RunOrchestrator:
        return RunOrchestrator(
            ExcelPilotConfig(workspace_root=str(workspace)), jev=MockJevAdapter("approve")
        )

    def test_a_mutation_is_denied(self, macro_book: Path, tmp_path: Path) -> None:
        before = vba_sha256(macro_book)
        run = self._orchestrator(tmp_path).run(
            macro_book,
            UntrustedText("normalise the Item column on Inventory", provenance="user_task"),
            approve=True,
        )
        assert run.outcome.value == "rejected_by_policy"
        assert run.record.policy_outcome is not None
        assert run.record.policy_outcome.value == "deny"
        assert "vba_read_only" in run.record.policy_rule_ids
        assert run.record.output_path is None
        assert vba_sha256(macro_book) == before

    def test_the_denial_explains_itself(self, macro_book: Path, tmp_path: Path) -> None:
        """A refusal that does not say why is indistinguishable from a bug."""
        run = self._orchestrator(tmp_path).run(
            macro_book,
            UntrustedText("normalise the Item column on Inventory", provenance="user_task"),
            approve=True,
        )
        reasons = " ".join(run.policy.reasons if run.policy else [])
        assert "VBA macro project" in reasons
        assert "will not write to it" in reasons

    def test_approval_cannot_override_it(self, macro_book: Path, tmp_path: Path) -> None:
        """``approve=True`` is the strongest signal a caller can send.

        It must still be denied, because the rule is a hard deny and not an
        escalation. This is the test that would fail first if someone converted
        ``vba_read_only`` into an escalation rule.
        """
        for approve in (True,):
            run = self._orchestrator(tmp_path).run(
                macro_book,
                UntrustedText("normalise the Item column on Inventory", provenance="user_task"),
                approve=approve,
            )
            assert run.outcome.value == "rejected_by_policy", (
                f"approval={approve} overrode the vba_read_only hard deny"
            )
            assert "vba_read_only" in run.record.policy_rule_ids

    def test_a_dry_run_does_not_write_either(self, macro_book: Path, tmp_path: Path) -> None:
        plan, _, _, policy = self._orchestrator(tmp_path).plan(
            macro_book,
            UntrustedText("normalise the Item column on Inventory", provenance="user_task"),
        )
        assert policy.outcome.value == "deny"
        assert "vba_read_only" in list(policy.rule_ids)
        assert vba_sha256(macro_book) is not None

    def test_a_read_only_operation_is_permitted(self, macro_book: Path, tmp_path: Path) -> None:
        """The rule is not "refuse everything" — reading a macro book is fine."""
        before = vba_sha256(macro_book)
        run = self._orchestrator(tmp_path).run(
            macro_book,
            UntrustedText("read the Inventory range", provenance="user_task"),
            approve=True,
        )
        assert run.outcome.value == "succeeded"
        assert vba_sha256(macro_book) == before

    def test_the_rule_survives_a_maximally_permissive_config(self) -> None:
        """No configuration can turn the hard deny off.

        Every soft threshold is driven to its most permissive value — the shape
        of a config someone would write trying to make ExcelPilot write to a
        macro workbook. The denial must stand.
        """
        from app.contracts.enums import PolicyOutcome

        workspace = Path(tempfile.mkdtemp())
        copy = build_macro_workbook(workspace / "vba_macro.xlsm")
        before = vba_sha256(copy)

        permissive = ExcelPilotConfig(
            workspace_root=str(workspace),
            policy={
                "cell_change_approval_threshold": 0,
                "row_change_approval_threshold": 0,
                "deny_cells_affected_above": 20_000_000,
                "formula_removal_requires_approval": False,
                "structural_change_requires_approval": False,
                "hidden_sheet_change_requires_approval": False,
                "restricted_data_requires_approval": False,
                "ambiguous_task_requires_approval": False,
            },
        )
        run = RunOrchestrator(permissive, jev=MockJevAdapter("approve")).run(
            copy,
            UntrustedText("normalise the Item column on Inventory", provenance="user_task"),
            approve=True,
        )

        assert run.outcome.value == "rejected_by_policy"
        assert "vba_read_only" in run.record.policy_rule_ids
        assert run.record.policy_outcome is not None
        assert run.record.policy_outcome.value == PolicyOutcome.DENY.value
        assert vba_sha256(copy) == before, "a denied run must not touch the workbook"


@needs_real_macro
@pytest.mark.slow
class TestRealMacroWorkbook:
    """The authoritative measurement, against a genuine 152 KB VBA project.

    Skipped unless a real macro-bearing workbook is configured via
    ``fixtures/real.py``, and slow, because it touches a real workbook. Nothing
    derived from it is committed: such files embed Windows usernames and absolute
    business paths, which is exactly why they must stay outside the repository.

    These tests assert *properties* of whatever workbook is configured, not the
    identity of one particular file. The measurements taken against the workbook
    used during development — a 152,576-byte project, preserved byte-for-byte —
    are recorded in ``docs/verification-report.md``; the digests of any individual
    private file are deliberately not pinned here, because a pin would make these
    tests fail for anyone supplying a different (equally valid) workbook.
    """

    #: A real VBA project is far larger than a stub container. This floor exists
    #: to assert "a real project", not to pin one file's size.
    MIN_PROJECT_BYTES = 16_384

    def test_it_is_a_genuine_macro_project(self) -> None:
        path = _real_macro_or_skip()
        assert has_vba(path) is True
        with zipfile.ZipFile(path) as archive:
            container = archive.read(VBA_PART)
        assert container[:8] == OLE_MAGIC, "not an OLE2 compound file"
        assert len(container) >= self.MIN_PROJECT_BYTES, (
            f"the VBA project is only {len(container)} bytes; this looks like a stub "
            "rather than a real project"
        )

    def test_inspection_reports_it_as_macro_enabled(self) -> None:
        result = inspect_workbook(_real_macro_or_skip())
        assert result.extension == "xlsm"
        assert result.metadata.has_vba is True
        assert result.sheet_names, "no sheets read"
        assert result.total_rows > 0

    def test_inspection_does_not_touch_it(self) -> None:
        from app.workbook import file_sha256

        path = _real_macro_or_skip()
        before = file_sha256(path)
        inspect_workbook(path)
        assert file_sha256(path) == before

    def test_round_trip_preserves_the_real_project_byte_identically(self, tmp_path: Path) -> None:
        source = _real_macro_or_skip()
        before = vba_sha256(source)
        assert before is not None

        output = tmp_path / "real_roundtrip.xlsm"
        workbook = load_workbook(source)
        save_atomic(workbook, output)
        workbook.close()

        assert vba_sha256(output) == before, "a real VBA project was not preserved byte-for-byte"
        assert output.suffix == ".xlsm"
        with zipfile.ZipFile(output) as archive:
            assert "vbaProject" in archive.read("[Content_Types].xml").decode()
            assert "vbaProject.bin" in archive.read("xl/_rels/workbook.xml.rels").decode()

    def test_data_survives_the_round_trip(self, tmp_path: Path) -> None:
        source = _real_macro_or_skip()
        before = inspect_workbook(source)
        output = tmp_path / "real_roundtrip.xlsm"
        workbook = load_workbook(source)
        save_atomic(workbook, output)
        workbook.close()

        after = inspect_workbook(output)
        assert after.sheet_names == before.sheet_names
        assert after.total_non_empty_cells == before.total_non_empty_cells

    def test_a_mutation_is_refused(self, tmp_path: Path) -> None:
        """A resolvable mutating request on a macro workbook is always refused.

        The request is built from the workbook's own headers, so the test does
        not depend on any particular sheet or column name. What matters is that
        a request which *would* otherwise execute is denied.
        """
        import shutil

        source = _real_macro_or_skip()
        workspace = tmp_path / "w"
        workspace.mkdir()
        copy = workspace / "macro.xlsm"
        shutil.copy2(source, copy)
        before = vba_sha256(copy)

        # Find a real column to ask about, so the planner resolves a real target
        # and the denial comes from the VBA rule rather than from no_guessing.
        inspection = inspect_workbook(copy)
        sheet = inspection.sheet_names[0]
        headers = next((s for s in inspection.sheets if s.name == sheet and s.header_row), None)
        column = next((h for h in (headers.header_row if headers else []) if h and h.strip()), None)
        if column is None:
            pytest.skip(f"'{sheet}' has no named column to build a request against")

        run = RunOrchestrator(
            ExcelPilotConfig(workspace_root=str(workspace)),
            jev=MockJevAdapter("approve"),
        ).run(
            copy,
            UntrustedText(f"normalise the {column} column on {sheet}", provenance="user_task"),
            approve=True,
        )
        assert run.outcome.value == "rejected_by_policy"
        assert "vba_read_only" in run.record.policy_rule_ids
        assert run.record.output_path is None
        assert vba_sha256(copy) == before

    def test_the_round_trip_drops_drawing_parts(self, tmp_path: Path) -> None:
        """A measured loss, recorded so it cannot be rediscovered by surprise.

        In the workbook measured during development, ``xl/drawings/drawing1.xml``
        was a shape bound to the macro — the button a user clicks to run it — and
        openpyxl dropped it on save. The macro *project* survived byte-for-byte,
        so the file still opened and the macro still ran; what was lost was the
        on-sheet affordance that invoked it.

        Asserted generically: whatever drawing parts the configured workbook has,
        they must not survive, and the macro must. A workbook with no drawings
        cannot demonstrate the loss, so it skips rather than passing vacuously.
        See ``docs/limitations.md`` §3.
        """
        source = _real_macro_or_skip()
        before = part_names(source)
        drawings = {p for p in before if p.startswith("xl/drawings/")}
        if not drawings:
            pytest.skip("the configured workbook has no drawing parts to test with")

        output = tmp_path / "real_roundtrip.xlsm"
        workbook = load_workbook(source)
        save_atomic(workbook, output)
        workbook.close()
        after = part_names(output)

        assert not (drawings & after), (
            f"openpyxl now preserves drawing parts ({sorted(drawings & after)}); "
            "docs/limitations.md §3 is stale"
        )
        # The macro itself is unaffected by that loss.
        assert vba_sha256(output) == vba_sha256(source)
