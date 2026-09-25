"""Workbook engine: inspection, reading, hashing, and resource limits.

Depends on ``app.contracts`` only. No AI, no JEV, no policy, no CLI
(``tests/test_architecture.py`` enforces this).
"""

from app.workbook.hashing import content_hash, file_sha256, is_formula, normalise_value
from app.workbook.inspector import WorkbookInspector, inspect_workbook
from app.workbook.limits import (
    SUPPORTED_EXTENSIONS,
    UnsupportedFormat,
    check_archive_integrity,
    check_extension,
    check_file_size,
)
from app.workbook.reader import (
    copy_snapshot,
    has_external_links,
    has_vba,
    load_workbook,
    opened,
    save_atomic,
    sheet_state,
)
from app.workbook.table import (
    TableView,
    expand_range,
    find_sheet,
    read_table,
    resolve_range,
    resolve_target,
)

__all__ = [
    "SUPPORTED_EXTENSIONS",
    "TableView",
    "UnsupportedFormat",
    "WorkbookInspector",
    "check_archive_integrity",
    "check_extension",
    "check_file_size",
    "content_hash",
    "copy_snapshot",
    "expand_range",
    "file_sha256",
    "find_sheet",
    "has_external_links",
    "has_vba",
    "inspect_workbook",
    "is_formula",
    "load_workbook",
    "normalise_value",
    "opened",
    "read_table",
    "resolve_range",
    "resolve_target",
    "save_atomic",
    "sheet_state",
]
