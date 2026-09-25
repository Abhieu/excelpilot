"""Content hashing.

ExcelPilot never compares workbook *bytes* to detect change. A load/save cycle
rewrites the OOXML zip, so an untouched workbook still produces a different file
(ADR-0009). Change detection therefore runs on logical cell content.

``file_sha256`` is still recorded: it identifies *which file* was read, which is a
different question from *what changed in it*.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from collections.abc import Iterable
from pathlib import Path
from typing import Any

#: Read in chunks so a 200 MB workbook is not loaded into memory just to hash it.
_CHUNK_SIZE = 1024 * 1024


def file_sha256(path: Path) -> str:
    """SHA-256 of a file's bytes, streamed.

    Answers "is this the same file?" — not "did its contents change?".
    """
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def normalise_value(value: Any) -> str:
    """Canonical string form of a cell value, for stable comparison.

    Without this, float representation noise and timezone variants would produce
    phantom diffs on every run:

    * ``datetime`` -> ISO-8601 (timezone-aware preserved, naive left naive)
    * ``float`` -> repr, with ``-0.0`` folded to ``0.0`` and integral floats
      rendered without a trailing ``.0`` so ``5`` and ``5.0`` match
    * ``bool`` -> ``TRUE``/``FALSE``, Excel's own convention
    * ``None`` -> empty string, so a blank cell and an empty string agree
    * ``Decimal`` -> normalised string
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, float):
        if value != value:  # NaN
            return "NaN"
        if value in (float("inf"), float("-inf")):
            return "INF" if value > 0 else "-INF"
        if value == int(value) and abs(value) < 1e15:
            return str(int(value))
        return repr(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, dt.timedelta):
        return f"PT{value.total_seconds()}S"
    if isinstance(value, bytes):
        return value.hex()
    return str(value)


def is_formula(value: Any) -> bool:
    """Whether a cell holds a formula rather than a literal."""
    return isinstance(value, str) and value.startswith("=")


def content_hash(cells: Iterable[tuple[str, str, str, str, str]]) -> str:
    """Stable fingerprint of a sheet's logical content.

    Takes ``(sheet, coordinate, value_repr, data_type, number_format, style_id)``
    tuples, sorts them, and hashes. Sorting makes the result independent of
    iteration order; including the sheet and coordinate makes it position-aware.

    ``style_id`` is a workbook-local index and can be renumbered on save, so it is
    used only to detect *whether* formatting changed in a cell, never to claim
    exact style equality. ADR-0009 records this.
    """
    digest = hashlib.sha256()
    for record in sorted(cells):
        digest.update("\x1f".join(record).encode("utf-8", errors="replace"))
        digest.update(b"\x1e")
    return digest.hexdigest()


__all__ = ["content_hash", "file_sha256", "is_formula", "normalise_value"]
