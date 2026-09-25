"""Secret redaction.

Applied **before** an audit event is serialised, not when it is read. Redacting on
read would mean secrets were already on disk, which defeats the purpose.

Two complementary strategies:

* **Registered values** — the literal contents of any secret this process can see
  (typically an API key from the environment). Replaced wherever they appear.
* **Shape-based patterns** — anything that *looks* like a credential, so a secret
  ExcelPilot was never told about still does not leak.

A redactor that can raise is worse than one that over-masks, so this function
never raises: on any internal failure it returns a conservative marker.
"""

from __future__ import annotations

import re
from typing import Any

#: Pattern-based redaction. Each entry is (name, compiled pattern).
#:
#: Order matters: patterns are applied in order, so a **more specific pattern must
#: come first**. A bare ``sk-`` pattern placed before ``sk-ant-`` masks an
#: Anthropic key as ``[REDACTED:openai_key]`` — still redacted, but mislabelled,
#: which is exactly the kind of detail an operator debugging an auth failure
#: needs to get right.
#:
#: Deliberately broad. A false positive hides a harmless string in a log; a false
#: negative puts a live credential on disk. The trade is not close.
_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "anthropic_key",
        re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{16,}"),
    ),
    (
        "openrouter_key",
        re.compile(r"\bsk-or-v1-[A-Za-z0-9_\-]{16,}"),
    ),
    (
        "openai_key",
        re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"),
    ),
    (
        "bearer_token",
        re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{16,}"),
    ),
    (
        "aws_access_key",
        re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    ),
    (
        "google_api_key",
        re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"),
    ),
    (
        "jwt",
        re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"),
    ),
    (
        "private_key_block",
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
            re.DOTALL,
        ),
    ),
    (
        "connection_string",
        re.compile(r"(?i)\b(?:postgres|postgresql|mysql|mongodb|redis)://[^\s:@]+:[^\s@]+@"),
    ),
)

#: Environment variables whose values are registered as secrets at startup.
SECRET_ENV_VARS: tuple[str, ...] = (
    "TYPESAFE_API_KEY",
    "OPENROUTER_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "GOOGLE_API_KEY",
    "AZURE_API_KEY",
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "DATABASE_URL",
    "SUPABASE_KEY",
)

#: Keys whose *values* are always redacted, whatever they contain. Covers
#: application-level secrets that the patterns above would not recognise.
SENSITIVE_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "secret",
        "secret_key",
        "token",
        "access_token",
        "refresh_token",
        "password",
        "passwd",
        "pass",
        "authorization",
        "auth",
        "private_key",
        "client_secret",
        "credential",
        "credentials",
        "session",
        "cookie",
        "bearer",
        "connection_string",
        "dsn",
        "otp",
        "mfa",
        "pin",
    }
)

#: Shortest secret value worth searching for. Below this, a match would be
#: ambiguous and would redact half the log.
_MIN_SECRET_LENGTH = 8

#: Upper bound on registered values, to bound the cost of a large scan.
_MAX_REGISTERED = 64


class Redactor:
    """Redacts secrets from strings, mappings, and sequences.

    Registered values are held in memory only. They are never written anywhere,
    and the redactor itself is not part of any persisted artefact.
    """

    __slots__ = ("_values",)

    def __init__(self, extra_values: tuple[str, ...] = ()) -> None:
        import os

        values: list[str] = []
        for name in SECRET_ENV_VARS:
            value = os.environ.get(name, "").strip()
            if len(value) >= _MIN_SECRET_LENGTH:
                values.append(value)
        for value in extra_values:
            if value and len(value) >= _MIN_SECRET_LENGTH:
                values.append(value)
        # Longest first, so a secret that is a prefix of another is fully masked.
        self._values: tuple[str, ...] = tuple(sorted(set(values), key=len, reverse=True))[
            :_MAX_REGISTERED
        ]

    @property
    def registered_count(self) -> int:
        return len(self._values)

    def mask(self, text: str) -> str:
        """Redact secrets from a string."""
        try:
            result = text
            for value in self._values:
                if value in result:
                    result = result.replace(value, "[REDACTED:registered]")
            for name, pattern in _PATTERNS:
                result = pattern.sub(f"[REDACTED:{name}]", result)
            return result
        except Exception:  # noqa: BLE001 - a redactor must never raise
            return "[REDACTED:unprocessable]"

    def redact(self, value: Any, *, _depth: int = 0) -> Any:
        """Recursively redact a value.

        Sensitive *keys* are masked wholesale; other strings are scanned for
        secret shapes. Depth-capped so a deeply nested or self-referential
        structure cannot cause unbounded work.
        """
        if _depth > 12:
            return "[REDACTED:too_deep]"
        try:
            if isinstance(value, str):
                return self.mask(value)
            if isinstance(value, dict):
                result: dict[Any, Any] = {}
                for key, item in value.items():
                    if isinstance(key, str) and _is_sensitive_key(key):
                        result[key] = "[REDACTED:sensitive_key]"
                    else:
                        result[self.mask(key) if isinstance(key, str) else key] = self.redact(
                            item, _depth=_depth + 1
                        )
                return result
            if isinstance(value, (list, tuple, set)):
                return [self.redact(item, _depth=_depth + 1) for item in value]
            if isinstance(value, (int, float, bool, type(None))):
                return value
            return self.mask(str(value))
        except Exception:  # noqa: BLE001 - a redactor must never raise
            return "[REDACTED:unprocessable]"

    def is_clean(self, text: str) -> bool:
        """Whether a string appears to contain no secret material.

        Used by a test that asserts audit output is clean; not a gate at runtime,
        because raising on a false positive would be worse than masking.
        """
        return self.mask(text) == text


def _is_sensitive_key(key: str) -> bool:
    """Whether a mapping key names something secret.

    Matched on normalised substrings so ``openai_api_key``, ``apiKey`` and
    ``X-Auth-Token`` are all caught.
    """
    normalised = key.strip().lower().replace("-", "_").replace(" ", "_")
    if normalised in SENSITIVE_KEYS:
        return True
    return any(
        marker in normalised
        for marker in ("api_key", "apikey", "secret", "password", "passwd", "token", "private_key")
    )


#: Module-level default, so callers that do not need a custom instance can use a
#: shared one. Registered values are read from the environment at import, which
#: means a key exported *after* import is not registered — hence
#: :func:`redactor` for callers that need a fresh read.
_default: Redactor | None = None


def redactor(extra_values: tuple[str, ...] = ()) -> Redactor:
    """Get a redactor, re-reading the environment on first use."""
    global _default
    if _default is None or extra_values:
        return Redactor(extra_values)
    return _default


def redact(value: Any) -> Any:
    """Redact a value using the shared redactor."""
    global _default
    if _default is None:
        _default = Redactor()
    return _default.redact(value)


__all__ = [
    "SENSITIVE_KEYS",
    "SECRET_ENV_VARS",
    "Redactor",
    "redact",
    "redactor",
]
