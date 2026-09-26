"""Workbook engine tests.

Two kinds of evidence:

1. **Synthetic fixtures** built by ``fixtures/workbooks.py``, so conditions
   (hidden sheets, formula damage, injection payloads, VBA) are deliberate.
2. **Real workbooks**, when configured. These are the real fidelity bar — a 6-sheet
   workbook with 69,221 formulas, and a 37,883x120 sheet. They are located at test
   time via ``fixtures/real.py``; no particular collection, filename, or
   filesystem path is assumed, and every real-file test skips cleanly when none is
   configured, so the suite stays self-contained.
"""

from __future__ import annotations

import datetime as dt
import zipfile
from pathlib import Path

import pytest
from fixtures import real
from fixtures.workbooks import build, minimal, monthly_sales

from app.contracts.config import LimitsConfig
from app.contracts.errors import LimitExceeded, TargetResolutionError, WorkbookSecurityError
from app.contracts.operations import Target
from app.workbook import (
    UnsupportedFormat,
    content_hash,
    file_sha256,
    has_vba,
    inspect_workbook,
    normalise_value,
    opened,
    read_table,
    resolve_target,
    save_atomic,
)
from app.workbook.limits import check_archive_integrity, check_extension

#: Real workbooks, located at test time rather than assumed to be at a fixed
#: path. See ``fixtures/real.py`` for how to supply them. Nothing about any
#: particular collection, filename, or filesystem layout is encoded here.
_ROLES = ("large", "wide", "macro_extension")

REAL_WORKBOOKS: list[Path] = [p for p in (real.workbook(r) for r in _ROLES) if p is not None]

needs_real = pytest.mark.skipif(
    not REAL_WORKBOOKS,
    reason=real.skip_reason("large"),
)


def _role_or_skip(role: str) -> Path:
    """Resolve a role, skipping the test when that workbook is not configured."""
    path = real.workbook(role)
    if path is None:
        pytest.skip(real.skip_reason(role))
    return path


@pytest.fixture
def sales(tmp_path: Path) -> Path:
    return build("monthly_sales", tmp_path / "monthly_sales.xlsx", rows=30)


@pytest.fixture
def small(tmp_path: Path) -> Path:
    workbook = minimal()
    path = tmp_path / "small.xlsx"
    workbook.save(path)
    return path


class TestValueNormalisation:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (None, ""),
            (True, "TRUE"),
            (False, "FALSE"),
            (5, "5"),
            (5.0, "5"),
            (-0.0, "0"),
            (3.14, "3.14"),
            (dt.date(2026, 1, 2), "2026-01-02"),
            (dt.datetime(2026, 1, 2, 3, 4), "2026-01-02T03:04:00"),
            ("text", "text"),
        ],
    )
    def test_normalisation(self, value: object, expected: str) -> None:
        assert normalise_value(value) == expected

    def test_integral_float_matches_int(self) -> None:
        """ExcelPilot writes 5 and 5.0 interchangeably; they must not diff."""
        assert normalise_value(5) == normalise_value(5.0)

    def test_nan_and_inf_do_not_crash(self) -> None:
        assert normalise_value(float("nan")) == "NaN"
        assert normalise_value(float("inf")) == "INF"
        assert normalise_value(float("-inf")) == "-INF"


class TestContentHash:
    def test_order_independent(self) -> None:
        forward = [("S", "A1", "1", "n", "General", "0")]
        backward = list(reversed(forward))
        assert content_hash(forward) == content_hash(backward)

    def test_position_sensitive(self) -> None:
        a = [("S", "A1", "1", "n", "General", "0")]
        b = [("S", "A2", "1", "n", "General", "0")]
        assert content_hash(a) != content_hash(b)

    def test_value_sensitive(self) -> None:
        a = [("S", "A1", "1", "n", "General", "0")]
        b = [("S", "A1", "2", "n", "General", "0")]
        assert content_hash(a) != content_hash(b)

    def test_sheet_sensitive(self) -> None:
        a = [("S1", "A1", "1", "n", "General", "0")]
        b = [("S2", "A1", "1", "n", "General", "0")]
        assert content_hash(a) != content_hash(b)


