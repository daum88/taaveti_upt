"""Process-wide pacing and 429 backoff for SEC EDGAR requests.

SEC's fair-access policy caps automated traffic at 10 requests per second per
source IP, and sustained bursts are throttled earlier than that. Every EDGAR
adapter routes through :func:`get` so the whole process shares one
conservative budget, and a rate-limited response is retried with backoff
instead of failing the calling pipeline outright.
"""

from __future__ import annotations

import logging
import threading
import time

import requests

logger = logging.getLogger(__name__)

_MIN_INTERVAL_SECONDS = 0.2
_MAX_ATTEMPTS = 3
_BACKOFF_BASE_SECONDS = 2.0

_lock = threading.Lock()
_next_slot = 0.0


def get(url: str, *, timeout: float, headers: dict[str, str]) -> requests.Response:
    """GET one EDGAR URL at the shared pace, retrying HTTP 429 with backoff.

    The final response is returned as-is so callers keep their existing
    ``raise_for_status`` and error-mapping paths.
    """
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        response = _paced_get(url, timeout=timeout, headers=headers)
        if response.status_code != 429:
            return response
        if attempt < _MAX_ATTEMPTS:
            wait = _retry_after_seconds(response) or _BACKOFF_BASE_SECONDS * 2 ** (attempt - 1)
            logger.warning("SEC EDGAR rate-limited %s; retry %d/%d in %.1fs", url, attempt, _MAX_ATTEMPTS, wait)
            time.sleep(wait)
    return response


def _paced_get(url: str, *, timeout: float, headers: dict[str, str]) -> requests.Response:
    global _next_slot
    with _lock:
        slot = max(time.monotonic(), _next_slot)
        _next_slot = slot + _MIN_INTERVAL_SECONDS
    delay = slot - time.monotonic()
    if delay > 0:
        time.sleep(delay)
    return requests.get(url, timeout=timeout, headers=headers)


def _retry_after_seconds(response: requests.Response) -> float | None:
    try:
        return max(0.0, float(response.headers.get("Retry-After", "")))
    except (TypeError, ValueError):
        return None
