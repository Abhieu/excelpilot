"""Formula recalculation — an **optional** capability.

## The finding that changed this

The plan originally recorded recalculation as "not yet determined". It has now
been measured, and the answer is that it **is** available:

* ``formulas`` 1.3.4 correctly recalculates the benchmark workbook, including
  cross-sheet references. Verified against independently computed Python
  ground truth: ``Sales!G2..G4`` and ``Summary!B2..B4`` all matched exactly.
* It scales acceptably: 205 formulas in 0.30s.
* It **fails loudly** on a workbook referencing a missing external file, which is
  the correct behaviour.

## Why it is optional anyway

``formulas`` pulls in ``scipy`` and ``schedula``. ADR-0002 commits ExcelPilot to a
five-package dependency set, and most ExcelPilot users never need to evaluate a
formula — they need the workbook *edited correctly*. So recalculation ships as the
``recalc`` extra.

## The honesty rule

``recalculated`` is ``True`` **only** when a recalculation actually completed for
that specific file. It is never inferred from the library being installed, and a
recalculation that fails is reported as a failure, not quietly downgraded to
"static checks" (ADR-0011).
"""

from __future__ import annotations

import contextlib
import os
import warnings
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

#: A recalculation is only attempted up to this size. Beyond it the cost is not
#: worth paying inside a verification step, and the caller is told.
DEFAULT_MAX_CELLS = 500_000


@dataclass(frozen=True, slots=True)
class RecalcResult:
    """The outcome of one recalculation attempt."""

    available: bool
    recalculated: bool
    values: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    library: str | None = None
    cell_count: int = 0
    notes: list[str] = field(default_factory=list)

    def get(self, sheet: str, coordinate: str) -> Any:
        """A recalculated value, or ``None`` if it is not available.

        Keys are matched case-insensitively because ``formulas`` upper-cases
        sheet names in its solution keys, which is a quirk of the library rather
        than of Excel.
        """
        wanted = f"{sheet.upper()}'!{coordinate.upper()}"
        for key, value in self.values.items():
            text = str(key).upper()
            if text.endswith(f"]{wanted}") or text.endswith(f".{wanted}"):
                return _unwrap(value)
        return None

    def value_map(self) -> dict[str, Any]:
        """All values keyed by a stable ``Sheet!A1`` form."""
        flattened: dict[str, Any] = {}
        for key, value in self.values.items():
            text = str(key)
            if "]" in text:
                text = text.split("]", 1)[1]
            if text.startswith("'"):
                text = text.strip("'")
            if "!" in text:
                flattened[text] = _unwrap(value)
        return flattened


class Recalculator(Protocol):
    """Something that can evaluate a workbook's formulas."""

    @property
    def available(self) -> bool: ...

    def recalculate(self, path: Path, *, max_cells: int = DEFAULT_MAX_CELLS) -> RecalcResult: ...


@contextlib.contextmanager
def _muted_stderr() -> Iterator[None]:
    """Silence a third-party library's chatter for the duration of a block.

    ``formulas`` draws tqdm progress bars and prints scheduler warnings straight to
    stderr. Those are noise to an operator reading a report, and they corrupt
    ``--json`` consumers that merge stderr into stdout.

    Only the library's own output is suppressed, and only inside this block —
    ExcelPilot writes nothing during it, so nothing of ours can be lost. If
    redirecting stderr fails for any reason, the block simply proceeds and the
    noise stays, which is preferable to failing a verification over a stream.
    """
    try:
        with Path(os.devnull).open("w") as sink, contextlib.redirect_stderr(sink):
            yield
    except (OSError, ValueError):
        yield


def _unwrap(value: Any) -> Any:
    """Reduce ``formulas``' ``Ranges`` object to a plain scalar.

    Note the asymmetry that made this fiddly: on ``Ranges`` the ``value``
    attribute is a **property** returning a numpy-like ``Array``, while
    ``tolist`` is a **method**. Calling ``value()`` raises, which silently sent
    an earlier version of this function down its fallback path and handed callers
    a ``Ranges`` object instead of a number.
    """
    if not hasattr(value, "value"):
        return value
    try:
        array = value.value  # property, not a call
    except Exception:  # noqa: BLE001 - some value type cannot be read this way
        return value

    # A single-cell result reduces to a float; a range keeps its structure.
    try:
        if getattr(array, "size", None) == 1:
            return float(array.reshape(-1)[0])
    except Exception:  # noqa: BLE001 - fall through to the list strategies
        pass

    if isinstance(array, list):
        if array and isinstance(array[0], list):
            return array[0][0] if len(array[0]) == 1 else array
        return array[0] if len(array) == 1 else array

    try:
        listed = value.tolist()  # method, unlike `value`
    except Exception:  # noqa: BLE001
        return array
    if isinstance(listed, list) and listed and isinstance(listed[0], list):
        return listed[0][0] if len(listed[0]) == 1 else listed
    return listed