class TestInspection:
    def test_minimal_workbook(self, small: Path) -> None:
        result = inspect_workbook(small)
        assert result.extension == "xlsx"
        assert result.sheet_names == ["Sheet"]
        assert result.total_non_empty_cells == 4
        assert result.content_hash == file_sha256(small)

    def test_monthly_sales_structure(self, sales: Path) -> None:
        result = inspect_workbook(sales)
        assert set(result.sheet_names) == {"Sales", "Summary", "_Lookup"}
        sales_sheet = result.sheet("sales")
        assert sales_sheet is not None
        assert sales_sheet.header_row[1] == "Customer"
        # 30 generated rows + 2 duplicated rows = 32, each with an Amount formula.
        assert sales_sheet.formula_count == 32
        # 32 Amount formulas + 3 Summary formulas (two of the other Summary
        # metrics are literals, so they are not formulas).
        assert result.total_formulas == 35
        assert result.hidden_sheet_count == 1

    def test_hidden_and_very_hidden_are_distinguished(self, tmp_path: Path) -> None:
        path = build("hidden_sheets", tmp_path / "hidden.xlsx")
        result = inspect_workbook(path)
        assert result.sheet("Config").state == "hidden"
        assert result.sheet("_AuditState").state == "veryHidden"
        assert result.sheet("_AuditState").is_protected is True
        assert result.hidden_sheet_count == 2

    def test_sheet_lookup_is_case_insensitive(self, sales: Path) -> None:
        result = inspect_workbook(sales)
        assert result.sheet("SALES") is not None
        assert result.sheet("nope") is None

    def test_tables_and_defined_names(self, tmp_path: Path) -> None:
        path = build("table", tmp_path / "table.xlsx")
        result = inspect_workbook(path)
        assert len(result.tables) == 1
        assert result.tables[0].name == "OrdersTable"
        assert result.tables[0].column_names == ["OrderId", "Customer", "Total", "Region"]
        assert result.tables[0].row_count == 10
        names = {d.name for d in result.defined_names}
        assert {"OrderIds", "Totals"} <= names

    def test_formula_references_extracted(self, tmp_path: Path) -> None:
        path = build("monthly_sales", tmp_path / "ms.xlsx", rows=5)
        result = inspect_workbook(path)
        amount_formulas = [f for f in result.formulas if f.coordinate.startswith("G")]
        assert amount_formulas
        assert amount_formulas[0].references == ["E2", "F2"]

    def test_external_reference_detected(self, tmp_path: Path) -> None:
        path = build("formula_damage", tmp_path / "damage.xlsx", rows=10)
        result = inspect_workbook(path)
        assert any(f.has_external_reference for f in result.formulas)

    def test_sensitivity_classification(self, tmp_path: Path) -> None:
        import openpyxl

        workbook = openpyxl.Workbook()
        sheet = workbook.active
        sheet.title = "People"
        sheet.append(["Name", "SSN", "Salary"])
        for index in range(3):
            sheet.append([f"n{index}", f"000-00-000{index}", 1000 * index])
        path = tmp_path / "sensitive.xlsx"
        workbook.save(path)

        result = inspect_workbook(path)
        assert result.sensitivity.level == "restricted"
        assert result.sensitivity.matched_signals
        assert result.sensitivity.method == "header_name_and_pattern_heuristic"

    def test_inspection_does_not_modify_the_file(self, sales: Path) -> None:
        """Read-only guarantee: inspecting must not touch the bytes."""
        before = file_sha256(sales)
        inspect_workbook(sales)
        assert file_sha256(sales) == before

    def test_empty_workbook(self, tmp_path: Path) -> None:
        path = build("empty", tmp_path / "empty.xlsx")
        result = inspect_workbook(path)
        assert result.sheet("Empty").is_empty is True
        assert result.total_non_empty_cells == 0


class TestUnsupportedFormats:
    @pytest.mark.parametrize("name", ["legacy.xls", "book.xlsb", "data.csv", "noext"])
    def test_rejected(self, tmp_path: Path, name: str) -> None:
        path = tmp_path / name
        path.write_bytes(b"irrelevant")
        with pytest.raises(UnsupportedFormat):
            check_extension(path)

    def test_legacy_xls_message_is_actionable(self, tmp_path: Path) -> None:
        path = tmp_path / "old.xls"
        path.write_bytes(b"x")
        with pytest.raises(UnsupportedFormat) as info:
            check_extension(path)
        assert "re-save as .xlsx" in str(info.value)

    def test_xlsm_accepted(self, tmp_path: Path) -> None:
        path = tmp_path / "macro.xlsm"
        path.write_bytes(b"x")
        assert check_extension(path) == ".xlsm"


