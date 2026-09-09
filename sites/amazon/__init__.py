"""Amazon: the only site here with multiple vendors per listing.

Unlike the Shopify stores, availability alone isn't enough — a listing can
be perfectly purchasable at five times its real price. So this module also
maintains a reference price per ASIN and flags listings that sit far above
it (see prices.py). That logic lives HERE, not in core/: no other retailer
has the problem, and pushing it into shared code is exactly the smell
CLAUDE.md's principle 1 warns about.

Scope note: news-notifier tracked delivery DATES. This project needs
availability, price and seller, which is a much smaller job — no month
tables, no date parsing, no threshold/anchoring logic. Delivery LOCATION
still matters though, because whether a market will ship to you changes
availability, not just the date.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from bs4 import BeautifulSoup
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError, sync_playwright

from sites.amazon import browser as amazon_browser
from sites.amazon import dates as dates_mod
from sites.amazon.markets import MARKETS, NOT_DELIVERABLE_SIGNAL
from sites.amazon.prices import ReferencePrices, detect_currency, parse_price, to_sek
from sites.amazon.tiers import ceiling_for
from sites.base import SiteChecker, StockResult

logger = logging.getLogger(__name__)

# The buybox's own price containers. Deliberately NOT a bare
# ".a-price .a-offscreen": a product page is full of prices — "similar
# items" carousels, sponsored rows — any of which would look plausible
# while belonging to a different product entirely.
PRICE_SELECTORS = [
    "#corePriceDisplay_desktop_feature_div .a-offscreen",
    "#corePrice_feature_div .a-offscreen",
    "#apex_desktop .a-offscreen",
    "#priceblock_ourprice",
    "#priceblock_dealprice",
]

# Confirmed against real HTML for both an Amazon-sold and a third-party
# listing. A set of guessed fallbacks (#merchant-info, #tabular-buybox,
# #aod-offer-soldBy) was removed in the pilot: across every run they
# produced a seller name exactly never, while #merchant-info repeatedly
# showed up present-but-empty — so as a fallback it could only contribute
# an empty or non-seller string to a field that decides whether a price
# counts as Amazon's own.
SELLER_SELECTOR = "#merchantInfoFeature_feature_div"

# Availability copy is matched ONLY inside these, never against the whole
# page. Whole-page matching is a false-positive machine: a toy listing's
# details table almost always contains a "Release date" row, and a page
# whose buybox simply hadn't rendered would be confidently misread.
AVAILABILITY_SELECTORS = [
    "#availability",
    "#outOfStock",
    "#exports_desktop_outOfStock_buybox_feature_div",
    "#desktop_buybox",
    "#qualifiedBuybox",
    "#buybox",
]

BUYABLE_SELECTORS = ["#add-to-cart-button", "#buy-now-button"]

# The delivery promise. The date is searched ONLY inside whichever of these
# matches — never against the whole page. A whole-page search is a
# false-positive machine: "Reviewed in Spain on 21 January 2026" parses as a
# date, and because it carries an explicit year the assume-next-year
# correction never fires, so a stale unrelated date sails through looking
# real. The DEXUnifiedCXPDM attribute is Amazon's own unified
# delivery-promise container and survives the element-id churn that differs
# between markets.
DELIVERY_SELECTORS = [
    "#mir-layout-DELIVERY_BLOCK-slot-PRIMARY_DELIVERY_MESSAGE_LARGE",
    "#deliveryBlockMessage",
    "#contextualIngressPtLabel_deliveryShortDeliveryDate",
    "#deliveryMessageMirId",
    "#mir-layout-DELIVERY_BLOCK",
    '[data-csa-c-content-id="DEXUnifiedCXPDM"]',
    "#ddmDeliveryMessage",
    "#fast-track-message",
    "#dynamicDeliveryMessage",
]

INTERNATIONAL_BANNER = "international shopping transition alert"

# Wait for ANY of these before reading the page. Amazon injects the delivery,
# seller and price blocks client-side, AFTER domcontentloaded — confirmed
# against a real .de page whose raw server HTML had none of them despite being
# a normal purchasable listing. Reading immediately is a race, and losing it
# looks exactly like "this product has no date", which is a confidently wrong
# answer rather than a visible failure. news-notifier waits 6s here and its
# comment records that a fixed sleep was not enough even on .se.
CONTENT_SELECTORS = (
    DELIVERY_SELECTORS + AVAILABILITY_SELECTORS + BUYABLE_SELECTORS + [SELLER_SELECTOR]
)

# Parse the destination OUT of the banner rather than asking whether the
# country appears anywhere on the page. A whole-page check is useless here:
# once the location is pinned, the glow ingress names the destination on
# EVERY page, so "is Sweden mentioned?" is always true and the guard never
# fires. Anchored on "showing you" so it cannot match the banner's own
# second clause ("...to see items that dispatch to a different country").
INTERNATIONAL_DESTINATION_PATTERN = re.compile(
    r"showing you items that dispatch to\s+([^.]+?)\s*\.", re.IGNORECASE
)


def international_destination(page_text_lower: str) -> str:
    match = INTERNATIONAL_DESTINATION_PATTERN.search(page_text_lower)
    return " ".join(match.group(1).split()).title() if match else ""


@dataclass
class ParsedProduct:
    """What one product page said. `untrusted` means don't act on it."""

    title: str
    in_stock: bool
    price_text: str | None
    price_value: float | None
    currency: str | None
    seller: str | None
    is_amazon_seller: bool | None
    delivery_date: str | None = None
    delivery_days: int | None = None
    delivery_iso: str | None = None
    untrusted: str | None = None


