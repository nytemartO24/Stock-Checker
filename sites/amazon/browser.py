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
from dataclasses import dataclass

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


MODAL_CONTROLS = "#GLUXCountryList, [id^='GLUXZipUpdateInput']"
POPOVER = ".a-popover-inner"
POPOVER_CLOSE = ("button[data-action='a-popover-close']", ".a-popover .a-button-close")


@dataclass
class Destination:
    """What this session will quote availability FOR.

    Two separate questions, and conflating them is what made amazon.se's 2026-09
    collapse so expensive:

    `usable` — is the COUNTRY right? Availability from a session pointed at the
    wrong country is not comparable to the other markets and must not be pruned
    against. This is the one that matters.

    `exact` — was the postcode applied on top? It only sharpens the delivery
    ESTIMATE from country-level to city-level. Losing it costs precision, not
    correctness.
    """

    text: str
    """Whatever the location widget reads, for the log and the alert."""

    usable: bool
    exact: bool
    note: str | None = None
    """Set when something is degraded; rendered in the alert verbatim."""


def dismiss_popover(page, market: str) -> bool:
    """Close whatever popover is open, so a retry can actually re-click.

    THIS IS THE BUG THAT MADE THE RETRY LOOP USELESS on amazon.se. When the
    location fragment fails, Amazon still opens the popover — containing
    "Sorry, content is not available." — and it COVERS the opener. Every
    subsequent `opener.click()` then dies with TimeoutError, so attempts 2 and 3
    never even reached the check. The production log shows exactly that shape:
    one "popover open but no control", then two "opener click failed
    (TimeoutError)". The loop's docstring measured the OTHER failure (popover
    never opened), where re-clicking is the right move.
    """
    if not page.locator(POPOVER).count():
        return False
    for selector in POPOVER_CLOSE:
        control = page.locator(selector)
        if control.count():
            try:
                control.first.click(timeout=3000)
                page.wait_for_timeout(400)
                if not page.locator(POPOVER).count():
                    return True
            except Exception:
                pass
    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(400)
    except Exception:
        pass
    return not page.locator(POPOVER).count()


def _open_location_modal(page, market: str, attempts: int = 3) -> bool:
    """Click the location opener until the modal is actually there.

    MEASURED, not guessed — and the measurement overturned the obvious theory.
    Roughly 1 run in 8 failed to pin a location, logging "modal did not fill in
    within 8s", which reads like the contents rendering too slowly. They do not:
    instrumenting the real code path on the VPS showed the controls present at
    **1000ms** whenever the modal opens, and byte-identical at 15s. A longer
    wait would have changed nothing.

    What actually happens is that `.a-popover-inner` count stays at ZERO — the
    modal never opens. The opener exists, the click raises nothing, there is no
    captcha and the page is normal; the click simply lands before Amazon's
    handler is bound. Over 14 production-equivalent runs: 13 opened on the first
    click, and the one that did not RECOVERED when the page was allowed to settle
    and the opener clicked again.

    So the fix is to verify and re-click, not to wait longer. Waiting for
    `networkidle` before retrying is what the successful recovery did, so it is
    kept rather than trimmed.
    """
    opener = page.locator(GLOW_OPENER_SELECTOR)
    for attempt in range(1, attempts + 1):
        try:
            opener.first.click(timeout=5000)
        except Exception as e:
            logger.warning("[%s]   location opener click failed (%d/%d): %s",
                           market, attempt, attempts, type(e).__name__)
        try:
            # Short, because when it opens at all it is populated in ~1s.
            # A long timeout here only delays the retry that actually helps.
            page.wait_for_selector(MODAL_CONTROLS, timeout=3000)
            page.wait_for_timeout(400)
            return True
        except PlaywrightTimeoutError:
            pass

        opened = 0
        try:
            opened = page.locator(POPOVER).count()
        except Exception:
            pass
        # Distinguish the two failures rather than logging one message for both.
        # "popover open but no controls" is Amazon failing to serve the location
        # fragment (it renders "Sorry, content is not available."); no popover at
        # all is the click-too-early case this loop was built for.
        logger.warning("[%s]   location modal not usable (attempt %d/%d): "
                       "%s — retrying", market, attempt, attempts,
                       "popover open but no postcode/country control" if opened
                       else "popover did not open at all")
        if attempt < attempts:
            # MUST come before the next click. A broken popover covers the
            # opener, so without this every remaining attempt dies with
            # TimeoutError and the retry loop cannot help at all.
            if opened:
                dismiss_popover(page, market)
            try:
                page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                page.wait_for_timeout(1500)
    logger.warning("[%s] location modal never opened after %d clicks", market, attempts)
    return False


def set_delivery_location(page, market: str, config: dict, country: str,
                          postcode: str) -> Destination:
    """Pin the destination. Never raises; reports what it actually achieved.

    A FAILURE MEANS DIFFERENT THINGS ON A FOREIGN AND A DOMESTIC MARKETPLACE,
    and treating them alike is what made amazon.se's collapse cost far more than
    it should have.

    On a FOREIGN market the country picker is the whole mechanism: if it does not
    apply, the session describes whatever country Amazon geolocated, so the
    results are not comparable and must not be pruned against. Unusable.

    On the DOMESTIC market the fallback IS the country we want — amazon.se serves
    Sweden by default. Measured 2026-09-26 on an unpinned .se session: prices
    quoted in SEK, a normal Swedish delivery promise, and no international
    shopping banner. The postcode only sharpens the estimate from country-level
    to Karlskrona-level. So this is degraded precision, not a wrong answer.

    That is safe rather than merely convenient because the wrong-destination case
    is caught INDEPENDENTLY downstream: parse_product marks a page untrusted when
    the international banner names a country other than the one we asked for. If
    amazon.se ever does start answering for somewhere else, that guard fires
    whatever this function concluded.
    """
    domestic = config["country"].strip().lower() == country.strip().lower()
    try:
        opener = page.locator(GLOW_OPENER_SELECTOR)
        if opener.count() == 0:
            logger.warning("[%s] no location picker on this page", market)
            return read_delivery_location(page)
        if not _open_location_modal(page, market):
            return read_delivery_location(page)

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

    return classify_destination(read_delivery_location(page), market,
                                domestic=domestic, country=country, postcode=postcode)


