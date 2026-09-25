"""Terminal output.

Human-readable text goes to **stdout** and diagnostics to **stderr**, so
``excelpilot run … --json | jq`` is reliable. Every ``--json`` document is
stable and versioned.
"""

from __future__ import annotations

import builtins
import json
from typing import Any

from rich.console import Console
from rich.table import Table

#: Streaming for humans.
_console = Console(stderr=False, highlight=False, soft_wrap=False)
#: Diagnostics, warnings, and errors.
_error = Console(stderr=True, highlight=False, soft_wrap=False)

#: JSON is printed with the builtin ``print`` rather than through rich, so it
#: stays machine-parseable even when stdout is a pipe.


def emit_json(payload: dict[str, Any]) -> None:
    """Print a versioned JSON document to stdout."""
    document = {"contract_version": "1", **payload}
    builtins.print(json.dumps(document, indent=2, ensure_ascii=False, default=str))


def line(text: str = "") -> None:
    _console.print(text)


def heading(text: str) -> None:
    _console.rule(text)


def field(label: str, value: Any) -> None:
    _console.print(f"  {label + ':':<28} {value}")


def success(text: str) -> None:
    _console.print(f"[green]OK[/green]  {text}")


def warn(text: str) -> None:
    _error.print(f"[yellow]WARN[/yellow]  {text}")


def error(text: str) -> None:
    _error.print(f"[red]ERROR[/red] {text}")


def note(text: str) -> None:
    _error.print(f"[dim]{text}[/dim]")


def key_values(rows: list[tuple[str, Any]]) -> None:
    """A left-aligned key/value block."""
    for label, value in rows:
        field(label, value)


def table(title: str, columns: list[str], rows: list[list[Any]]) -> None:
    """A simple bordered table."""
    grid = Table(title=title, show_header=True, header_style="bold")
    for column in columns:
        grid.add_column(column)
    for row in rows:
        grid.add_row(*[str(cell) for cell in row])
    _console.print(grid)


def table_to_json(columns: list[str], rows: list[list[Any]]) -> dict[str, Any]:
    """The same table as a JSON object, for ``--json`` consumers."""
    return {"columns": columns, "rows": rows}


def dry_run_banner(preview: Any) -> None:
    """Render the dry-run summary.

    Every number here comes from a simulation against the real workbook; none is
    an estimate.
    """
    heading("DRY RUN")
    line()
    field("Workbook", preview.workbook_name)
    field("Source", preview.source_path)
    field("Proposed output", preview.proposed_output_path)
    line()
    line("Changes:")
    field("cells that would change", f"{preview.cells_to_change:,}")
    field("sheets affected", preview.sheets_affected)
    if preview.sheets_affected_names:
        field("", ", ".join(preview.sheets_affected_names))
    field("formulas added", f"{preview.formulas_to_add:,}")
    field("formulas removed", f"{preview.formulas_to_remove:,}")
    field("records to normalise", f"{preview.records_to_normalize:,}")
    field("records to remove", f"{preview.records_to_remove:,}")
    field("records requiring review", f"{preview.records_to_require_review:,}")
    line()
    line("Risk:")
    field("level", str(preview.risk).upper())
    for reason in preview.risk_reasons:
        field("", reason)
    line()
    line("Approval:")
    field("required", "YES" if preview.approval_required else "no")
    line()
    line("Verification:")
    for check in preview.verification_plan:
        field("", check)
    if preview.structural_change:
        line()
        warn("this plan changes workbook structure")


def verification_banner(result: Any) -> None:
    """Render verification, always stating that formulas were not recalculated."""
    from app.verification import describe

    line(describe(result))


__all__ = [
    "emit_json",
    "error",
    "field",
    "heading",
    "key_values",
    "line",
    "note",
    "success",
    "table",
    "table_to_json",
    "warn",
]
