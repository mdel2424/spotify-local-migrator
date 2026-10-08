import logging
import math
import time
from collections.abc import Callable
from typing import Any

import httpx

from ..errors import RateLimitError, SpotifyAPIError

logger = logging.getLogger(__name__)


def request_json(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    label: str,
    max_retries: int = 3,
    max_retry_wait: float = 60,
    sleep: Callable[[float], None] = time.sleep,
    allow_empty: bool = False,
    before_request: Callable[[], None] | None = None,
    stop_on_rate_limit: bool = False,
    **kwargs: Any,
) -> tuple[int, dict[str, Any]]:
    """Bounded retries. Never log request bodies, OAuth data or server error text."""
    for attempt in range(max_retries + 1):
        if before_request is not None:
            before_request()
        try:
            response = client.request(method, url, **kwargs)
        except httpx.RequestError as exc:
            if method == "GET" and attempt < max_retries:
                delay = min(2**attempt, max_retry_wait)
                logger.warning("%s: network failure; retrying in %ss", label, delay)
                sleep(delay)
                continue
            raise SpotifyAPIError(f"{label}: network failure. Try again when connected.") from exc

        logger.debug("%s: HTTP %s, attempt %s", label, response.status_code, attempt + 1)
        if response.status_code == 429:
            try:
                payload = response.json()
                quota = (
                    isinstance(payload, dict)
                    and payload.get("error", {}).get("reason") == "QUOTA_EXCEEDED"
                )
            except (ValueError, AttributeError):
                quota = False
            try:
                delay = float(response.headers["Retry-After"])
                if not math.isfinite(delay) or delay < 0:
                    raise ValueError
            except (KeyError, ValueError):
                delay = None
            reason = "QUOTA_EXCEEDED" if quota else None
            if stop_on_rate_limit or quota:
                raise RateLimitError(
                    f"{label}: Spotify {'quota exceeded' if quota else 'rate limited'}."
                    + (
                        f" Retry-After: {delay:g} seconds."
                        if delay is not None
                        else " No reset time was supplied."
                    ),
                    retry_after=delay,
                    reason=reason,
                )
            wait = delay if delay is not None else min(2**attempt, max_retry_wait)
            if attempt == max_retries or wait > max_retry_wait:
                raise RateLimitError(
                    f"{label}: rate limited. Retry later"
                    + (f" (Retry-After: {wait:g} seconds)." if delay is not None else "."),
                    retry_after=delay,
                )
            wait = max(1.0, wait)
            logger.warning("%s: HTTP 429; waiting %gs before retry", label, wait)
            sleep(wait)
            continue

        if response.status_code >= 500 and method == "GET" and attempt < max_retries:
            delay = min(2**attempt, max_retry_wait)
            logger.warning("%s: HTTP %s; retrying in %ss", label, response.status_code, delay)
            sleep(delay)
            continue

        if not 200 <= response.status_code < 300:
            return response.status_code, {}
        if allow_empty and not response.content:
            return response.status_code, {}
        try:
            body = response.json()
        except ValueError as exc:
            raise SpotifyAPIError(f"{label}: invalid JSON response.") from exc
        if not isinstance(body, dict):
            raise SpotifyAPIError(f"{label}: expected a JSON object.")
        return response.status_code, body
    raise AssertionError("Unreachable retry state")