def classify_destination(text: str, market: str, *, domestic: bool, country: str,
                         postcode: str) -> Destination:
    """Turn what the widget reads into the two answers that matter.

    Pure, so the rules are testable without a browser — which they need to be,
    because they decide whether a market's results may be pruned against.
    """
    # Substring check both ways: the widget renders the country alone for an
    # international destination ("Sweden") but city + postcode for a domestic one
    # ("Karlskrona 371 16"), so neither is a prefix of a fixed string.
    matched = bool(text) and (
        country.strip().lower() in text.lower()
        or (postcode.replace(" ", "") and postcode.replace(" ", "") in text.replace(" ", ""))
    )
    if matched:
        return Destination(text, usable=True, exact=True)
    if domestic:
        return Destination(
            text, usable=True, exact=False,
            note=(f"postcode not applied on the domestic market (widget reads "
                  f"{text or 'nothing'!r}) — delivery estimates are country-level "
                  f"for {country} rather than {postcode or 'your postcode'}"),
        )
    return Destination(
        text, usable=False, exact=False,
        note=(f"delivery location not applied (reads {text or 'nothing'!r}) — "
              f"availability may describe a destination other than {country}"),
    )


def open_market(playwright, market: str, config: dict, *, country: str, postcode: str, headless: bool = True):
    """Launch a browser for one marketplace, warmed up and pinned.

    Returns (browser, page, Destination). Close the browser yourself.

    `Destination.usable` False means the COUNTRY could not be applied, which the
    caller MUST act on rather than merely log: availability read from such a
    session describes wherever Amazon guessed, so it is not comparable to the
    other markets and must not be pruned against. `exact` False is only a loss of
    precision — see set_delivery_location.
    """
    # news-notifier launches with channel="chromium" — the branded build
    # rather than Playwright's bundled one — and ran for months that way. It is
    # a different binary, so download-prompt and navigation behaviour can
    # genuinely differ, and .se's chrome-error failures are exactly that kind
    # of symptom. Tried first, but NEVER at the cost of the run: if that
    # channel is not installed, launching would fail and take the whole market
    # with it, which is far worse than a behavioural difference.
    try:
        browser = playwright.chromium.launch(headless=headless, channel="chromium")
    except Exception as e:
        logger.info("[%s] chromium channel unavailable (%s) — using the bundled build",
                    market, type(e).__name__)
        browser = playwright.chromium.launch(headless=headless)
    context = browser.new_context(user_agent=USER_AGENT, locale=f"en-{market.upper()}")
    page = context.new_page()

    warmup_url = f"https://www.{config['domain']}/-/en/"
    domestic = config["country"].strip().lower() == country.strip().lower()
    destination = classify_destination("", market, domestic=domestic,
                                       country=country, postcode=postcode)
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
        # Overnight this failed on .se in 6 of 36 runs, always the same way:
        # Amazon's spurious "Download is starting" aborts the navigation and
        # leaves the page at chrome-error://chromewebdata/. safe_goto tolerates
        # the exception, but re-navigating THAT page lands on chrome-error
        # again — so the original retry, which only repeated the goto, could
        # never recover. Replacing the page escapes the broken state; keeping
        # the same context keeps the cookies, which is the whole point of
        # having warmed up.
        attempts = 3
        for attempt in range(1, attempts + 1):
            safe_goto(page, warmup_url, market)
            try:
                page.wait_for_selector(GLOW_OPENER_SELECTOR, timeout=8000)
                break
            except PlaywrightTimeoutError:
                logger.warning(
                    "[%s] warm-up landed on %r with no location picker — retrying (%d/%d)",
                    market, page.url, attempt, attempts,
                )
                if attempt < attempts:
                    page.close()
                    page = context.new_page()
        else:
            logger.warning(
                "[%s] no location picker after 2 warm-up attempts; the destination "
                "cannot be pinned and this market's results are not comparable", market,
            )
        destination = set_delivery_location(page, market, config, country, postcode)

        if destination.exact:
            logger.info("[%s] delivery location confirmed: %r", market, destination.text)
        elif destination.usable:
            # Worth a warning, not an error: the country is right, so the market
            # is still comparable and still prunable.
            logger.warning(
                "[%s] postcode not applied — widget reads %r. The domestic store "
                "already answers for %s, so results stand; delivery estimates are "
                "country-level rather than for %s.",
                market, destination.text, country, postcode or "your postcode",
            )
        else:
            logger.warning(
                "[%s] DELIVERY LOCATION NOT APPLIED — widget reads %r, wanted %s/%s. "
                "Results from this market describe Amazon's guessed destination.",
                market, destination.text, country, postcode,
            )
    except Exception as e:
        logger.warning("[%s] warm-up failed: %s", market, e)

    return browser, page, destination
