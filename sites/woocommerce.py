"""WooCommerce storefronts, parsed from their server-rendered listing pages.

A THIRD transport shape, alongside the Shopify JSON API and Amazon's browser:
plain HTTP plus HTML parsing, no browser. WooCommerce renders everything we
need server-side, so this is cheap — one request returns a page of ~40
products complete with stock state and price.

WHY NOT AN API: gameshop.se runs WooCommerce, whose Store API
(/wp-json/wc/store/v1/products) would be ideal — but it answers 403 there,
Cloudflare-blocked even with browser headers. The WordPress core API
(/wp-json/wp/v2/product) does answer, but returns post data only: no price,
no stock status, which is precisely what we need. The HTML is the only route
that carries the whole answer, and it carries it well.

WHY THE MARKUP IS TRUSTWORTHY: WooCommerce stamps each product tile with
`instock` or `outofstock` as a CSS class. That is the shop's own state, not
something inferred from wording or a missing button — the same quality of
signal as Shopify's `available` flag, and much better than guessing from
prose the way the Amazon module must.
"""

from __future__ import annotations

import logging
from typing import Iterator
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from core.parsing import parse_price
from sites.base import SiteChecker, StockResult

logger = logging.getLogger(__name__)

# A full listing page. Fewer tiles than this means the last page.
PAGE_SIZE = 40
MAX_PAGES = 15

# The product tile. WooCommerce themes vary, so match either shape.
TILE_SELECTOR = "li.product, div.product"
TITLE_SELECTORS = [".woocommerce-loop-product__title", "h2", "h3"]


class WooCommerceChecker(SiteChecker):
    """Options: domain (required), listing_path (required), country, currency,
    currency_symbol, watchlist, title_include, title_exclude.

    `listing_path` is whatever URL lists the products, typically a search:
    `/?s=beyblade&post_type=product`. On gameshop.se that returns everything
    across 4 pages — in-stock first, then out-of-stock — so a listing sorted
    this way still shows the out-of-stock items, which is essential: we need to
    SEE a product while it is unavailable in order to notice it coming back.
    """

    @property
    def domain(self) -> str:
        return self.options["domain"]

    @property
    def watchlist(self) -> set[str]:
        """Product slugs worth being NOTIFIED about. Empty means everything.

        Slugs, like Shopify handles: exact, unique, per store, and visible in
        the product URL so they can be looked up rather than guessed.
        """
        return {s.strip().lower() for s in (self.options.get("watchlist") or []) if s.strip()}

    @property
    def title_include(self) -> list[str]:
        return [t.lower() for t in (self.options.get("title_include") or [])]

    @property
    def title_exclude(self) -> list[str]:
        return [t.lower() for t in (self.options.get("title_exclude") or [])]

    def wanted(self, title: str) -> bool:
        low = (title or "").lower()
        if any(bad in low for bad in self.title_exclude):
            return False
        return not self.title_include or any(good in low for good in self.title_include)

    def is_watched(self, slug: str) -> bool:
        return not self.watchlist or (slug or "").lower() in self.watchlist

    def _page_url(self, page: int) -> str:
        path = self.options["listing_path"]
        base = f"https://{self.domain}"
        if page == 1:
            return urljoin(base, path)
        # WordPress paginates with /page/N/ before the query string.
        path_part, _, query = path.partition("?")
        paged = f"{path_part.rstrip('/')}/page/{page}/"
        return urljoin(base, paged + (f"?{query}" if query else ""))

    def check(self) -> Iterator[StockResult]:
        seen: set[str] = set()
        for page in range(1, MAX_PAGES + 1):
            try:
                html = self.client.get(self._page_url(page)).text
            except Exception:
                self.errors += 1
                logger.exception("[%s] listing page %d failed", self.name, page)
                return

            tiles = BeautifulSoup(html, "html.parser").select(TILE_SELECTOR)
            if not tiles:
                if page == 1:
                    self.errors += 1
                    logger.warning("[%s] no product tiles on page 1 — has the markup changed?",
                                   self.name)
                return
            logger.info("[%s] page %d: %d tile(s)", self.name, page, len(tiles))

            for tile in tiles:
                try:
                    result = self._to_result(tile)
                except Exception:
                    # Per-product isolation: one odd tile must not drop the rest.
                    self.errors += 1
                    logger.exception("[%s] skipping unparseable tile", self.name)
                    continue
                if result is None or result.product_id in seen:
                    continue
                if not self.wanted(result.product_name):
                    continue
                seen.add(result.product_id)
                yield result

            if len(tiles) < PAGE_SIZE:
                break
        else:
            self.errors += 1
            logger.warning("[%s] still full after %d pages — truncating listing",
                           self.name, MAX_PAGES)

        missing = self.watchlist - seen
        if missing and not self.errors:
            logger.warning("[%s] %d watchlist slug(s) matched no product — typo or renamed? %s",
                           self.name, len(missing), ", ".join(sorted(missing)))

    def _to_result(self, tile) -> StockResult | None:
        classes = set(tile.get("class") or [])
        link = tile.select_one("a[href]")
        if link is None:
            return None
        url = link["href"]
        # The slug is the last path segment of the product URL — the readable,
        # stable identifier, and what a watchlist entry looks like.
        slug = url.rstrip("/").rsplit("/", 1)[-1]
        if not slug:
            return None

        title = ""
        for selector in TITLE_SELECTORS:
            element = tile.select_one(selector)
            if element and element.get_text(strip=True):
                title = " ".join(element.get_text(" ", strip=True).split())
                break

        # The shop's own state, not inferred from wording or a missing button.
        # `outofstock` is checked first: a tile can carry both when a variable
        # product has some variants available, and the cautious reading of an
        # ambiguous tile is "not available".
        in_stock = "instock" in classes and "outofstock" not in classes

        price_value = self._price(tile)
        currency = self.options.get("currency")
        symbol = self.options.get("currency_symbol") or (f"{currency} " if currency else "")
        price_text = f"{symbol}{price_value:.2f}" if price_value is not None else None

        return StockResult(
            product_id=slug,
            product_name=title or slug,
            url=url,
            in_stock=in_stock,
            price_text=price_text,
            price_value=price_value,
            currency=currency,
            seller=None,  # single-vendor store; the site IS the seller
            alertable=self.is_watched(slug),
        )

    @staticmethod
    def _price(tile) -> float | None:
        """Lowest amount in the tile's price block.

        WooCommerce renders a sale as <del>old</del><ins>new</ins> and a
        variable product as a range, so taking the minimum gets the sale price
        in the first case and the cheapest variant in the second — the figure
        you would actually pay either way.
        """
        block = tile.select_one(".price")
        if block is None:
            return None
        amounts = block.select(".woocommerce-Price-amount") or [block]
        values = [v for v in (parse_price(a.get_text(" ", strip=True)) for a in amounts) if v]
        return min(values) if values else None
