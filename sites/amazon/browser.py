"""Chromium plumbing for Amazon: getting a usable, correctly-located page.

Ported from news-notifier's pilot/eu_multimarket/browser.py, which took
several rounds of live debugging against real Amazon markup to get right.
Kept faithful deliberately — the selectors and the choice of mechanism
below are load-bearing findings, not preferences. Notably:

* The warm-up navigates to the "/-/en/" HOMEPAGE, not the bare domain. The
  bare domain sets the session language cookie to the market's native
  language, and a cookieless request to a product URL gets served the
  NATIVE layout where none of these selectors match. That looks exactly
  like being blocked and is not.
* Location pinning uses postcode on the domestic market and the country
  <select> elsewhere, with NO cross-fallback: the foreign markets validate
  their postcode field against their own country, so neither mechanism can
  do the other's job and "try the other one" could only turn a clear
  failure into a confusing one.
* #GLUXCountryList is a real <select>, not a list of links. An earlier
  version looked for "#GLUXCountryList li a" and matched nothing, ever.
"""

from __future__ import annotations

import logging
import re

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

GLOW_INGRESS_SELECTOR = "#glow-ingress-line2"
GLOW_OPENER_SELECTOR = "#nav-global-location-popover-link"


def dismiss_cookie_banner(page, market: str) -> bool:
    """Amazon's EU cookie-consent banner. On some markets it is a full-page
    interstitial rather than an overlay, in which case nothing downstream
    can find anything until it is cleared."""
    try:
        button = page.locator("#sp-cc-accept")
        if button.count() == 0:
            return False
        try:
            # Short timeout: the 30s default is pure waste. Seen live on
            # amazon.com.be, where a #redir-modal backdrop sat over the
            # banner and Playwright retried the click for the full 30s.
            button.first.click(timeout=5000)
        except PlaywrightTimeoutError:
            # Visible and enabled, just overlaid. A DOM-level click ignores
            # the overlay where a synthetic one cannot.
            logger.info("[%s] cookie banner intercepted — clicking via DOM", market)
            button.first.evaluate("el => el.click()")
        page.wait_for_timeout(1000)
        return True
    except Exception as e:
        logger.warning("[%s] cookie banner dismissal failed: %s", market, e)
        return False


def dismiss_interstitial(page, market: str) -> bool:
    """Amazon's "Continue shopping" / captcha-form interstitial."""
    try:
        form = page.locator("form[action='/errors_page/validateCaptcha']")
        if form.count() == 0:
            return False
        button = page.locator("form[action='/errors_page/validateCaptcha'] button[type='submit']")
        if button.count() == 0:
            logger.info("[%s] interstitial had no submit button", market)
            return False
        button.first.click()
        page.wait_for_load_state("domcontentloaded", timeout=15000)
        page.wait_for_timeout(1500)
        logger.info("[%s] interstitial dismissed", market)
        return True
    except Exception as e:
        logger.warning("[%s] interstitial dismissal failed: %s", market, e)
        return False


def safe_goto(page, url: str, market: str) -> None:
    """Navigate, tolerate Amazon's spurious download prompt, then settle.

    Every navigation must go through this. A real pilot run showed why: two
    products hit "Page.goto: Download is starting" on a retry, which
    propagated out and killed the item even though the initial navigation
    had handled the identical condition fine three times in the same run.
    """
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
    except Exception as e:
        if "Download is starting" not in str(e):
            raise
        logger.info("[%s] spurious download prompt on navigation — continuing", market)
        page.wait_for_timeout(1000)

    page.wait_for_timeout(1500)
    dismiss_cookie_banner(page, market)
    for _ in range(2):
        if not dismiss_interstitial(page, market):
            break


def read_delivery_location(page) -> str:
    try:
        ingress = page.locator(GLOW_INGRESS_SELECTOR)
        if ingress.count() == 0:
            return ""
        return " ".join(ingress.first.inner_text().split())
    except Exception:
        return ""


def _fill_postcode(page, market: str, postcode: str) -> bool:
    """amazon.se splits the postcode across TWO inputs (maxlength 3 + 2, for
    "371 16"); other markets use one. The prefix selector matches both, and
    digits are distributed by each field's own maxlength rather than a
    hardcoded 3/2."""
    digits = re.sub(r"\D", "", postcode)
    fields = page.locator("[id^='GLUXZipUpdateInput']")
    count = fields.count()
    if count == 0:
        logger.warning("[%s] no postcode field in this modal", market)
        return False

    # DOM order should be _0, _1 but sort on the id suffix rather than trust
    # it — filling out of order silently produces a different but
    # structurally valid postcode instead of an error.
    indexed = []
    for i in range(count):
        element = fields.nth(i)
        suffix = (element.get_attribute("id") or "").rsplit("_", 1)[-1]
        indexed.append((int(suffix) if suffix.isdigit() else i, element))
    indexed.sort()

    offset = 0
    for _, element in indexed:
        maxlength = element.get_attribute("maxlength")
        take = int(maxlength) if maxlength and maxlength.isdigit() else len(digits) - offset
        element.fill(digits[offset:offset + take])
        offset += take
    return True


