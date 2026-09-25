"""Workbook resource limits (security controls, not tuning knobs).

ExcelPilot opens workbooks it did not create. A hostile or merely broken file
must not be able to exhaust memory, CPU, or disk. Every limit here raises a typed
``LimitExceeded`` rather than allocating.

The thresholds are sized to admit the real fixture set — which includes a
37,883x120 sheet — while rejecting pathological archives.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

from app.contracts.config import LimitsConfig
from app.contracts.errors import LimitExceeded, WorkbookSecurityError

#: Extensions ExcelPilot will open. XLSM is read/preserve only (ADR-0001).
SUPPORTED_EXTENSIONS = frozenset({".xlsx", ".xlsm"})

#: Refuse to even attempt these, before opening.
FORBIDDEN_EXTENSIONS = frozenset({".xls", ".xlb", ".xlt", ".xltm", ".xlam", ".csv", ".etl"})

#: Minimum bytes per zip entry worth reading. Guards against a zip bomb that
#: inflates a single tiny member.
_MIN_ENTRY_BYTES = 32


class UnsupportedFormat(WorkbookSecurityError):
    """The file is not a workbook format ExcelPilot supports.

    A legacy ``.xls`` lands here too, with a message saying what to do about it
    rather than failing obscurely inside openpyxl.
    """

    code = "unsupported_format"

    def __init__(self, path: Path, extension: str) -> None:
        legacy = extension in FORBIDDEN_EXTENSIONS
        hint = (
            " Legacy .xls is not supported; re-save as .xlsx first."
            if legacy
            else " Supported formats: .xlsx, .xlsm."
        )
        super().__init__(
            f"cannot open {path.name!r}: {extension or '(no extension)'} is not a supported "
            f"workbook format.{hint}",
            details={"path": str(path), "extension": extension, "legacy": legacy},
        )
        self.extension = extension


def check_extension(path: Path) -> str:
    """Validate the file extension, returning it lowercased.

    A legacy ``.xls`` is rejected with a clear "not supported" rather than
    allowed to fail obscurely inside openpyxl.
    """
    extension = path.suffix.lower()
    if extension in FORBIDDEN_EXTENSIONS:
        raise UnsupportedFormat(path, extension)
    if extension not in SUPPORTED_EXTENSIONS:
        raise UnsupportedFormat(path, extension)
    return extension


def check_file_size(path: Path, limits: LimitsConfig) -> int:
    """Reject an oversized file before reading it."""
    try:
        size = path.stat().st_size
    except OSError as error:
        raise WorkbookSecurityError(f"cannot stat {path.name}: {error}") from error
    if size == 0:
        raise WorkbookSecurityError(f"{path.name} is empty (0 bytes)")
    if size > limits.max_file_size_bytes:
        raise LimitExceeded(
            f"{path.name} is {size:,} bytes, above the {limits.max_file_size_bytes:,} byte limit",
            details={"size": size, "limit": limits.max_file_size_bytes, "file": path.name},
        )
    return size


def check_archive_integrity(path: Path, limits: LimitsConfig) -> int:
    """Validate the OOXML package before openpyxl parses it.

    Two attacks are stopped here:

    * **Zip bomb** — a small archive that inflates enormously. Detected by
      comparing total uncompressed size against compressed size.
    * **Path traversal inside the archive** — a member named ``../evil`` or an
      absolute path. ExcelPilot never extracts archives, so this is not directly
      exploitable, but a package containing such a member is malformed and worth
      refusing.
    """
    try:
        with zipfile.ZipFile(path) as archive:
            bad = archive.testzip()
            if bad is not None:
                raise WorkbookSecurityError(
                    f"{path.name} is a corrupt archive; CRC mismatch in {bad!r}",
                    details={"file": path.name, "member": bad},
                )
            entries = archive.infolist()
            # compress_size is the on-disk size; file_size is the inflated size.
            # The ratio between them is the zip-bomb signal.
            total_uncompressed = sum(entry.file_size for entry in entries)
            total_compressed = sum(entry.compress_size for entry in entries)
            for entry in entries:
                name = entry.filename
                if name.startswith("/") or ".." in Path(name).parts:
                    raise WorkbookSecurityError(
                        f"{path.name} contains a suspicious archive member {name!r}",
                        details={"file": path.name, "member": name},
                    )
    except zipfile.BadZipFile as error:
        raise WorkbookSecurityError(
            f"{path.name} is not a valid XLSX/XLSM package: {error}",
            details={"file": path.name},
        ) from error

    if total_compressed > 0:
        ratio = total_uncompressed / total_compressed
        if ratio > limits.max_compression_ratio:
            raise LimitExceeded(
                f"{path.name} has a compression ratio of {ratio:.0f}:1, above the "
                f"{limits.max_compression_ratio}:1 limit (possible zip bomb)",
                details={
                    "file": path.name,
                    "ratio": round(ratio, 1),
                    "limit": limits.max_compression_ratio,
                    "uncompressed": total_uncompressed,
                },
            )
    return total_uncompressed


def check_sheet_shape(
    *,
    sheet_name: str,
    max_row: int,
    max_column: int,
    non_empty_cells: int,
    limits: LimitsConfig,
) -> None:
    """Enforce per-sheet and whole-workbook dimension limits."""
    if max_row > limits.max_rows_per_sheet:
        raise LimitExceeded(
            f"sheet {sheet_name!r} declares {max_row:,} rows, above the "
            f"{limits.max_rows_per_sheet:,} limit",
            details={"sheet": sheet_name, "rows": max_row, "limit": limits.max_rows_per_sheet},
        )
    if max_column > limits.max_columns_per_sheet:
        raise LimitExceeded(
            f"sheet {sheet_name!r} declares {max_column:,} columns, above the "
            f"{limits.max_columns_per_sheet:,} limit",
            details={"sheet": sheet_name, "columns": max_column},
        )
    if non_empty_cells > limits.max_total_cells:
        raise LimitExceeded(
            f"sheet {sheet_name!r} has {non_empty_cells:,} non-empty cells, above the "
            f"{limits.max_total_cells:,} limit",
            details={"sheet": sheet_name, "cells": non_empty_cells},
        )


def check_formula_count(count: int, limits: LimitsConfig) -> None:
    if count > limits.max_formula_count:
        raise LimitExceeded(
            f"workbook has {count:,} formulas, above the {limits.max_formula_count:,} limit",
            details={"formulas": count, "limit": limits.max_formula_count},
        )


def check_sheet_count(count: int, limits: LimitsConfig) -> None:
    if count > limits.max_sheets:
        raise LimitExceeded(
            f"workbook has {count:,} sheets, above the {limits.max_sheets:,} limit",
            details={"sheets": count, "limit": limits.max_sheets},
        )


__all__ = [
    "FORBIDDEN_EXTENSIONS",
    "SUPPORTED_EXTENSIONS",
    "UnsupportedFormat",
    "check_archive_integrity",
    "check_extension",
    "check_file_size",
    "check_formula_count",
    "check_sheet_count",
    "check_sheet_shape",
]
