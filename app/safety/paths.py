"""Path sandbox.

Every input and output path is resolved and checked against the configured
workspace root. The check happens **after** ``Path.resolve()``, so ``..``
sequences and symlinks are both defeated — a symlink pointing outside the
workspace resolves outside it and is rejected.

This is why a path like ``../../etc/passwd`` or a symlink to ``/tmp`` cannot be
used to read or write outside the workspace, regardless of how the path was
supplied.
"""

from __future__ import annotations

from pathlib import Path

from app.contracts.errors import ExcelPilotError


class PathOutsideWorkspace(ExcelPilotError):
    """A path resolved outside the configured workspace root."""

    code = "path_outside_workspace"

    def __init__(self, path: Path, root: Path) -> None:
        super().__init__(
            f"path {path} resolves outside the workspace root {root}",
            details={"path": str(path), "workspace_root": str(root)},
        )
        self.path = path
        self.root = root


def workspace_root(config_root: str | Path) -> Path:
    """Resolve the workspace root, creating it if necessary."""
    root = Path(config_root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def resolve_within(
    path: str | Path,
    root: str | Path,
    *,
    must_exist: bool = False,
) -> Path:
    """Resolve ``path`` and require it to be inside ``root``.

    ``must_exist`` additionally requires the resolved path to be an existing
    file, which is what input paths need.
    """
    root_resolved = Path(root).expanduser().resolve()
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = root_resolved / candidate
    try:
        resolved = candidate.resolve()
    except (OSError, RuntimeError) as error:
        # A symlink loop, or a path component that cannot be resolved.
        raise PathOutsideWorkspace(candidate, root_resolved) from error

    if not resolved.is_relative_to(root_resolved):
        raise PathOutsideWorkspace(resolved, root_resolved)
    if must_exist and not resolved.is_file():
        raise ExcelPilotError(
            f"file not found: {resolved}",
            details={"path": str(resolved)},
        )
    return resolved


def is_within(path: str | Path, root: str | Path) -> bool:
    """Whether a path resolves inside the root. Non-raising form."""
    try:
        resolve_within(path, root)
    except ExcelPilotError:
        return False
    return True


def versioned_output(source: Path, run_id: str, *, suffix: str | None = None) -> Path:
    """Build the versioned output path for a run.

    ``monthly_sales.xlsx`` + ``run-1a2b3c4d5e6f7890`` becomes
    ``monthly_sales__run-1a2b3c4d5e6f7890.xlsx``.

    Always a *different* path from the source, which is the property that makes
    "never overwrite the original" structural rather than a policy check
    (ADR-0010).
    """
    extension = suffix or source.suffix
    stem = source.stem
    return source.with_name(f"{stem}__{run_id}{extension}")


__all__ = [
    "PathOutsideWorkspace",
    "is_within",
    "resolve_within",
    "versioned_output",
    "workspace_root",
]