def _first_text(soup, selectors: list[str]) -> str:
    for selector in selectors:
        for element in soup.select(selector):
            text = " ".join(element.get_text(" ", strip=True).split())
            if text:
                return text
    return ""


def parse_product(html: str, config: dict, *, delivery_country: str) -> ParsedProduct:
    """Read one product page. Pure — no browser, so it is testable offline."""
    soup = BeautifulSoup(html, "html.parser")

    title_el = soup.select_one("#productTitle")
    title = " ".join(title_el.get_text(" ", strip=True).split()) if title_el else ""

    page_text = soup.get_text(" ", strip=True).lower()
    # Amazon serves an "international shopping" variant when it geolocates
    # you outside the marketplace's country. Those pages quote prices and
    # availability for a DIFFERENT destination. When the banner names the
    # country we actually asked for, that IS the question being asked
    # ("can I get this from .de, to me?") and the page reads normally; when
    # it names another, nothing on it is about us.
    if INTERNATIONAL_BANNER in page_text:
        destination = international_destination(page_text)
        if destination.lower() != delivery_country.strip().lower():
            return ParsedProduct(
                title, False, None, None, None, None, None, None, None, None,
                untrusted=(f"page dispatches to {destination or 'an unknown country'}, "
                           f"not {delivery_country}"),
            )

    seller_text = ""
    seller_el = soup.select_one(SELLER_SELECTOR)
    if seller_el:
        name_el = seller_el.select_one(".offer-display-feature-text-message")
        seller_text = " ".join((name_el or seller_el).get_text(" ", strip=True).split())
    # A plain "amazon" substring is safe HERE because the text comes from
    # the seller-specific container — unlike the whole page, this string
    # never mentions Amazon for reasons other than being the seller.
    is_amazon = ("amazon" in seller_text.lower()) if seller_text else None

    price_text = _first_text(soup, PRICE_SELECTORS)
    availability = " ".join(
        " ".join(el.get_text(" ", strip=True).split())
        for selector in AVAILABILITY_SELECTORS
        for el in soup.select(selector)
    ).lower()

    buyable = any(soup.select_one(selector) for selector in BUYABLE_SELECTORS)
    unavailable = NOT_DELIVERABLE_SIGNAL in availability or any(
        signal in availability for signal in config["unavailable_signals"]
    )

    # Delivery date, scoped to a matched delivery container only.
    delivery_text = _first_text(soup, DELIVERY_SELECTORS)
    delivery_date = delivery_days = delivery_iso = None
    if delivery_text:
        match = dates_mod.pattern_for(config["months"]).search(delivery_text)
        if match:
            parsed = dates_mod.date_from_match(match, config["months"])
            # Sanity-check before trusting it: a bogus date here is worse than
            # none, because it becomes the baseline a future "moved earlier"
            # alert fires against.
            if dates_mod.is_plausible(parsed):
                delivery_date = " ".join(match.group().split()).strip().rstrip(",")
                delivery_days = dates_mod.days_until(parsed)
                # Stored so comparison never re-parses a yearless string.
                delivery_iso = parsed.isoformat()

    return ParsedProduct(
        title=title,
        # Pre-orders count: Amazon renders them with the same add-to-cart
        # button, which is exactly why this checks the button rather than
        # trying to distinguish "in stock" from "orderable".
        in_stock=buyable and not unavailable,
        price_text=price_text or None,
        price_value=parse_price(price_text),
        # Read off the page, with the market's home currency only as a
        # fallback — a cross-border .de page pinned to Sweden quotes SEK.
        currency=detect_currency(price_text, config["currency"]) if price_text else None,
        seller=seller_text or None,
        is_amazon_seller=is_amazon,
        delivery_date=delivery_date,
        delivery_days=delivery_days,
        delivery_iso=delivery_iso,
    )


