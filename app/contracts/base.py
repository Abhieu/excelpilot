"""Base contract model and the ``UntrustedText`` boundary type."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field, field_validator

CONTRACT_VERSION: Final[str] = "1"

#: Maximum characters retained from any single piece of untrusted content before
#: truncation. Bounds both prompt size and memory use from a hostile workbook.
MAX_UNTRUSTED_CHARS: Final[int] = 4_000


def new_run_id() -> str:
    """Generate a run identifier.

    Format: ``run-<8 hex>-<4 hex>-<4 hex>`` — a UUID4 rendered compactly. Short
    enough to type, wide enough that collision is not a practical concern, and
    filesystem-safe on every platform ExcelPilot targets.
    """
    raw = uuid.uuid4().hex
    return f"run-{raw[:16]}"


def utc_now() -> datetime:
    """Timezone-aware current time. Used everywhere instead of ``datetime.now()``."""
    return datetime.now(UTC)


class ContractModel(BaseModel):
    """Base for every persisted contract.

    ``extra="forbid"`` is the important setting: a hallucinated or unexpected
    field from a model is a validation error, never a silently ignored key. That
    is a large part of why untrusted output cannot smuggle anything through.
    """

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        validate_assignment=True,
        str_strip_whitespace=True,
        use_enum_values=False,
        ser_json_timedelta="float",
    )

    def to_json_dict(self) -> dict[str, Any]:
        """JSON-safe dict, with datetimes as ISO-8601 and enums as their values."""
        return self.model_dump(mode="json")


class UntrustedText:
    """Text originating outside ExcelPilot's control.

    Wrapping untrusted content in a distinct type makes the trust boundary
    visible: a function that expects a system instruction takes ``str``, and one
    that expects workbook content takes ``UntrustedText``. The two cannot be
    confused, and mypy will say so.

    Instances are immutable and carry provenance plus a character cap, so a
    single hostile cell cannot blow up a prompt or a log line.
    """

    __slots__ = ("_text", "_provenance", "_truncated")

    _text: str
    _provenance: str
    _truncated: bool

    def __init__(
        self,
        text: str,
        *,
        provenance: str,
        max_chars: int = MAX_UNTRUSTED_CHARS,
    ) -> None:
        cleaned = text if isinstance(text, str) else str(text)
        if len(cleaned) > max_chars:
            cleaned = cleaned[:max_chars]
            truncated = True
        else:
            truncated = False
        object.__setattr__(self, "_text", cleaned)
        object.__setattr__(self, "_provenance", provenance)
        object.__setattr__(self, "_truncated", truncated)

    @property
    def text(self) -> str:
        return self._text

    @property
    def provenance(self) -> str:
        """Where this text came from: ``workbook_cell``, ``sheet_name``, ..."""
        return self._provenance

    @property
    def truncated(self) -> bool:
        return self._truncated

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self._text

    def __len__(self) -> int:
        return len(self._text)

    def __bool__(self) -> bool:
        return bool(self._text.strip())

    def __eq__(self, other: object) -> bool:
        if isinstance(other, UntrustedText):
            return self._text == other._text and self._provenance == other._provenance
        return NotImplemented

    def __hash__(self) -> int:
        return hash((self._text, self._provenance))

    def __repr__(self) -> str:
        return f"UntrustedText(provenance={self._provenance!r}, len={len(self._text)})"

    def to_redacted(self, limit: int = 120) -> str:
        """Short preview for logs, safe against leaking a whole cell into a log line."""
        preview = self._text[:limit].replace("\n", "\\n")
        suffix = "..." if len(self._text) > limit else ""
        return f"[{self._provenance}] {preview}{suffix}"

    @classmethod
    def __get_pydantic_core_schema__(cls, source_type: type, handler: Any) -> Any:
        """Let pydantic carry UntrustedText through validation and serialisation.

        Persisted as ``{"text": ..., "provenance": ...}`` so audit records stay
        readable and the provenance of untrusted content survives a round-trip —
        which matters, because "where did this string come from" is what tells a
        reviewer whether it can be trusted.
        """
        from pydantic_core import core_schema

        def _validate(value: Any) -> UntrustedText:
            if isinstance(value, UntrustedText):
                return value
            if isinstance(value, dict):
                return cls(
                    str(value.get("text", "")), provenance=str(value.get("provenance", "unknown"))
                )
            return cls(str(value), provenance="unknown")

        return core_schema.no_info_plain_validator_function(
            _validate,
            serialization=core_schema.plain_serializer_function_ser_schema(
                lambda value: {"text": value.text, "provenance": value.provenance},
                return_schema=core_schema.dict_schema(),
            ),
        )


class CellCoordinate(BaseModel):
    """A single A1-style cell reference, validated on construction."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    row: int = Field(ge=1, le=1_048_576)
    column: int = Field(ge=1, le=16_384)

    @field_validator("column")
    @classmethod
    def _column_in_range(cls, value: int) -> int:
        if value > 16_384:
            raise ValueError("column must be <= 16384 (Excel's XFD limit)")
        return value

    def __str__(self) -> str:
        return f"{column_letter(self.column)}{self.row}"


def column_letter(index: int) -> str:
    """1-based column index to letters: 1 -> A, 26 -> Z, 27 -> AA."""
    if index < 1:
        raise ValueError("column index must be >= 1")
    letters = ""
    while index > 0:
        index, remainder = divmod(index - 1, 26)
        letters = chr(ord("A") + remainder) + letters
    return letters


def column_index(letters: str) -> int:
    """Column letters to 1-based index: A -> 1, AA -> 27. Case-insensitive."""
    cleaned = letters.strip().upper()
    if not cleaned or not cleaned.isalpha():
        raise ValueError(f"invalid column reference: {letters!r}")
    total = 0
    for char in cleaned:
        total = total * 26 + (ord(char) - ord("A") + 1)
    return total


def parse_a1(reference: str) -> tuple[int, int]:
    """Parse an A1 reference into ``(row, column)``, both 1-based.

    Only absolute and relative single-cell references are accepted. Range
    references (``A1:B2``) and whole-column forms are handled by the workbook
    layer, which has the worksheet context needed to do it correctly.
    """
    cleaned = reference.strip().replace("$", "")
    if not cleaned:
        raise ValueError("empty cell reference")
    index = 0
    while index < len(cleaned) and cleaned[index].isalpha():
        index += 1
    letters, digits = cleaned[:index], cleaned[index:]
    if not letters or not digits.isdigit():
        raise ValueError(f"invalid A1 reference: {reference!r}")
    row = int(digits)
    if row < 1:
        raise ValueError(f"invalid A1 reference {reference!r}: Excel rows are 1-based")
    return row, column_index(letters)


__all__ = [
    "CONTRACT_VERSION",
    "CellCoordinate",
    "ContractModel",
    "MAX_UNTRUSTED_CHARS",
    "UntrustedText",
    "column_index",
    "column_letter",
    "new_run_id",
    "parse_a1",
    "utc_now",
]
