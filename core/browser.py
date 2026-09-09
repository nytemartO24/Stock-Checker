"""Chromium fetching for hosts that refuse plain HTTP.

Separate from `sites/amazon/browser.py`, which is Amazon-specific plumbing
(delivery-location pinning, the glow modal, market warm-ups). This is the
generic part: launch a browser, fetch a page, hand back HTML — for the case
where a site answers 403 to httpx and 200 to a real browser.

WHY THIS EXISTS: every EU price aggregator worth reading — idealo.de,
geizhals.de, prisjakt.nu, pricespy, ledenicheur.fr — answers **403 to plain
HTTP from both the dev machine and the VPS**. That rules out IP reputation and
names the cause: DataDome/Cloudflare browser fingerprinting. These are also the
single highest-yield source available, because one product page lists every
retailer with an offer, which is exactly the question `discover_stores.py`
asks. So the browser is not a luxury here, it is the only door.

It obeys `core/http.Pacer` for the same reason `sites/amazon` does: a
Playwright navigation is a request to the site like any other, and the
polite-scraper rule in CLAUDE.md is per site, not per library.

Amazon's own launch predates this module and still has its own copy of the
channel fallback. Left alone deliberately — it is live in production and there
is nothing to gain from editing it today; fold it in next time it is touched.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Iterator
from urllib.parse import urlparse

from core.http import DEFAULT_USER_AGENT, Pacer

logger = logging.getLogger(__name__)

# Long enough for a bot-check interstitial to resolve itself. Aggregators
# routinely serve one, sit on it for a few seconds, then continue to the real
# page — reading the DOM before that finishes gets you the challenge, not the
# offers, and it looks exactly like an empty result.
CHALLENGE_WAIT_MS = 6000

# Text that means "we are checking your browser", not "here is your page".
CHALLENGE_MARKERS = (
    "checking your browser", "just a moment", "verifying you are human",
    "enable javascript and cookies", "captcha", "ddos protection",
    "überprüfung", "sicherheitsabfrage", "access denied",
)


class BrowserFetcher:
    """Fetches pages with a real browser, one page per navigation.

    A fresh page per fetch rather than reusing one: a navigation that fails
    leaves the page parked at `chrome-error://chromewebdata/`, where every
    subsequent goto lands again — the exact failure that cost 6 of 36 overnight
    Amazon runs (see CLAUDE.md). The browser and context are shared, so cookies
    and any solved challenge persist, which is most of the value.
    """

    def __init__(self, *, headless: bool = True, min_delay: float = 2.0,
                 max_delay: float = 5.0, locale: str = "de-DE") -> None:
        self._pacers: dict[str, Pacer] = {}
        self._bounds = (min_delay, max_delay)
        self.headless = headless
        self.locale = locale
        self._playwright = None
        self._browser = None
        self._context = None

    def start(self) -> None:
        from playwright.sync_api import sync_playwright

        self._playwright = sync_playwright().start()
        try:
            # Same reasoning as sites/amazon/browser.py: the branded build is
            # a different binary and behaves differently, but a missing channel
            # must not take the whole run down.
            self._browser = self._playwright.chromium.launch(
                headless=self.headless, channel="chromium")
        except Exception:
            logger.info("channel='chromium' unavailable — using the bundled build")
            self._browser = self._playwright.chromium.launch(headless=self.headless)
        self._context = self._browser.new_context(
            user_agent=DEFAULT_USER_AGENT,
            locale=self.locale,
            viewport={"width": 1440, "height": 900},
        )

    def close(self) -> None:
        for closer in (self._context, self._browser):
            try:
                if closer is not None:
                    closer.close()
            except Exception:
                pass
        try:
            if self._playwright is not None:
                self._playwright.stop()
        except Exception:
            pass

    def _pacer(self, url: str) -> Pacer:
        host = urlparse(url).netloc.lower()
        if host not in self._pacers:
            self._pacers[host] = Pacer(*self._bounds)
        return self._pacers[host]

    def fetch(self, url: str, *, wait_for: str | None = None,
              settle_ms: int = CHALLENGE_WAIT_MS) -> tuple[str, str | None]:
        """Return (html, error). Never raises — a scan must survive any host.

        `wait_for` is a selector to await before reading, for the same reason
        Amazon awaits its content selectors: an aggregator injects its offer
        list client-side, so reading immediately is a race whose loss looks
        like "this product has no offers".
        """
        if self._context is None:
            return "", "browser not started"
        self._pacer(url).wait()
        page = None
        try:
            page = self._context.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            self._dismiss_consent(page)
            if wait_for:
                try:
                    page.wait_for_selector(wait_for, timeout=settle_ms)
                except Exception:
                    logger.debug("%s: %r never appeared", url, wait_for)
            else:
                page.wait_for_timeout(settle_ms)
            html = page.content()
            low = html.lower()
            if len(html) < 20000 and any(m in low for m in CHALLENGE_MARKERS):
                # Reported rather than retried. A challenge page is a fact about
                # this host, and hammering it is both rude and useless.
                return html, "bot challenge not cleared"
            return html, None
        except Exception as e:  # noqa: BLE001 — every failure is data here
            return "", f"{type(e).__name__}: {e}"[:160]
        finally:
            if page is not None:
                try:
                    page.close()
                except Exception:
                    pass

    def fetch_pages(self, url: str, next_selector: str, *,
                    max_pages: int = 20, settle_ms: int = CHALLENGE_WAIT_MS,
                    ) -> tuple[list[str], str | None]:
        """HTML for each page of a paginated view, following `next_selector`.

        NECESSARY, NOT A CONVENIENCE: idealo's category listing is a single-page
        application. Its pager adds NO query parameter — `pageIndex`, `p`,
        `pageNumber`, `offset` and `resultsPerPage` are all silently ignored and
        return the byte-identical first page — so the only way to reach page 2
        is to click. A 16-page listing read as one page is an 94% loss reported
        as a complete answer, which is exactly the failure mode this project
        keeps meeting.

        Stops when the next control is gone, or when a page yields nothing new —
        the caller decides what "new" means, so this returns raw HTML per page
        and simply refuses to loop past `max_pages`.
        """
        if self._context is None:
            return [], "browser not started"
        self._pacer(url).wait()
        page = None
        pages: list[str] = []
        try:
            page = self._context.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            self._dismiss_consent(page)
            page.wait_for_timeout(settle_ms)
            for _ in range(max_pages):
                pages.append(page.content())
                control = page.locator(next_selector).first
                try:
                    if not control.count() or not control.is_visible(timeout=1500):
                        break
                    control.scroll_into_view_if_needed(timeout=3000)
                    control.click(timeout=5000)
                except Exception:
                    break
                # Pace between pages: clicking through a listing is still a
                # sequence of requests to the site.
                self._pacer(url).wait()
                page.wait_for_timeout(settle_ms)
            return pages, None
        except Exception as e:  # noqa: BLE001
            return pages, f"{type(e).__name__}: {e}"[:160]
        finally:
            if page is not None:
                try:
                    page.close()
                except Exception:
                    pass

    def _dismiss_consent(self, page) -> None:
        """Click a cookie wall if one is in the way.

        Not politeness theatre: on idealo and geizhals the consent layer is a
        full-page interstitial, and nothing behind it is in the DOM until it is
        cleared. Selectors are tried in order and failure is silent, because a
        site without a banner is the normal case.
        """
        for selector in (
            "#usercentrics-root >>> button[data-testid='uc-accept-all-button']",
            "button[data-testid='uc-accept-all-button']",
            "#onetrust-accept-btn-handler",
            "button#didomi-notice-agree-button",
            "[aria-label='Accept all']",
            "button:has-text('Alle akzeptieren')",
            "button:has-text('Akzeptieren')",
            "button:has-text('Godkänn alla')",
            "button:has-text('Accept all')",
        ):
            try:
                element = page.locator(selector).first
                if element.count() and element.is_visible(timeout=1200):
                    element.click(timeout=2500)
                    page.wait_for_timeout(600)
                    return
            except Exception:
                continue


@contextmanager
def browser_fetcher(**kwargs) -> Iterator[BrowserFetcher]:
    fetcher = BrowserFetcher(**kwargs)
    fetcher.start()
    try:
        yield fetcher
    finally:
        fetcher.close()
