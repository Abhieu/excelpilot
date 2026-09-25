"""Minimal HTTP client.

Deliberately stdlib-only, mirroring the reasoning in JEV's own ``jev.py``:
the component that handles API keys should have no dependencies of its own, and
should have complete control over timeouts, redirects, and what reaches a log
(ADR-0002, ADR-0004).

Hard rules, all of which are security properties rather than conveniences:

* **No automatic retry.** JEV's documented behaviour is "no automatic retry was
  made"; a silent retry against a paid endpoint can bill twice.
* **No redirect following.** A redirect is how an API key gets exfiltrated.
* **Credentials never reach a log or an error message.** Provider error bodies
  are discarded, not surfaced, because they can echo the request.
"""

from __future__ import annotations

import json
import math
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

#: Bound on any response body read. A hostile or broken endpoint should not be
#: able to exhaust memory.
MAX_RESPONSE_BYTES = 8 * 1024 * 1024


#: Reject non-finite JSON numbers rather than silently accepting NaN/Infinity,
#: which are not valid JSON and would break serialisation downstream.
def _reject_constant(value: str) -> Any:
    raise ValueError(f"non-finite JSON number: {value}")


def _load_json(raw: bytes) -> Any:
    return json.loads(raw.decode("utf-8"), parse_constant=_reject_constant)


class HttpError(Exception):
    """A network or protocol failure.

    The provider's response body is deliberately **not** included: it can echo
    the request, and the request carries the task and workbook facts.
    """

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect. Following one would send the bearer token elsewhere."""

    def redirect_request(
        self,
        req: Any,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        """Refuse the redirect by returning None, which aborts the open."""
        return


@dataclass(frozen=True, slots=True)
class HttpResponse:
    """A successful, parsed JSON response."""

    data: Any
    status: int
    elapsed_seconds: float


def post_json(
    url: str,
    payload: dict[str, Any],
    *,
    api_key: str,
    timeout: float,
    provider_name: str,
    extra_headers: dict[str, str] | None = None,
) -> HttpResponse:
    """POST a JSON payload and parse the JSON response.

    Raises :class:`HttpError` on any failure. Never retries, never follows a
    redirect, never puts the key or the response body in the error.
    """
    if not api_key.strip():
        raise HttpError(f"{provider_name} API key is not set in the environment")
    if any(ord(char) < 33 or ord(char) > 126 for char in api_key):
        # A key with whitespace or non-ASCII is a configuration error; failing
        # here is better than sending a malformed Authorization header.
        raise HttpError(f"{provider_name} API key contains invalid characters")

    import time

    body = json.dumps(payload, allow_nan=False).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        **(extra_headers or {}),
    }
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")

    started = time.monotonic()
    try:
        with urllib.request.build_opener(_NoRedirect).open(request, timeout=timeout) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise HttpError(f"{provider_name} response exceeded {MAX_RESPONSE_BYTES} bytes")
            status = getattr(response, "status", 200)
    except urllib.error.HTTPError as error:
        status = error.code
        error.close()
        # Body intentionally discarded.
        raise HttpError(
            f"{provider_name} returned HTTP {status}; no automatic retry was made",
            status=status,
        ) from None
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise HttpError(
            f"{provider_name} connection failed or timed out; no automatic retry was made "
            f"({type(error).__name__})"
        ) from None

    try:
        data = _load_json(raw)
    except (json.JSONDecodeError, UnicodeError, ValueError):
        raise HttpError(f"{provider_name} returned invalid JSON") from None

    if isinstance(data, dict) and "error" in data:
        raise HttpError(f"{provider_name} returned an error response", status=status)
    if not isinstance(data, dict):
        raise HttpError(f"{provider_name} returned a non-object response")

    return HttpResponse(data=data, status=status, elapsed_seconds=time.monotonic() - started)


def is_finite_number(value: Any) -> bool:
    """Whether a value is a real, finite number (rejects bool, NaN, inf)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(float(value))


__all__ = ["HttpError", "HttpResponse", "is_finite_number", "post_json"]