def _select_country(page, market: str, country: str) -> bool:
    select = page.locator("#GLUXCountryList")
    if select.count() == 0:
        logger.warning("[%s] no country picker (#GLUXCountryList) in this modal", market)
        return False
    labels = select.first.evaluate("el => Array.from(el.options).map(o => o.text.trim())")
    if country not in labels:
        logger.warning("[%s] %r not in this market's country list (%d options)", market, country, len(labels))
        return False
    # Exact label match, never substring: "United States" must not select
    # "United States Minor Outlying Islands".
    select.first.select_option(label=country, timeout=5000)
    return True


def set_delivery_location(page, market: str, config: dict, country: str, postcode: str) -> str:
    """Pin the destination. Never raises; returns whatever the widget says.

    A failure here doesn't invalidate the run, it just means results
    describe Amazon's guessed destination instead of the requested one — so
    the caller logs the discrepancy loudly rather than silently comparing
    incomparable answers.
    """
    domestic = config["country"].strip().lower() == country.strip().lower()
    try:
        opener = page.locator(GLOW_OPENER_SELECTOR)
        if opener.count() == 0:
            logger.warning("[%s] no location picker on this page", market)
            return read_delivery_location(page)
        opener.first.click(timeout=5000)
        page.wait_for_timeout(1500)

        if domestic:
            if not _fill_postcode(page, market, postcode):
                return read_delivery_location(page)
            # #GLUXZipUpdate is a <span> wrapping the real submit input.
            page.locator("#GLUXZipUpdate input.a-button-input").first.click(timeout=5000)
        elif not _select_country(page, market, country):
            return read_delivery_location(page)

        # Applying swaps in a success panel whose Continue button starts
        # hidden inside #GLUXHiddenSuccessDialog — it becoming visible is
        # itself confirmation Amazon accepted the change, not merely that
        # we clicked something.
        page.wait_for_timeout(1500)
        for selector in ("#GLUXConfirmClose", "[name='glowDoneButton']"):
            button = page.locator(selector)
            if button.count() and button.first.is_visible():
                button.first.click(timeout=5000)
                break

        # Applied server-side against the session; reload so everything
        # downstream reflects it.
        page.wait_for_timeout(2000)
        page.reload(wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(1500)
    except Exception as e:
        logger.warning("[%s] could not set delivery location: %s", market, e)

    return read_delivery_location(page)


def open_market(playwright, market: str, config: dict, *, country: str, postcode: str, headless: bool = True):
    """Launch a browser for one marketplace, warmed up and pinned.

    Returns (browser, page, location, pinned). Close the browser yourself.

    `pinned` is False when the destination could not be applied, which the
    caller MUST act on rather than merely log: availability read from an
    unpinned session describes wherever Amazon guessed, so it is not
    comparable to the other markets and shouldn't be pruned against.
    """
    browser = playwright.chromium.launch(headless=headless)
    page = browser.new_context(user_agent=USER_AGENT, locale=f"en-{market.upper()}").new_page()

    warmup_url = f"https://www.{config['domain']}/-/en/"
    location = ""
    pinned = False
    try:
        # MUST go through safe_goto, not a bare page.goto: Amazon throws a
        # spurious "Download is starting" on navigation, and an unprotected
        # warm-up loses the ENTIRE market when it hits — location never gets
        # pinned, so every product that market returns describes a
        # destination we didn't ask for. Seen live on .se. safe_goto also
        # clears the cookie banner and interstitial, which this used to do
        # itself.
        # Verify the warm-up actually landed on a usable page before trying
        # to pin anything. news-notifier applies this check to product pages
        # ("landed on X instead of the product page — retrying") but never to
        # the warm-up, and .se showed why it is needed here too: after an
        # aborted download-prompt navigation the page can be mid-transition
        # or still showing an interstitial, so the location widget does not
        # exist yet. Pinning then fails with "no location picker", and the
        # interstitial gets dismissed seconds later by the first product's
        # navigation — too late to matter.
        for attempt in range(1, 3):
            safe_goto(page, warmup_url, market)
            try:
                page.wait_for_selector(GLOW_OPENER_SELECTOR, timeout=8000)
                break
            except PlaywrightTimeoutError:
                logger.warning(
                    "[%s] warm-up landed on %r with no location picker — retrying (%d/2)",
                    market, page.url, attempt,
                )
        else:
            logger.warning(
                "[%s] no location picker after 2 warm-up attempts; the destination "
                "cannot be pinned and this market's results are not comparable", market,
            )
        location = set_delivery_location(page, market, config, country, postcode)

        # Substring check both ways: the widget renders the country alone
        # for international ("Sweden") but city + postcode for domestic
        # ("Karlskrona 371 16"), so neither is a prefix of a fixed string.
        pinned = bool(location) and (
            country.lower() in location.lower()
            or postcode.replace(" ", "") in location.replace(" ", "")
        )
        if pinned:
            logger.info("[%s] delivery location confirmed: %r", market, location)
        else:
            logger.warning(
                "[%s] DELIVERY LOCATION NOT APPLIED — widget reads %r, wanted %s/%s. "
                "Results from this market describe Amazon's guessed destination.",
                market, location, country, postcode,
            )
    except Exception as e:
        logger.warning("[%s] warm-up failed: %s", market, e)

    return browser, page, location, pinned
