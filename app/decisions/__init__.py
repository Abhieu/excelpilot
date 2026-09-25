"""JEV decisioning: advisory only.

Depends on ``app.contracts`` and ``app.net``. Must not import the executor, the
policy engine, or the workbook engine — JEV cannot reach a workbook
(``tests/test_architecture.py`` enforces this).
"""

from app.decisions.jev import (
    ENDPOINTS,
    KEY_ENV_VARS,
    HttpJevAdapter,
    JevAdapter,
    MockJevAdapter,
    build_request,
    parse_response,
    resolve_provider,
)
from app.decisions.questions import REVIEW_LABELS, build_questions, build_state

__all__ = [
    "ENDPOINTS",
    "KEY_ENV_VARS",
    "REVIEW_LABELS",
    "HttpJevAdapter",
    "JevAdapter",
    "MockJevAdapter",
    "build_questions",
    "build_request",
    "build_state",
    "parse_response",
    "resolve_provider",
]
