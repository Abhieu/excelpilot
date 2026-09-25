"""Storage: run records, artefacts, and atomic versioned writes.

``app.storage`` is the package that knows about the filesystem layout. It must
not import the executor or the planner — persistence is not allowed to depend on
how a run was produced.
"""

from app.storage.store import FileRunStore, RunLocked, RunPaths

__all__ = ["FileRunStore", "RunLocked", "RunPaths"]
