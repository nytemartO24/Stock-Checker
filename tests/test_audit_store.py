"""URL handling for the store-audit tool.

Only the pure helpers are tested — the rest of the script talks to live
stores by design, which is the one thing tests here must not do.
"""

import pytest

from scripts.audit_store import split_base, suggested_name


@pytest.mark.parametrize("url,expected", [
    # The URLs actually to hand: a collection URL with filter query, a
    # search URL, and a bare host.
    ("https://www.popsplanet.it/en/collections/beyblade-x-booster?sort_by=created-descending",
     ("https://www.popsplanet.it", "/en")),
    ("https://toysnowman.com/a/search/beyblade?filter_in_stock_and_preorders=all&country=se",
     ("https://toysnowman.com", "")),
    ("https://toysnowman.com", ("https://toysnowman.com", "")),
    ("toysnowman.com", ("https://toysnowman.com", "")),
])
def test_split_base(url, expected):
    assert split_base(url) == expected


def test_two_letter_locale_is_detected_but_longer_segments_are_not():
    """A /en prefix is a locale; /collections is a Shopify route."""
    assert split_base("https://x.test/en/collections/y")[1] == "/en"
    assert split_base("https://x.test/collections/y")[1] == ""


def test_shopify_app_route_is_not_mistaken_for_a_locale():
    """toysnowman's search lives under /a/ — a single char, not a locale."""
    assert split_base("https://toysnowman.com/a/search/beyblade")[1] == ""


@pytest.mark.parametrize("origin,expected", [
    ("https://www.popsplanet.it", "popsplanet"),
    ("https://toysnowman.com", "toysnowman"),
    ("https://shop.example.co.uk", "shop-example"),
])
def test_suggested_name(origin, expected):
    assert suggested_name(origin) == expected
