"""rarewaves.com — a Shopify store with no usable collection, via Klevu.

It IS Shopify, but `sites/shopify.py` cannot serve it: that module polls
`/collections/<handle>/products.json`, and this store has 1348 collections
and not one of them is Beyblade. `/collections/beyblade` exists and answers
200 with ZERO products, which is worse than a 404 — it looks like a working,
empty collection. Its 100k-product `/collections/all` is not pollable.

So the listing comes from the site's own search API (the third transport in
CLAUDE.md, same reasoning as Ginza): its search is Klevu, and one POST returns
every Beyblade product with stock state. Two facts make that safe rather than
convenient:

* Klevu is an INDEX, and a stale index reporting stock would be the worst
  failure this project can have. Cross-checked against Shopify's own
  authoritative `available` on 2026-09-09: Klevu `inStock: no` for Scale Shark
  matched Shopify `available: False`. Re-check this if stock ever looks wrong;
  it is the assumption the module rests on.
* Klevu returns GBP and ONLY GBP (`storeBaseCurrency`, no per-currency
  fields), while the same product is 134.00 SEK through Shopify with
  `?country=SE`. So the price is read as GBP — never assumed — and the SEK
  figure is fetched per product ONLY where it will actually be used.

THE USEFUL PART: every handle and SKU here is the product's EAN
(`5010996385222-beyblade-x-scale-shark-4-50uf-attack-toys`, sku
`5010996385222`, barcode the same). This store is therefore a barcode->product
dictionary for the whole product line, and the only tracked store where a
watchlist entry can be DERIVED from a barcode rather than looked up. See the
naming problem in CLAUDE.md.

NOTE: rarewaves is UK-based. It quotes SEK and ships here, but it is outside
the EU, so its shipping is not comparable to a German or Dutch store's.
"""

from __future__ import annotations

import logging
from typing import Any, Iterator

from sites.base import SiteChecker, StockResult

logger = logging.getLogger(__name__)

BASE = "https://www.rarewaves.com"
# Its own Klevu cloud, read off the search page (`*.ksearchnet.com`). A
# different store has a different host AND key, so neither is a shared default.
KLEVU_URL = "https://rwcsv2.ksearchnet.com/cs/v2/search"
KLEVU_KEY = "klevu-163067900222714176"
KLEVU_PAGE = 100  # its documented maximum per query
MAX_PAGES = 6

FIELDS = ["id", "name", "url", "inStock", "sku", "salePrice", "price",
          "currency", "storeBaseCurrency"]


