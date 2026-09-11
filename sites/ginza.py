"""ginza.se, via the JSON endpoint its own search page calls.

The search page ships 413KB of HTML with ZERO product links — results are
rendered client-side — so the obvious HTML route gets you nothing. Watching
what the page actually requests found `/api/Apptus/Search`, which returns the
whole result set as JSON. One request per run for the entire catalogue, no
browser, which is the cheapest transport of the three sites that need one.

TWO NON-OBVIOUS REQUIREMENTS, both invisible until measured:

* The endpoint needs a `Referer` matching the search term AND the `s=`
  parameter to agree. With no Referer it returns an EMPTY BODY rather than an
  error, so the requirement announces itself as "the API is broken". With a
  mismatched pair it returns zero results, which looks like "nothing in
  stock". Both failure modes are silent, which is why `check()` treats an
  empty result set as an error rather than as an empty catalogue.
* Availability is not a field. `ProductStockStatus` is empty for exactly the
  products you cannot buy. The real signal is the buy button: measured across
  the catalogue, `btn-add-to-cart` means orderable ("Beställningsvara",
  delivery from 5-7 working days), `btn-watchlist` means not ("Osäker
  leveranstid" — uncertain delivery), and `btn-disabled` means discontinued
  ("Utgått ur sortimentet").

NAMING: Ginza uses Takara Tomy's names, not Hasbro's. Scale Shark 4-50UF is
listed as "BEYBLADE Bbx Kobuk Valley" — a completely different name, not a
word-order variation. So a watchlist here CANNOT reuse another store's terms,
and searching for "beyblade x" misses everything: the titles say "BBX".
Entries are Ginza's own numeric ProductIdentifier, which is stable and
appears in the product URL.
"""

from __future__ import annotations

import logging
import re
from typing import Iterator
from urllib.parse import quote, urljoin

from core.parsing import parse_price
from sites.base import SiteChecker, StockResult

logger = logging.getLogger(__name__)

BASE = "https://www.ginza.se"
API = f"{BASE}/api/Apptus/Search"
PAGE_SIZE = 60
MAX_PAGES = 10

# The shop's own state, read off the button it renders.
BUYABLE_MARKER = "btn-add-to-cart"

_TAGS = re.compile(r"<[^>]+>")


class GinzaChecker(SiteChecker):
    """Options: search (required), currency, currency_symbol, watchlist,
    title_include, title_exclude."""

    @property
    def search(self) -> str:
        return self.options["search"]

    @property
    def watchlist(self) -> set[str]:
        """Ginza's numeric product ids. Its names match no other store's, so
        nothing here can be shared or guessed — look one up in the product URL:
        /product/<slug>/<id>/"""
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

    def _request(self, page: int) -> tuple[str, dict[str, str]]:
        term = quote(self.search)
        url = (f"{API}?pageSize={PAGE_SIZE}&pageIndex={page}&sort=SO_ALL&cache=false"
               f"&isErotikOn=false&priceRange=%2C&letter=&s={term}&isError=false")
        # Must match `s=`; see the module docstring.
        return url, {"Referer": f"{BASE}/search/1?s={term}",
                     "Accept": "application/json",
                     "X-Requested-With": "XMLHttpRequest"}

    def check(self) -> Iterator[StockResult]:
        seen: set[str] = set()
        total = None
        fetched = 0
        for page in range(1, MAX_PAGES + 1):
            url, headers = self._request(page)
            try:
                payload = self.client.get_json(url, headers=headers)
            except Exception:
                self.errors += 1
                logger.exception("[%s] search page %d failed", self.name, page)
                return

            products = payload.get("Products") or []
            if total is None:
                total = payload.get("totalCount")
                logger.info("[%s] %s reports %s result(s) for %r",
                            self.name, "API", total, self.search)
            if not products:
                if page == 1:
                    # An empty body or a Referer/term mismatch both land here,
                    # and both look exactly like "the shop sells none of this".
                    self.errors += 1
                    logger.warning("[%s] no products returned — Referer/term mismatch, or "
                                   "the endpoint changed. NOT treating this as an empty "
                                   "catalogue.", self.name)
                # A later empty page falls through to the completeness check
                # below rather than returning silently.
                break

            fetched += len(products)

            for product in products:
                try:
                    result = self._to_result(product)
                except Exception:
                    self.errors += 1
                    logger.exception("[%s] skipping unparseable product entry", self.name)
                    continue
                if result is None or result.product_id in seen:
                    continue
                if not self.wanted(result.product_name):
                    continue
                seen.add(result.product_id)
                yield result

            if len(products) < PAGE_SIZE:
                break
        else:
            self.errors += 1
            logger.warning("[%s] still full after %d pages — truncating", self.name, MAX_PAGES)

        # Same completeness guard as rarewaves, and for the same reason: the API
        # states a total, so returning fewer records than promised is a PARTIAL
        # view, and main.py prunes state against a run that reports no errors.
        # Latent here today — 22 products fit in one 60-item page — but the
        # failure is identical and silent, and rarewaves proved what it costs:
        # a transient short page pruned 12 entries, which the next run then
        # re-alerted as brand new.
        if total is not None and fetched < total:
            self.errors += 1
            logger.warning(
                "[%s] INCOMPLETE: API reported %s result(s) but returned %d — "
                "not pruning against a partial view", self.name, total, fetched)

        missing = self.watchlist - seen
        if missing and not self.errors:
            logger.warning("[%s] %d watchlist id(s) matched no product — typo or delisted? %s",
                           self.name, len(missing), ", ".join(sorted(missing)))

    def _to_result(self, product: dict) -> StockResult | None:
        product_id = str(product.get("ProductIdentifier") or "").strip()
        if not product_id:
            return None

        title = " ".join((product.get("ProductTitle") or "").split())
        # ProductPrice arrives as a rendered fragment: "<div ...><strong>169 kr</strong></div>"
        price_value = parse_price(_TAGS.sub(" ", product.get("ProductPrice") or ""))
        currency = self.options.get("currency")
        symbol = self.options.get("currency_symbol") or (f"{currency} " if currency else "")
        price_text = f"{symbol}{price_value:.0f}" if price_value is not None else None

        in_stock = BUYABLE_MARKER in (product.get("BuyButtonHtml") or "")

        # A lead time, not a date ("Leveranstid: från 5 vardagar"). Carried for
        # the alert because "orderable, ships in 5 days" is materially
        # different from "orderable, uncertain" — but never parsed as a date,
        # so none of Amazon's date comparison touches it.
        delivery = " ".join((product.get("ProductDelivery") or "").split()) or None

        return StockResult(
            product_id=product_id,
            product_name=title or product_id,
            url=urljoin(BASE, product.get("ProductUrl") or ""),
            in_stock=in_stock,
            price_text=price_text,
            price_value=price_value,
            currency=currency,
            seller=None,  # single-vendor store; the site IS the seller
            delivery_date=delivery,
            alertable=self.is_watched(product_id),
        )
