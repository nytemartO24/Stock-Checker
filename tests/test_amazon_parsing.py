"""Product-page parsing, against markup captured from a real listing.

The fixture is the containers parse_product() actually reads, lifted
verbatim from a live amazon.de page for B0DN6YLGRX (the third-party
listing that motivated scalp detection). Never hits the live site.
"""

from pathlib import Path

import pytest

from sites.amazon import parse_product
from sites.amazon.markets import MARKETS

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def third_party_html() -> str:
    return (FIXTURES / "amazon_de_thirdparty.html").read_text(encoding="utf-8")


def test_parses_a_real_third_party_listing(third_party_html):
    parsed = parse_product(third_party_html, MARKETS["de"], delivery_country="Sweden")
    assert parsed.title.startswith("Beyblade X Sterling Wolf")
    assert parsed.in_stock is True
    assert parsed.seller == "London Lane Company"
    assert parsed.is_amazon_seller is False
    assert parsed.price_value == 766.25
    # Pinned to Sweden, amazon.de quotes SEK despite being a EUR market.
    assert parsed.currency == "SEK"
    assert parsed.untrusted is None


def _page(body: str, lang: str = "en-gb") -> str:
    return f'<html lang="{lang}"><body>{body}</body></html>'


def test_unavailable_listing_is_not_in_stock():
    html = _page(
        '<span id="productTitle">Thing</span>'
        '<div id="availability">Currently unavailable. We don\'t know when or if '
        'this item will be back in stock.</div>'
    )
    assert parse_product(html, MARKETS["de"], delivery_country="Sweden").in_stock is False


def test_buy_button_without_unavailable_text_is_in_stock():
    html = _page('<span id="productTitle">Thing</span><input id="add-to-cart-button" value="Add to Basket">')
    assert parse_product(html, MARKETS["de"], delivery_country="Sweden").in_stock is True


def test_preorder_counts_as_in_stock():
    """'Available for pre-order' is exactly what we want to be told about."""
    html = _page(
        '<span id="productTitle">Thing</span>'
        '<div id="availability">Available for pre-order.</div>'
        '<input id="add-to-cart-button" value="Pre-order now">'
    )
    assert parse_product(html, MARKETS["de"], delivery_country="Sweden").in_stock is True


def test_not_deliverable_is_not_in_stock():
    """A listing that won't ship to you is not something you can buy."""
    html = _page(
        '<span id="productTitle">Thing</span>'
        '<div id="availability">This item cannot be dispatched to your selected '
        'delivery location.</div>'
        '<input id="add-to-cart-button" value="Add to Basket">'
    )
    assert parse_product(html, MARKETS["de"], delivery_country="Sweden").in_stock is False


def test_polish_native_signal_matches():
    """Poland ignores the /-/en/ override and serves pl-pl, so its native
    phrasing is load-bearing rather than a fallback."""
    html = _page('<span id="productTitle">Rzecz</span><div id="availability">Obecnie niedostępny</div>', "pl-pl")
    assert parse_product(html, MARKETS["pl"], delivery_country="Sweden").in_stock is False


def test_wrong_destination_page_is_untrusted():
    """A page quoting another country's availability must not be recorded —
    it would become the baseline a future alert fires against."""
    html = _page(
        '<span id="productTitle">Thing</span>'
        '<p>International Shopping Transition Alert We are showing you items that '
        'dispatch to United States.</p>'
        '<input id="add-to-cart-button" value="Add to Basket">'
    )
    parsed = parse_product(html, MARKETS["de"], delivery_country="Sweden")
    assert parsed.untrusted is not None
    assert parsed.in_stock is False


def test_requested_destination_banner_reads_normally():
    """Cross-border pages are the GOAL, not a failure: with the destination
    pinned to Sweden, amazon.de saying it dispatches to Sweden is exactly
    the question being asked."""
    html = _page(
        '<span id="productTitle">Thing</span>'
        '<p>International Shopping Transition Alert We are showing you items that '
        'dispatch to Sweden.</p>'
        '<input id="add-to-cart-button" value="Add to Basket">'
    )
    parsed = parse_product(html, MARKETS["de"], delivery_country="Sweden")
    assert parsed.untrusted is None
    assert parsed.in_stock is True


def test_availability_signals_are_scoped_not_whole_page():
    """A 'Release date' style row elsewhere on the page must not decide
    availability — whole-page matching is a false-positive machine."""
    html = _page(
        '<span id="productTitle">Thing</span>'
        '<input id="add-to-cart-button" value="Add to Basket">'
        '<table id="productDetails"><tr><td>Currently unavailable</td></tr></table>'
    )
    assert parse_product(html, MARKETS["de"], delivery_country="Sweden").in_stock is True