def apply_delivery_window(in_stock: bool, delivery_days: int | None,
                          delivery_date: str | None,
                          max_days: int | None) -> tuple[bool, str | None]:
    """Treat an estimate beyond `max_days` as not available yet.

    A long estimate is itself a form of unavailability: an add-to-cart button
    and a date six months out is not something you can have.

    Returns (in_stock, note). Setting in_stock False rather than merely muting
    the alert is the point — the estimate later coming inside the window then
    reads as an ordinary out-of-stock -> in-stock transition, so you are told
    when the item becomes ACTUALLY available. Muting via `alertable` would be
    worse twice over: it would also gag the date-moved-earlier alert, which is
    the very signal that matters here.

    Nothing is hidden — the note states the real position, and the result still
    carries the date.
    """
    if not in_stock or not max_days or delivery_days is None:
        return in_stock, None
    if delivery_days <= int(max_days):
        return in_stock, None
    return False, (
        f"orderable, but the estimate is {delivery_days} days out "
        f"({delivery_date}), beyond the {max_days}-day window — "
        f"treated as not available yet"
    )


class AmazonChecker(SiteChecker):
    """Options: markets, watchlist, delivery_country, delivery_postcode,
    scalp_multiplier, alert_on_suspected_scalp, headless, max_price_sek."""

    @property
    def markets(self) -> list[str]:
        return self.options.get("markets") or ["se"]

    @property
    def watchlist(self) -> list[str]:
        return [str(a).strip().upper() for a in (self.options.get("watchlist") or []) if str(a).strip()]

    @property
    def multiplier(self) -> float:
        return float(self.options.get("scalp_multiplier", 2.0))

    def _state_path(self, kind: str) -> Path:
        """Auxiliary state this site owns, beyond core's alert state.

        Project root, not the process CWD: a run started elsewhere would
        otherwise split accumulated knowledge across files.
        """
        base = self.state_dir or Path(__file__).resolve().parents[2] / "state"
        return base / f"{self.name}_{kind}.json"

    def _reference_path(self) -> Path:
        return self._state_path("reference_prices")

    def check(self) -> Iterator[StockResult]:
        if not self.watchlist:
            # Counts as an error so main.py does NOT prune: an empty
            # watchlist means "we saw nothing", not "nothing exists", and
            # pruning against it wipes every stored product.
            self.errors += 1
            logger.warning("[%s] watchlist is empty — nothing to check", self.name)
            return

        references = ReferencePrices(
            self._reference_path(), overrides=self.options.get("max_price_sek") or {}
        )
        deliveries = dates_mod.DeliveryState(
            self._state_path("delivery"),
            min_improvement_days=int(self.options.get(
                "min_improvement_days", dates_mod.DEFAULT_MIN_IMPROVEMENT_DAYS)),
        )
        self._seen_keys: set[str] = set()
        try:
            with sync_playwright() as playwright:
                for market in self.markets:
                    if market not in MARKETS:
                        self.errors += 1
                        logger.error("[%s] unknown market %r (known: %s)",
                                     self.name, market, ", ".join(sorted(MARKETS)))
                        continue
                    try:
                        yield from self._check_market(playwright, market, references, deliveries)
                    except Exception:
                        # One market failing must not lose the others, and
                        # must mark the run incomplete so state isn't pruned
                        # against a partial view.
                        self.errors += 1
                        logger.exception("[%s] market %r failed", self.name, market)
        finally:
            # Save whatever was learned even if a market blew up — a
            # reference price observed before the failure is still valid.
            references.save()
            if not self.errors:
                deliveries.prune(self._seen_keys)
            deliveries.save()

    def _check_market(self, playwright, market: str, references: ReferencePrices,
                      deliveries) -> Iterator[StockResult]:
        config = MARKETS[market]
        # Destination comes from the environment first. The postcode is
        # personal data (it identifies a town), so it lives in gitignored
        # .env rather than the committed config, and there is deliberately
        # no default — a missing one should be a loud failure to pin the
        # location, not a silent fallback to someone else's address.
        country = os.environ.get("DELIVERY_COUNTRY") or self.options.get("delivery_country", "Sweden")
        postcode = str(os.environ.get("DELIVERY_POSTCODE") or self.options.get("delivery_postcode", ""))
        if not postcode:
            logger.warning(
                "[%s] no DELIVERY_POSTCODE set — the domestic market cannot pin a "
                "precise address; set it in .env", self.name)

        logger.info("[%s] %s: checking %d product(s)", self.name, market, len(self.watchlist))
        browser_handle, page, location, pinned = amazon_browser.open_market(
            playwright, market, config,
            country=country, postcode=postcode,
            headless=bool(self.options.get("headless", True)),
        )
        if not pinned:
            # Availability is meaningless without a destination: Amazon has
            # geolocated the runner instead of answering our question. Don't
            # prune against this view, and label anything it produces.
            self.errors += 1
        location_note = None if pinned else (
            f"delivery location not applied (reads {location or 'nothing'!r}) — "
            f"availability may describe a destination other than {country}"
        )
        try:
            for asin in self.watchlist:
                try:
                    result = self._check_one(page, market, config, asin, references, country,
                                             deliveries, location_note=location_note)
                except Exception:
                    self.errors += 1
                    logger.exception("[%s] %s %s failed", self.name, market, asin)
                    continue
                if result is not None:
                    yield result
        finally:
            browser_handle.close()
            logger.info("[%s] %s: done (delivering to %s)", self.name, market, location or "UNKNOWN")

    def _check_one(self, page, market: str, config: dict, asin: str,
                   references: ReferencePrices, country: str, deliveries,
                   *, location_note: str | None = None) -> StockResult | None:
        url = f"https://www.{config['domain']}/-/en/dp/{asin}"
        # Browser navigations are requests to the site like any other, so
        # they go through the same politeness policy as the JSON transports.
        self.client.pacer.wait()
        amazon_browser.safe_goto(page, url, market)

        # A goto can silently land somewhere other than the product page
        # (the pilot caught a run reading the plain .se homepage as "the
        # delivery block just isn't there"). Verify before trusting content.
        if asin not in page.url:
            amazon_browser.safe_goto(page, url, market)
            if asin not in page.url:
                self.errors += 1
                logger.warning("[%s] %s %s: landed on %s — skipping", self.name, market, asin, page.url)
                return None

        # Give the client-side blocks a chance to arrive before reading. On
        # timeout, fall through and parse anyway — the page may legitimately
        # have none of them (an unavailable listing has no delivery block), and
        # the parser reports that honestly.
        try:
            page.wait_for_selector(", ".join(CONTENT_SELECTORS), timeout=6000)
        except PlaywrightTimeoutError:
            logger.info("[%s] %s %s: no target selector within 6s — parsing as-is",
                        self.name, market, asin)

        parsed = parse_product(page.content(), config, delivery_country=country)
        if parsed.untrusted:
            # Not a result: recording it would let a wrong-destination page
            # become the baseline a future alert fires against.
            self.errors += 1
            logger.warning("[%s] %s %s: %s", self.name, market, asin, parsed.untrusted)
            return None

        price_sek = to_sek(parsed.price_value, parsed.currency)
        references.observe(asin, price_sek, is_amazon_seller=parsed.is_amazon_seller)
        # Title-derived tier is only a FALLBACK — assess() prefers a real
        # per-ASIN observation and only reaches for this when there isn't one.
        tier, tier_ceiling = ceiling_for(parsed.title, self.options.get("tier_ceilings_sek") or {})
        verdict = references.assess(asin, price_sek, self.multiplier,
                                    tier=tier, tier_ceiling=tier_ceiling)

        # Delivery date: track it, and let the site REQUEST an alert when it
        # moves meaningfully earlier. That is not a stock transition, so core's
        # own rules would never surface it — a listing can sit "in stock" for
        # weeks while its estimate walks from November to next Tuesday, which
        # is the difference between unavailable and buyable.
        key = f"{market}:{asin}"
        self._seen_keys.add(key)
        baseline, improved = deliveries.observe(key, parsed.delivery_date, parsed.delivery_iso)
        alert_reason = None
        if improved:
            alert_reason = (
                f"delivery date moved earlier: {baseline} → {parsed.delivery_date}"
                if baseline else f"delivery date now promised: {parsed.delivery_date}"
            )

        notes = [verdict.note] if verdict.note else []
        if location_note:
            notes.append(location_note)

        in_stock, window_note = apply_delivery_window(
            parsed.in_stock, parsed.delivery_days, parsed.delivery_date,
            self.options.get("max_delivery_days"),
        )
        if window_note:
            notes.append(window_note)

        alertable = not (verdict.suspected and not self.options.get("alert_on_suspected_scalp", True))

        logger.info(
            "[%s] %s %s: %s%s%s%s", self.name, market, asin,
            "IN STOCK" if in_stock else ("too far out" if parsed.in_stock else "unavailable"),
            f" @ {parsed.price_text}" if parsed.price_text else "",
            f" arrives {parsed.delivery_date} (+{parsed.delivery_days}d)"
            if parsed.delivery_date else " no date",
            f" [{parsed.seller}]" if parsed.seller else "",
        )
        return StockResult(
            # Namespaced by market: the same ASIN restocking on .de and .se
            # are two separate events you'd want telling about separately.
            product_id=f"{market}:{asin}",
            product_name=parsed.title or asin,
            url=f"https://www.{config['domain']}/dp/{asin}",
            in_stock=in_stock,
            price_text=parsed.price_text,
            price_value=parsed.price_value,
            currency=parsed.currency,
            seller=parsed.seller,
            delivery_date=parsed.delivery_date,
            alertable=alertable,
            alert_reason=alert_reason,
            notes=notes,
        )
