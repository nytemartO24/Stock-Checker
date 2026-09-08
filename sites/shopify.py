"""Shopify storefronts, via the public products.json endpoint.

Covers every Shopify store, parameterized by domain + collections, because
two of them (popsplanet.it, toysnowman.com) need exactly the same code.

No browser and no HTML parsing: `/collections/<handle>/products.json`
returns `available`, `price` and `compare_at_price` per variant, and
`available` is authoritative — it already accounts for pre-orders, which is
precisely this project's definition of in stock. One request per collection
covers every product in it.

Amazon's multi-vendor scalper problem does not exist here: the store is the
only seller, so its price is the price. Don't port that logic across.

Prices are market-dependent on Shopify Markets stores, so every request
pins `?country=` explicitly — see _collection_url().
"""

from __future__ import annotations

import logging
from typing import Iterator

from sites.base import SiteChecker, StockResult

logger = logging.getLogger(__name__)

PAGE_SIZE = 250  # Shopify's maximum for this endpoint.
MAX_PAGES = 10   # Guard against paginating forever on a huge store.


class ShopifyChecker(SiteChecker):
    """Options: domain (required), collections (required), base_path,
    country, currency, currency_symbol, title_include, title_exclude."""

    @property
    def title_include(self) -> list[str]:
        """Keep only products whose title contains one of these (case
        insensitive). Empty means keep everything."""
        return [t.lower() for t in (self.options.get("title_include") or [])]

    @property
    def title_exclude(self) -> list[str]:
        """Drop products whose title contains any of these."""
        return [t.lower() for t in (self.options.get("title_exclude") or [])]

    def wanted(self, title: str) -> bool:
        """A store groups by its own logic, not ours: toysnowman files two
        older-generation products under `beyblade` alongside Beyblade X, and
        it has only one collection so they cannot be excluded by collection
        choice. Filtered products never enter the result stream, so they are
        also pruned from state on the next clean run."""
        low = (title or "").lower()
        if any(bad in low for bad in self.title_exclude):
            return False
        return not self.title_include or any(good in low for good in self.title_include)

    @property
    def domain(self) -> str:
        return self.options["domain"]

    @property
    def base_path(self) -> str:
        """Locale prefix, e.g. "/en" for popsplanet.it. Empty for most stores."""
        return self.options.get("base_path", "").rstrip("/")

    def _collection_url(self, handle: str, page: int) -> str:
        url = (
            f"https://{self.domain}{self.base_path}/collections/{handle}"
            f"/products.json?limit={PAGE_SIZE}&page={page}"
        )
        # ALWAYS pin the market. A Shopify Markets store prices in the
        # visitor's country, so the SAME url returns a different currency
        # depending on whether an Accept-Language header happened to be
        # sent — toysnowman answered 25.99 (CAD) bare and 185.00 (SEK) with
        # one, and nothing in the payload says which you got. `country`
        # overrides that guessing and makes `currency` in config a fact
        # rather than an assumption.
        country = self.options.get("country")
        if country:
            url += f"&country={country}"
        return url

    def check(self) -> Iterator[StockResult]:
        seen: set[str] = set()
        for handle in self.options.get("collections", []):
            try:
                yield from self._check_collection(handle, seen)
            except Exception:
                # One bad collection must not lose the others — but the run
                # is now an incomplete view, so say so.
                self.errors += 1
                logger.exception("[%s] collection %r failed", self.name, handle)

    def _check_collection(self, handle: str, seen: set[str]) -> Iterator[StockResult]:
        for page in range(1, MAX_PAGES + 1):
            payload = self.client.get_json(self._collection_url(handle, page))
            products = payload.get("products") or []
            if not products:
                return
            logger.info("[%s] %s page %d: %d product(s)", self.name, handle, page, len(products))
            for product in products:
                # Per-product isolation: one malformed entry must not drop
                # every product after it in this collection. The dedupe
                # check is inside the try because a product that isn't a
                # dict at all fails there first.
                try:
                    # Collections overlap (a booster can sit in two of
                    # them); the first sighting wins so state isn't written
                    # twice.
                    if product.get("handle") in seen:
                        continue
                    result = self._to_result(product)
                except Exception:
                    self.errors += 1
                    logger.exception("[%s] skipping malformed product entry: %r", self.name, product)
                    continue
                if result is not None and self.wanted(result.product_name):
                    seen.add(result.product_id)
                    yield result
            if len(products) < PAGE_SIZE:
                return

        # Fell out of the page loop still seeing full pages: the collection
        # is bigger than we're willing to read, so this view is incomplete.
        self.errors += 1
        logger.warning(
            "[%s] collection %r still full after %d pages — truncating; "
            "raise MAX_PAGES if this store is genuinely this large",
            self.name, handle, MAX_PAGES,
        )

    def _to_result(self, product: dict) -> StockResult | None:
        handle = product.get("handle")
        if not handle:
            logger.warning("[%s] product with no handle, skipping: %r", self.name, product.get("id"))
            return None

        variants = product.get("variants") or []
        available = [v for v in variants if v.get("available")]
        in_stock = bool(available)

        # Price the cheapest variant you could actually buy; fall back to the
        # cheapest listed one so an out-of-stock product still reports a
        # sensible figure.
        pool = available or variants
        price_value = min((p for p in (_to_float(v.get("price")) for v in pool) if p is not None), default=None)

        currency = self.options.get("currency")
        symbol = self.options.get("currency_symbol") or (f"{currency} " if currency else "")
        price_text = f"{symbol}{price_value:.2f}" if price_value is not None else None

        return StockResult(
            product_id=handle,
            product_name=product.get("title") or handle,
            url=f"https://{self.domain}{self.base_path}/products/{handle}",
            in_stock=in_stock,
            price_text=price_text,
            price_value=price_value,
            currency=currency,
            seller=None,  # single-vendor store; the site IS the seller
        )


def _to_float(value: object) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
