"""Safety controls: path sandbox, prompt-injection firewall, formula-injection guard.

These are the deterministic defences. They are deliberately separate from the
planner and the executor so the guards cannot be bypassed by a call path that
happens to skip the AI layer.
"""

from app.safety.formula_guard import (
    FORMULA_TRIGGERS,
    looks_like_formula,
    neutralise,
    neutralise_row,
)
from app.safety.formula_guard import (
    describe as describe_formula_value,
)
from app.safety.injection import (
    InjectionFinding,
    ScanResult,
    is_safe_name,
    sanitise_name,
    scan,
    scan_untrusted,
    wrap_as_data,
)
from app.safety.paths import (
    PathOutsideWorkspace,
    is_within,
    resolve_within,
    versioned_output,
    workspace_root,
)

__all__ = [
    "FORMULA_TRIGGERS",
    "InjectionFinding",
    "PathOutsideWorkspace",
    "ScanResult",
    "describe_formula_value",
    "is_safe_name",
    "is_within",
    "looks_like_formula",
    "neutralise",
    "neutralise_row",
    "resolve_within",
    "sanitise_name",
    "scan",
    "scan_untrusted",
    "versioned_output",
    "workspace_root",
    "wrap_as_data",
]