@pytest.mark.security
class TestMalformedWorkbooks:
    def test_not_a_zip(self, tmp_path: Path) -> None:
        path = tmp_path / "broken.xlsx"
        path.write_bytes(b"this is definitely not a zip archive" * 100)
        with pytest.raises(WorkbookSecurityError, match="not a valid"):
            check_archive_integrity(path, LimitsConfig())

    def test_empty_file(self, tmp_path: Path) -> None:
        path = tmp_path / "empty.xlsx"
        path.touch()
        with pytest.raises(WorkbookSecurityError, match="empty"):
            inspect_workbook(path)

    def test_truncated_archive(self, tmp_path: Path, small: Path) -> None:
        data = small.read_bytes()
        corrupt = tmp_path / "truncated.xlsx"
        corrupt.write_bytes(data[: len(data) // 2])
        with pytest.raises(WorkbookSecurityError):
            inspect_workbook(corrupt)

    def test_corrupt_crc(self, tmp_path: Path, small: Path) -> None:
        """Flip a byte inside the compressed payload to break its CRC."""
        source = zipfile.ZipFile(small)
        destination = tmp_path / "crc.xlsx"
        with zipfile.ZipFile(destination, "w") as out:
            for item in source.infolist():
                payload = bytearray(source.read(item.filename))
                if item.filename.endswith(".xml") and len(payload) > 40:
                    payload[30] ^= 0xFF
                out.writestr(item, bytes(payload))
        source.close()
        with pytest.raises(WorkbookSecurityError):
            inspect_workbook(destination)

    def test_zip_with_traversal_member(self, tmp_path: Path) -> None:
        path = tmp_path / "evil.xlsx"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("[Content_Types].xml", "<Types/>")
            archive.writestr("../escape.xml", "<evil/>")
        with pytest.raises(WorkbookSecurityError, match="suspicious"):
            check_archive_integrity(path, LimitsConfig())

    def test_zip_bomb_ratio_detected(self, tmp_path: Path) -> None:
        """A highly compressible payload must trip the ratio limit."""
        path = tmp_path / "bomb.xlsx"
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("[Content_Types].xml", "<Types/>")
            archive.writestr("xl/bomb.xml", "A" * 8_000_000)
        tight = LimitsConfig(max_compression_ratio=10)
        with pytest.raises(LimitExceeded, match="zip bomb"):
            check_archive_integrity(path, tight)

    def test_oversized_file_rejected(self, small: Path) -> None:
        from app.workbook.limits import check_file_size

        # The floor is 1 KiB, so use a file larger than that floor.
        with pytest.raises(LimitExceeded, match="above the"):
            check_file_size(small, LimitsConfig(max_file_size_bytes=1024))


@pytest.mark.security
class TestResourceLimits:
    def test_row_limit_enforced(self) -> None:
        from app.workbook.limits import check_sheet_shape

        with pytest.raises(LimitExceeded, match="rows"):
            check_sheet_shape(
                sheet_name="Sales",
                max_row=1000,
                max_column=10,
                non_empty_cells=100,
                limits=LimitsConfig(max_rows_per_sheet=5),
            )

    def test_cell_limit_enforced(self) -> None:
        from app.workbook.limits import check_sheet_shape

        with pytest.raises(LimitExceeded, match="non-empty cells"):
            check_sheet_shape(
                sheet_name="Big",
                max_row=100,
                max_column=100,
                non_empty_cells=10_000,
                limits=LimitsConfig(max_total_cells=1_000),
            )

    def test_sheet_count_limit(self) -> None:
        from app.workbook.limits import check_sheet_count

        with pytest.raises(LimitExceeded, match="sheets"):
            check_sheet_count(100, LimitsConfig(max_sheets=10))

    def test_formula_count_limit(self) -> None:
        from app.workbook.limits import check_formula_count

        with pytest.raises(LimitExceeded, match="formulas"):
            check_formula_count(5_000, LimitsConfig(max_formula_count=100))


class TestTargetResolution:
    def test_resolves_named_sheet(self, sales: Path) -> None:
        with opened(sales) as workbook:
            resolved = resolve_target(workbook, Target(sheet="Sales", cell_range="A1:C10"))
            assert (resolved.min_row, resolved.max_row) == (1, 10)
            assert (resolved.min_col, resolved.max_col) == (1, 3)

    def test_missing_sheet_lists_alternatives(self, sales: Path) -> None:
        with opened(sales) as workbook, pytest.raises(TargetResolutionError) as info:
            resolve_target(workbook, Target(sheet="Sale"))
        assert "available" in str(info.value)
        assert "Sales" in str(info.value)

    def test_whole_column_bounded_to_used_area(self, sales: Path) -> None:
        with opened(sales) as workbook:
            resolved = resolve_target(workbook, Target(sheet="Sales", cell_range="A:A"))
            assert resolved.max_row < 1_048_576
            assert resolved.min_col == 1

    def test_invalid_range_raises(self, sales: Path) -> None:
        with opened(sales) as workbook, pytest.raises(TargetResolutionError):
            resolve_target(workbook, Target(sheet="Sales", cell_range="not-a-range"))

    def test_header_row_must_be_inside_range(self, sales: Path) -> None:
        with opened(sales) as workbook, pytest.raises(TargetResolutionError, match="header_row"):
            resolve_target(workbook, Target(sheet="Sales", cell_range="A5:C10", header_row=1))

    def test_table_target(self, tmp_path: Path) -> None:
        path = build("table", tmp_path / "table.xlsx")
        with opened(path) as workbook:
            resolved = resolve_target(workbook, Target(sheet="Orders", table="OrdersTable"))
            assert resolved.table_name == "OrdersTable"
            assert resolved.min_row == 1
            assert resolved.max_col == 4

    def test_missing_table_lists_alternatives(self, tmp_path: Path) -> None:
        path = build("table", tmp_path / "table.xlsx")
        with (
            opened(path) as workbook,
            pytest.raises(TargetResolutionError, match="available tables"),
        ):
            resolve_target(workbook, Target(sheet="Orders", table="Nope"))


class TestReadTable:
    def test_reads_with_header(self, sales: Path) -> None:
        with opened(sales) as workbook:
            view = read_table(workbook, Target(sheet="Sales", cell_range="A1:J31"))
            assert view.headers[1] == "Customer"
            assert view.row_count == 30
            assert view.column_count == 10

    def test_column_lookup_case_insensitive(self, sales: Path) -> None:
        with opened(sales) as workbook:
            view = read_table(workbook, Target(sheet="Sales", cell_range="A1:J31"))
            assert view.require_column("customer") == 1
            assert view.require_column("CUSTOMER") == 1

    def test_missing_column_names_options(self, sales: Path) -> None:
        with opened(sales) as workbook:
            view = read_table(workbook, Target(sheet="Sales", cell_range="A1:J31"))
            with pytest.raises(TargetResolutionError) as info:
                view.require_column("Custmer")
            assert "available" in str(info.value)

    def test_to_dicts_handles_duplicate_headers(self, tmp_path: Path) -> None:
        import openpyxl

        workbook = openpyxl.Workbook()
        sheet = workbook.active
        # Intentionally duplicated header.
        sheet.append(["A", "A", "B"])
        sheet.append([1, 2, 3])
        path = tmp_path / "dup.xlsx"
        workbook.save(path)
        with opened(path) as loaded:
            view = read_table(loaded, Target(sheet="Sheet", cell_range="A1:C2"))
            rows = view.to_dicts()
            assert rows[0]["A"] == 1
            assert rows[0]["A_1"] == 2

    def test_max_rows_caps_read(self, sales: Path) -> None:
        with opened(sales) as workbook:
            view = read_table(workbook, Target(sheet="Sales", cell_range="A1:J31"), max_rows=5)
            assert view.row_count == 5


class TestVbaHandling:
    def test_keep_vba_is_enforced_for_xlsm(self, tmp_path: Path) -> None:
        """A macro-enabled workbook must be opened with keep_vba.

        Built by copying a real xlsx to .xlsm and injecting a vbaProject.bin, so
        the test is meaningful even though the fixture collection contains no
        genuinely macro-bearing file.
        """

        workbook = monthly_sales(rows=3)
        base = tmp_path / "base.xlsx"
        workbook.save(base)
        workbook.close()

        macro = tmp_path / "macro.xlsm"
        with zipfile.ZipFile(base) as source, zipfile.ZipFile(macro, "w") as out:
            for item in source.infolist():
                out.writestr(item, source.read(item.filename))
            # A minimal but structurally valid OLE container header.
            out.writestr("xl/vbaProject.bin", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 512)

        assert has_vba(macro) is True
        result = inspect_workbook(macro)
        assert result.metadata.has_vba is True

    def test_plain_xlsx_reports_no_vba(self, small: Path) -> None:
        assert has_vba(small) is False
        assert inspect_workbook(small).metadata.has_vba is False

    def test_xlsm_extension_without_macros_reports_false(self, tmp_path: Path) -> None:
        """A .xlsm saved without macros is common; report it truthfully."""
        workbook = minimal()
        path = tmp_path / "nomacro.xlsm"
        workbook.save(path)
        workbook.close()
        assert has_vba(path) is False


class TestRoundTripFidelity:
    def test_save_and_reload_preserves_content(self, sales: Path, tmp_path: Path) -> None:
        """A save/reload must not lose formulas or values (ADR-0001)."""
        from openpyxl import load_workbook

        before = inspect_workbook(sales)
        workbook = load_workbook(sales)
        destination = tmp_path / "out.xlsx"
        save_atomic(workbook, destination)
        workbook.close()

        after = inspect_workbook(destination)
        assert after.total_formulas == before.total_formulas
        assert after.total_non_empty_cells == before.total_non_empty_cells
        assert after.sheet_names == before.sheet_names

    def test_save_atomic_leaves_no_temp_file(self, sales: Path, tmp_path: Path) -> None:
        from openpyxl import load_workbook

        workbook = load_workbook(sales)
        destination = tmp_path / "out.xlsx"
        save_atomic(workbook, destination)
        workbook.close()
        assert destination.exists()
        assert list(tmp_path.glob(".*tmp*")) == []

    def test_save_atomic_replaces_existing(self, sales: Path, tmp_path: Path) -> None:
        from openpyxl import load_workbook

        destination = tmp_path / "out.xlsx"
        destination.write_bytes(b"stale content")
        workbook = load_workbook(sales)
        save_atomic(workbook, destination)
        workbook.close()
        assert destination.read_bytes()[:2] == b"PK"

    def test_bytes_differ_though_content_matches(self, sales: Path, tmp_path: Path) -> None:
        """The evidence behind ADR-0009: byte comparison would false-positive."""
        from openpyxl import load_workbook

        workbook = load_workbook(sales)
        destination = tmp_path / "out.xlsx"
        save_atomic(workbook, destination)
        workbook.close()

        assert file_sha256(sales) != file_sha256(destination)
        assert (
            inspect_workbook(sales).total_formulas == inspect_workbook(destination).total_formulas
        )


@needs_real
@pytest.mark.slow
class TestRealWorkbooks:
    """Fidelity against real workbooks, when configured.

    Marked ``slow``: a very large sheet takes ~70s to inspect and ~226s to
    round-trip, because openpyxl is a whole-file in-memory model (ADR-0001).
    Excluded from ``make test-fast`` and ``make check``; run explicitly with
    ``pytest -m slow``.

    These are the tests that justify the workbook-engine decision against real
    data rather than synthetic fixtures, so they are worth the wall-clock cost —
    but they should not sit in the pre-commit gate.

    Which workbooks they run against is supplied by ``fixtures/real.py``. Nothing
    about any particular collection is assumed, and each test skips if its role
    is not configured.
    """

    def test_inspects_large_real_workbook(self) -> None:
        result = inspect_workbook(_role_or_skip("large"))
        assert result.sheet_names
        assert result.total_formulas > 1_000
        assert len(result.defined_names) > 0

    def test_inspects_macro_extension_workbook(self) -> None:
        """A macro-enabled workbook opens, and is reported truthfully.

        The assertion is that detection *matches the file*, not that a particular
        fixture happens to contain no macros. A collection that does include a
        real VBA project should report one, and this test should not fail for
        being handed a better fixture than the one originally used.
        """
        path = _role_or_skip("macro_extension")
        result = inspect_workbook(path)
        assert result.extension == "xlsm"
        assert result.sheet_names
        assert result.metadata.has_vba is has_vba(path)

    def test_handles_very_wide_sheet(self) -> None:
        result = inspect_workbook(_role_or_skip("wide"))
        widest = max(result.sheets, key=lambda sheet: sheet.max_column)
        assert widest.max_column > 100

    @pytest.mark.parametrize("role", _ROLES)
    def test_roundtrip_real_workbook(self, role: str, tmp_path: Path) -> None:
        source = _role_or_skip(role)
        from openpyxl import load_workbook

        before = inspect_workbook(source)
        workbook = load_workbook(source, keep_vba=source.suffix.lower() == ".xlsm")
        destination = tmp_path / f"rt_{role}.xlsx"
        save_atomic(workbook, destination)
        workbook.close()

        after = inspect_workbook(destination)
        assert after.total_formulas == before.total_formulas
        assert after.total_non_empty_cells == before.total_non_empty_cells
        assert len(after.defined_names) == len(before.defined_names)
        assert after.sheet_names == before.sheet_names
