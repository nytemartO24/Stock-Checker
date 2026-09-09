"""lereservoir.lu — a Luxembourg shop with unusually honest markup.

Found by harvesting OpenStreetMap for mapped toy shops, and it is the store
that made that harvest worth doing: it had Delta Unicorn PO 3-60GU in stock,
listed as "Fig Beyblade X Guadalupe Mountains".

THE LISTING IS A CATEGORY, NOT THE SEARCH, and that is a measured decision:
its own search for "beyblade" returns TWO Beyblade products while the Hasbro
category page contains FOUR — the search silently misses "Great Smokey
Mountains" and the Courage Dran starter pack. Both sources are therefore
polled and unioned, because neither alone was complete. Two requests per run.

Availability is a CSS class, not a field. Each tile is
`<div id="ficheListe<EAN>" class="ficheArticle ... [disabled]">`, and
`disabled` is the shop saying you cannot buy it — confirmed against a product
the user bought, which flipped to `disabled` immediately afterwards.

WHY THIS STORE MATTERS BEYOND ITS SIZE: the tile's own id IS the product's
EAN, and it repeats in the product URL (`-fiche-5010996431134.html`). So, like
rarewaves, its watchlist is barcodes rather than an opaque per-store handle —
and it names products by Hasbro's US-national-park codenames, which is
independent confirmation that those come from a distributor feed rather than
being a Ginza quirk. See the naming problem in CLAUDE.md.

The catalogue is small (a dozen items in the Hasbro range, most of them Star
Wars), so `title_include` is doing real work here rather than tidying.
"""

from __future__ import annotations

import html as html_module
import logging
import re
from typing import Iterator

from core.parsing import parse_price
from sites.base import SiteChecker, StockResult

logger = logging.getLogger(__name__)

BASE = "https://www.lereservoir.lu"

# One product tile. The id carries the EAN; the rest of the tag carries the
# classes, `disabled` among them when the item cannot be bought.
TILE = re.compile(r'<div id="ficheListe(\d+)"([^>]*)>')
NAME = re.compile(r"itemprop='name'>([^<]+)<")
PRICE = re.compile(r"class='prix'>([^<]+)<")
FICHE = re.compile(r'href="(https://www\.lereservoir\.lu/catalogue[^"]*-fiche-(\d+)\.html)"')

# The shop's own statement that the item is unbuyable.
SOLD_OUT_CLASS = "disabled"


class LeReservoirChecker(SiteChecker):
    """Options: listing_paths (required), currency, currency_symbol, watchlist,
    title_include, title_exclude."""

    @property
    def listing_paths(self) -> list[str]:
        return [str(p) for p in (self.options.get("listing_paths") or []) if str(p).strip()]

    @property
    def watchlist(self) -> set[str]:
        """EANs. This store publishes the barcode as its own key, so unlike a
        Shopify handle these entries mean the same thing at another retailer."""
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

    def check(self) -> Iterator[StockResult]:
        if not self.listing_paths:
            self.errors += 1
            logger.error("[%s] no listing_paths configured", self.name)
            return

        seen: set[str] = set()
        for path in self.listing_paths:
            url = path if path.startswith("http") else BASE + path
            try:
                body = self.client.get(url).text
            except Exception:
                # One listing failing must not discard the other's results, but
                # it does make this run an incomplete view of the shop.
                self.errors += 1
                logger.exception("[%s] listing %s failed", self.name, url)
                continue

            tiles = list(self._tiles(body))
            if not tiles:
                # A listing that parses to nothing means the markup changed, not
                # that the shop emptied. Treating it as empty would prune every
                # product and then re-alert on all of them.
                self.errors += 1
                logger.warning("[%s] %s parsed to ZERO tiles — markup changed? NOT "
                               "treating this as an empty catalogue.", self.name, url)
                continue
            logger.info("[%s] %s: %d tile(s)", self.name, url, len(tiles))

            for ean, attributes, block in tiles:
                if ean in seen:
                    continue
                try:
                    result = self._to_result(ean, attributes, block)
                except Exception:
                    self.errors += 1
                    logger.exception("[%s] skipping unparseable tile %s", self.name, ean)
                    continue
                if result is None or not self.wanted(result.product_name):
                    continue
                seen.add(ean)
                yield result

        missing = self.watchlist - seen
        if missing and not self.errors:
            logger.warning("[%s] %d watchlist EAN(s) matched no product — delisted? %s",
                           self.name, len(missing), ", ".join(sorted(missing)))

    def _tiles(self, body: str) -> Iterator[tuple[str, str, str]]:
        """(ean, tag attributes, block html) per product tile.

        The block runs to the start of the next tile, which is what keeps one
        product's price from being read off its neighbour — the same scoping
        rule the Amazon module follows for delivery dates.
        """
        matches = list(TILE.finditer(body))
        for index, match in enumerate(matches):
            end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
            yield match.group(1), match.group(2), body[match.end():end]

    def _to_result(self, ean: str, attributes: str, block: str) -> StockResult | None:
        name = NAME.search(block)
        title = html_module.unescape(name.group(1)).strip() if name else ""

        price_match = PRICE.search(block)
        raw_price = html_module.unescape(price_match.group(1)) if price_match else ""
        price_value = parse_price(raw_price)
        currency = self.options.get("currency")
        symbol = self.options.get("currency_symbol") or ""
        price_text = f"{price_value:.2f}{symbol}" if price_value is not None else None

        # `disabled` on the tile is the shop's own "not buyable". Read off the
        # tag's class list only — searching the whole block would match any
        # disabled control inside it.
        in_stock = SOLD_OUT_CLASS not in attributes.lower()

        link = FICHE.search(block)
        url = link.group(1) if link else f"{BASE}/index-s-{ean}.html"

        return StockResult(
            product_id=ean,
            product_name=title or ean,
            url=url,
            in_stock=in_stock,
            price_text=price_text,
            price_value=price_value,
            currency=currency,
            seller=None,  # single-vendor shop
            alertable=self.is_watched(ean),
        )