class RarewavesChecker(SiteChecker):
    """Options: search, currency, currency_symbol, price_country, watchlist,
    title_include, title_exclude."""

    @property
    def search(self) -> str:
        return self.options.get("search") or "beyblade"

    @property
    def watchlist(self) -> set[str]:
        """Entries are EANs — which here are also the sku and the handle prefix.

        Every other store needs its own opaque identifier looked up by hand.
        This one does not, because the store publishes the barcode as its key.
        """
        return {str(w).strip() for w in (self.options.get("watchlist") or []) if str(w).strip()}

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

    def is_watched(self, product_id: str) -> bool:
        return not self.watchlist or product_id in self.watchlist

    def _query(self, offset: int) -> dict[str, Any]:
        return {
            "context": {"apiKeys": [KLEVU_KEY]},
            "recordQueries": [{
                "id": "search", "typeOfRequest": "SEARCH",
                "settings": {
                    "query": {"term": self.search},
                    "typeOfRecords": ["KLEVU_PRODUCT"],
                    "limit": KLEVU_PAGE, "offset": offset, "fields": FIELDS,
                },
            }],
        }

    def check(self) -> Iterator[StockResult]:
        seen: set[str] = set()
        total: int | None = None
        for page in range(MAX_PAGES):
            try:
                payload = self.client.post_json(KLEVU_URL, json=self._query(page * KLEVU_PAGE))
                result = payload["queryResults"][0]
                records = result.get("records") or []
            except Exception:
                self.errors += 1
                logger.exception("[%s] Klevu search page %d failed", self.name, page + 1)
                return

            if total is None:
                total = (result.get("meta") or {}).get("totalResultsFound")
                logger.info("[%s] Klevu reports %s result(s) for %r",
                            self.name, total, self.search)
            if not records:
                if page == 0:
                    # An empty first page means the key or host changed, not
                    # that the shop stopped selling these. Same reasoning as
                    # Ginza: a silent empty result must not read as "no stock",
                    # because pruning against it deletes everything.
                    self.errors += 1
                    logger.warning("[%s] Klevu returned no records at all — key or host "
                                   "changed? NOT treating this as an empty catalogue.",
                                   self.name)
                return

            for record in records:
                try:
                    stock_result = self._to_result(record)
                except Exception:
                    self.errors += 1
                    logger.exception("[%s] skipping unparseable record", self.name)
                    continue
                if stock_result is None or stock_result.product_id in seen:
                    continue
                if not self.wanted(stock_result.product_name):
                    continue
                seen.add(stock_result.product_id)
                yield stock_result

            if len(records) < KLEVU_PAGE:
                break
        else:
            self.errors += 1
            logger.warning("[%s] still full after %d pages — truncating", self.name, MAX_PAGES)

        missing = self.watchlist - seen
        if missing and not self.errors:
            logger.warning("[%s] %d watchlist EAN(s) matched no product — typo or delisted? %s",
                           self.name, len(missing), ", ".join(sorted(missing)))

    def _to_result(self, record: dict) -> StockResult | None:
        # sku IS the EAN here, which is why it is the product_id: it is stable,
        # unique, and the same string another retailer would use.
        ean = str(record.get("sku") or "").strip()
        if not ean:
            return None

        title = " ".join((record.get("name") or "").split())
        url = record.get("url") or f"{BASE}/products/{ean}"
        in_stock = str(record.get("inStock") or "").lower() == "yes"

        currency = record.get("currency") or record.get("storeBaseCurrency")
        price_value = _to_float(record.get("salePrice") or record.get("price"))
        alertable = self.is_watched(ean)
        notes: list[str] = []

        # The SEK price costs one extra request, so it is fetched only where it
        # will be read: an item that is in stock AND may notify. Everything
        # else keeps the GBP figure the API gave us, which is enough for price
        # history and cannot mislead, because the currency travels with it.
        if in_stock and alertable and self.options.get("price_country"):
            local = self._local_price(url)
            if local is not None:
                price_value, currency = local
            else:
                notes.append("price shown in store currency; SEK lookup failed")

        symbol = {"SEK": "kr", "GBP": "£", "EUR": "€"}.get(currency or "", "")
        price_text = None
        if price_value is not None:
            price_text = (f"{price_value:.0f}{symbol}" if currency == "SEK"
                          else f"{symbol}{price_value:.2f}")

        return StockResult(
            product_id=ean,
            product_name=title or ean,
            url=url,
            in_stock=in_stock,
            price_text=price_text,
            price_value=price_value,
            currency=currency,
            seller=None,  # single-vendor store
            alertable=alertable,
            notes=notes,
        )

    def _local_price(self, url: str) -> tuple[float, str] | None:
        """Shopify's own price for the configured country, in major units.

        `/products/<handle>.js` reports MINOR units (13400 for 134.00 SEK) and
        does not name the currency, so the country we asked for is the only
        statement of it — which is exactly why `country` is required in config
        rather than inferred. Same load-bearing rule as sites/shopify.py.
        """
        country = self.options["price_country"]
        handle = url.rstrip("/").rsplit("/", 1)[-1].split("?")[0]
        try:
            payload = self.client.get_json(f"{BASE}/products/{handle}.js?country={country}")
            variants = payload.get("variants") or []
            minor = variants[0]["price"] if variants else None
            currency = self.options.get("currency")
            if minor is None or not currency:
                return None
            return float(minor) / 100.0, currency
        except Exception:
            logger.debug("[%s] SEK price lookup failed for %s", self.name, handle, exc_info=True)
            return None


def _to_float(value: object) -> float | None:
    try:
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None
