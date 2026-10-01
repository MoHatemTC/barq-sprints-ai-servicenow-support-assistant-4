"""
S3.6 — Retry policy: classify a failure as transient (worth one retry) or
permanent (retrying can't help).

Per the S3.4 brief: transient failures (network timeouts, rate limits,
temporary 5xx) are retried once; permanent failures (malformed payloads,
auth/permission errors, missing records) bypass retries and fail cleanly.

Kept in its own tiny module (httpx import only) so it can be unit tested
without importing the Celery/agent/Qdrant task graph.
"""

from __future__ import annotations

import httpx

_TRANSIENT_NETWORK_ERRORS = (
    httpx.TimeoutException,  # connect/read/write/pool timeouts
    httpx.NetworkError,  # connect/read/write/close errors
    TimeoutError,
    ConnectionError,
)


def is_transient_error(exc: BaseException | None) -> bool:
    """
    True if `exc` (or the exception that caused it) looks transient.

    Walks the __cause__/__context__ chain so a library that wraps a network
    error in its own exception type (e.g. a Qdrant client wrapping an httpx
    ConnectError) is still recognised as transient.

    An HTTP status error is decided by its status code the moment it's
    reached: 429 and 5xx are transient; every other 4xx (401, 403, 404,
    400...) is permanent, because resending the same request won't change
    the answer.
    """
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))

        if isinstance(exc, httpx.HTTPStatusError):
            status = exc.response.status_code
            return status == 429 or status >= 500

        if isinstance(exc, _TRANSIENT_NETWORK_ERRORS):
            return True

        exc = exc.__cause__ or exc.__context__

    return False