def library_available() -> bool:
    """Whether a recalculation library is importable.

    Presence is not capability: this only says the module can be imported. The
    real answer comes from a recalculation actually completing.
    """
    try:
        import formulas  # noqa: F401
    except Exception:  # noqa: BLE001 - any import problem means unavailable
        return False
    return True


class NullRecalculator:
    """Used when no library is installed. Always reports that it did not recalculate."""

    @property
    def available(self) -> bool:
        return False

    def recalculate(self, path: Path, *, max_cells: int = DEFAULT_MAX_CELLS) -> RecalcResult:
        del path, max_cells  # Nothing to do: there is no library to call.
        return RecalcResult(
            available=False,
            recalculated=False,
            library=None,
            notes=[
                "no recalculation library is installed; formula checks are static only. "
                "Install the optional extra with: uv sync --extra recalc"
            ],
        )


class FormulaRecalculator:
    """Recalculates with the ``formulas`` library."""

    def __init__(self, *, timeout_seconds: float = 120.0) -> None:
        self.timeout_seconds = timeout_seconds

    @property
    def available(self) -> bool:
        return library_available()

    def recalculate(self, path: Path, *, max_cells: int = DEFAULT_MAX_CELLS) -> RecalcResult:
        """Recalculate a workbook, reporting honestly whether it happened.

        Every early return sets ``recalculated=False`` and says why. There is no
        path in this function that reports a recalculation that did not occur.
        """
        if not self.available:
            return NullRecalculator().recalculate(path, max_cells=max_cells)

        path = Path(path)
        if not path.exists():
            return RecalcResult(
                available=True,
                recalculated=False,
                library="formulas",
                error=f"workbook not found: {path}",
            )

        estimated = _estimate_formula_count(path)
        if estimated > max_cells:
            return RecalcResult(
                available=True,
                recalculated=False,
                library="formulas",
                cell_count=estimated,
                notes=[
                    f"the workbook has roughly {estimated:,} formulas, above the "
                    f"{max_cells:,} limit for in-process recalculation; formula checks "
                    f"remain static"
                ],
            )

        try:
            import formulas
        except Exception as error:  # noqa: BLE001 - reported, never swallowed
            return RecalcResult(
                available=False,
                recalculated=False,
                error=f"recalculation library could not be imported: {error}",
            )

        try:
            with warnings.catch_warnings():
                # `formulas` emits noisy warnings on import; they are not ours and
                # would drown the CLI's own output.
                warnings.simplefilter("ignore")
                with _muted_stderr():
                    model = formulas.ExcelModel().loads(str(path)).finish()
                    solution = model.calculate()
        except Exception as error:  # noqa: BLE001 - a failed recalculation is a result
            return RecalcResult(
                available=True,
                recalculated=False,
                library="formulas",
                error=f"recalculation failed: {type(error).__name__}: {error}",
                notes=[
                    "a formula could not be evaluated — commonly an external workbook "
                    "reference, an unsupported function, or a circular reference"
                ],
            )

        return RecalcResult(
            available=True,
            recalculated=True,
            library="formulas",
            values=dict(solution),
            cell_count=estimated,
        )


def _estimate_formula_count(path: Path) -> int:
    """Count formulas without importing a recalculation library.

    Used for the size guard, so the guard itself does not depend on the thing it
    is guarding.
    """
    try:
        import openpyxl

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            workbook = openpyxl.load_workbook(path, read_only=True, data_only=False)
        try:
            return sum(
                1
                for sheet in workbook.worksheets
                for row in sheet.iter_rows(values_only=True)
                for value in row
                if isinstance(value, str) and value.startswith("=")
            )
        finally:
            workbook.close()
    except Exception:  # noqa: BLE001 - a failed estimate must not block the attempt
        return 0


def build_recalculator(prefer: bool = True) -> Recalculator:
    """Return the best available recalculator.

    Falls back to :class:`NullRecalculator`, which reports ``recalculated: false``.
    """
    if prefer and library_available():
        return FormulaRecalculator()
    return NullRecalculator()


__all__ = [
    "DEFAULT_MAX_CELLS",
    "FormulaRecalculator",
    "NullRecalculator",
    "RecalcResult",
    "Recalculator",
    "build_recalculator",
    "library_available",
]
