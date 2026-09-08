"""WooCommerce listing parsing, against markup captured from gameshop.se.

The fixture holds four real tiles — two in stock, two out — lifted verbatim,
because the whole reason this module trusts the HTML is the instock/outofstock
class the shop stamps on each tile, and that is worth pinning against reality.
"""

from pathlib import Path

import pytest

from sites.woocommerce import WooCommerceChecker

FIXTURES = Path(__file__).parent / "fixtures"
OPTIONS = {
    "domain": "gameshop.se",
    "listing_path": "/?s=beyblade&post_type=product",
    "currency": "SEK",
    "currency_symbol": "kr",
}


class FakeClient:
    """Serves each queued page in turn, then empties — mirroring a listing
    running out of products. Records URLs so pagination is assertable."""

    def __init__(self, pages):
        self.pages = list(pages)
        self.requested = []

    def get(self, url):
        self.requested.append(url)
        html = self.pages.pop(0) if self.pages else "<html><body></body></html>"

        class Response:
            text = html
        return Response()


@pytest.fixture
def gameshop_html() -> str:
    return (FIXTURES / "woocommerce_gameshop.html").read_text(encoding="utf-8")


def _checker(client, **overrides):
    return WooCommerceChecker("gameshop", {**OPTIONS, **overrides}, client)


def test_parses_real_tiles(gameshop_html):
    results = {r.product_id: r for r in _checker(FakeClient([gameshop_html])).check()}
    assert len(results) == 4

    in_stock = [r for r in results.values() if r.in_stock]
    out = [r for r in results.values() if not r.in_stock]
    assert len(in_stock) == 2 and len(out) == 2

    # Stock comes from the shop's own class, not inferred from wording.
    priced = next(r for r in in_stock if r.price_value)
    assert priced.price_value > 0
    assert priced.price_text.startswith("kr")
    assert priced.currency == "SEK"
    assert priced.url.startswith("https://gameshop.se/")


def test_out_of_stock_tiles_carry_no_price(gameshop_html):
    """This theme hides the price on unavailable products. Documented rather
    than worked around: there is nothing to read, so None is the honest
    answer, and the price returns when the product does."""
    results = [r for r in _checker(FakeClient([gameshop_html])).check() if not r.in_stock]
    assert results and all(r.price_value is None for r in results)


def test_slug_is_the_product_id(gameshop_html):
    """The slug is the last path segment of the product URL — readable, stable,
    and what a watchlist entry looks like."""
    ids = [r.product_id for r in _checker(FakeClient([gameshop_html])).check()]
    assert all("/" not in i and i for i in ids)
    assert any(i.startswith("beyblade-x-") for i in ids)


def test_outofstock_wins_when_a_tile_carries_both():
    """A variable product can be tagged both ways; the cautious reading of an
    ambiguous tile is 'not available'."""
    html = ('<li class="product instock outofstock"><a href="https://gameshop.se/product/x/">'
            '<h2 class="woocommerce-loop-product__title">Thing</h2></a>'
            '<span class="price"><span class="woocommerce-Price-amount">100 kr</span></span></li>')
    result = next(_checker(FakeClient([html])).check())
    assert result.in_stock is False


def test_sale_price_takes_the_lower_amount():
    """WooCommerce renders a sale as <del>old</del><ins>new</ins>."""
    html = ('<li class="product instock"><a href="https://gameshop.se/product/x/">'
            '<h2 class="woocommerce-loop-product__title">Thing</h2></a>'
            '<span class="price"><del><span class="woocommerce-Price-amount">500 kr</span></del>'
            '<ins><span class="woocommerce-Price-amount">299 kr</span></ins></span></li>')
    assert next(_checker(FakeClient([html])).check()).price_value == 299.0


def test_watchlist_gates_alerts_but_not_tracking(gameshop_html):
    checker = _checker(FakeClient([gameshop_html]), watchlist=["beyblade-x-mirage-clock-9-65b-stamina"])
    results = {r.product_id: r for r in checker.check()}
    assert len(results) == 4                                        # all tracked
    assert results["beyblade-x-mirage-clock-9-65b-stamina"].alertable is True
    assert sum(1 for r in results.values() if r.alertable) == 1     # only that one


def test_title_exclude_drops_the_other_generation(gameshop_html):
    """gameshop mixes Beyblade Burst into the same listing."""
    results = list(_checker(FakeClient([gameshop_html]), title_exclude=["beyblade burst"]).check())
    assert results and not any("burst" in r.product_name.lower() for r in results)


def test_a_short_page_ends_pagination(gameshop_html):
    """Four tiles is fewer than a full page, so asking for page 2 would be a
    wasted request against a live store."""
    client = FakeClient([gameshop_html])
    list(_checker(client).check())
    assert len(client.requested) == 1


def test_page_two_url_uses_wordpress_paging():
    checker = _checker(FakeClient([]))
    assert checker._page_url(1) == "https://gameshop.se/?s=beyblade&post_type=product"
    assert checker._page_url(2) == "https://gameshop.se/page/2/?s=beyblade&post_type=product"


def test_missing_tiles_on_page_one_is_an_error():
    """Markup changing must not read as 'the store is empty'."""
    checker = _checker(FakeClient(["<html><body>no products here</body></html>"]))
    assert list(checker.check()) == []
    assert checker.errors == 1


def test_a_tile_without_a_link_is_skipped():
    html = '<li class="product instock"><h2>No link</h2></li>'
    assert list(_checker(FakeClient([html])).check()) == []


def test_unmatched_watchlist_slug_is_reported(gameshop_html, caplog):
    with caplog.at_level("WARNING"):
        list(_checker(FakeClient([gameshop_html]), watchlist=["typo-slug"]).check())
    assert "typo-slug" in caplog.text
