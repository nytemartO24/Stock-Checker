"""Shared politeness layer. Every outbound request goes through here.

One client per site, not one globally: a Shopify collection answers in a
single JSON request while an Amazon product page costs a page load per
ASIN per market, so they cannot sensibly share a rate limit.

Cookies persist across requests by design. Amazon serves a *different page
layout* to a session that hasn't been warmed up on the market homepage —
selectors that work in a browser match nothing on a cookieless fetch, which
looks exactly like being blocked and isn't. Session reuse is the default so
that trap can't be re-hit by a future site module.
"""

from __future__ import annotations

import logging
import random
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Status codes that mean "you are going too fast" rather than "this request
# was wrong". Amazon uses 403 for throttling as readily as 429.
BACKOFF_STATUSES = frozenset({429, 403, 503})


class RateLimited(Exception):
    """Raised when a host keeps refusing after the retry budget is spent."""


class Pacer:
    """Enforces a randomised minimum gap between actions against one host.

    Extracted from PoliteClient because the browser transport needs exactly
    the same policy and cannot use an httpx client to get it — a Playwright
    navigation is a request to the site like any other, and the
    polite-scraper principle applies per site, not per library.
    """

    def __init__(self, min_delay: float = 1.0, max_delay: float = 3.0) -> None:
        if min_delay > max_delay:
            raise ValueError(f"min_delay {min_delay} exceeds max_delay {max_delay}")
        self.min_delay = min_delay
        self.max_delay = max_delay
        self._last_at: float | None = None

    def wait(self) -> None:
        """Sleep so consecutive actions are never closer than min_delay.

        Randomised: a perfectly regular interval is a bot signature, and the
        jitter costs nothing.
        """
        target = random.uniform(self.min_delay, self.max_delay)
        if self._last_at is not None:
            remaining = target - (time.monotonic() - self._last_at)
            if remaining > 0:
                time.sleep(remaining)
        self._last_at = time.monotonic()


class PoliteClient:
    """An httpx client that paces itself and backs off when asked to.

    Deliberately synchronous: sequential checking is fast enough at this
    scale, and async would buy throughput we've explicitly decided not to
    want (see the polite-scraper principle in CLAUDE.md).
    """

    def __init__(
        self,
        *,
        user_agent: str = DEFAULT_USER_AGENT,
        min_delay: float = 1.0,
        max_delay: float = 3.0,
        timeout: float = 30.0,
        max_retries: int = 3,
        accept_language: str = "en-GB,en;q=0.9",
    ) -> None:
        self.pacer = Pacer(min_delay, max_delay)
        self.max_delay = max_delay
        # A configured 0 means "don't retry", which is still one attempt.
        # Taken literally it would skip the request loop entirely and then
        # crash reporting a response that was never fetched.
        self.max_retries = max(1, max_retries)
        self._client = httpx.Client(
            headers={
                "User-Agent": user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": accept_language,
            },
            timeout=timeout,
            follow_redirects=True,
        )

    def __enter__(self) -> PoliteClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def get(self, url: str) -> httpx.Response:
        """GET with pacing and backoff. Raises on a non-2xx that isn't retryable."""
        delay = max(self.max_delay, 2.0)
        for attempt in range(1, self.max_retries + 1):
            self.pacer.wait()
            response = self._client.get(url)
            if response.status_code not in BACKOFF_STATUSES:
                response.raise_for_status()
                return response

            # Honour Retry-After when the server bothers to send one; it
            # knows better than our guess.
            retry_after = response.headers.get("Retry-After")
            wait = delay
            if retry_after and retry_after.isdigit():
                wait = float(retry_after)
            if attempt == self.max_retries:
                break
            logger.warning(
                "%s returned %s (attempt %d/%d) — backing off %.1fs",
                url, response.status_code, attempt, self.max_retries, wait,
            )
            time.sleep(wait)
            delay *= 2

        raise RateLimited(f"{url} still returning {response.status_code} after {self.max_retries} attempts")

    def get_json(self, url: str) -> Any:
        return self.get(url).json()
