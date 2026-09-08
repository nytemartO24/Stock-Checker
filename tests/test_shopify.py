"""Shopify parsing, against a saved capture — never the live site."""

from sites.shopify import ShopifyChecker

OPTIONS = {
    "domain": "www.popsplanet.it",
    "base_path": "/en",
    "country": "SE",
    "currency": "EUR",
    "currency_symbol": "€",
    "collections": ["beyblade-x-booster"],
}


def _checker(client):
    return ShopifyChecker("popsplanet", OPTIONS, client)


def test_parses_availability_and_price(popsplanet_payload, fake_client):
    results = list(_checker(fake_client([popsplanet_payload])).check())

    assert len(results) == 3
    by_name = {r.product_name: r for r in results}
    tide = by_name["Beyblade X - Booster: Tide Whale 5-80E"]
    assert tide.in_stock is True
    assert tide.price_value == 9.90
    assert tide.price_text == "€9.90"
    assert tide.currency == "EUR"
    assert tide.product_id == "beyblade-x-booster-tide-whale-580e"
    assert tide.url == "https://www.popsplanet.it/en/products/beyblade-x-booster-tide-whale-580e"

    assert by_name["Beyblade X - Booster: Curse Mummy 7-55W"].in_stock is False


def test_out_of_stock_still_reports_a_price(popsplanet_payload, fake_client):
    """Falling back to the cheapest listed variant means a sold-out product
    still carries a usable figure for reference-price purposes."""
    results = {r.product_name: r for r in _checker(fake_client([popsplanet_payload])).check()}
    sold_out = results["Beyblade X - Booster: Cobalt Drake 4-60F"]
    assert sold_out.in_stock is False
    assert sold_out.price_value == 9.90


def test_single_short_page_makes_one_request(popsplanet_payload, fake_client):
    """A page shorter than the limit means the collection is exhausted —
    asking for page 2 would be a wasted request against a live store."""
    client = fake_client([popsplanet_payload])
    list(_checker(client).check())
    assert len(client.requested) == 1
    assert "limit=250&page=1" in client.requested[0]


def test_country_is_always_pinned(popsplanet_payload, fake_client):
    """Without ?country= a Shopify Markets store prices in the visitor's
    country, so the same URL can return a different currency run to run
    while `currency` in config keeps claiming otherwise."""
    client = fake_client([popsplanet_payload])
    list(_checker(client).check())
    assert "&country=SE" in client.requested[0]


def test_missing_country_omits_the_parameter(popsplanet_payload, fake_client):
    """Explicitly no country -> no parameter, rather than 'country=None'."""
    options = {k: v for k, v in OPTIONS.items() if k != "country"}
    client = fake_client([popsplanet_payload])
    list(ShopifyChecker("popsplanet", options, client).check())
    assert "country" not in client.requested[0]


def test_collections_overlap_yields_each_product_once(popsplanet_payload, fake_client):
    """A booster can sit in two collections; state must not be written twice."""
    options = dict(OPTIONS, collections=["beyblade-x-booster", "beyblade-x-starter-pack"])
    client = fake_client([popsplanet_payload, popsplanet_payload])
    results = list(ShopifyChecker("popsplanet", options, client).check())
    assert len(results) == 3
    assert len({r.product_id for r in results}) == 3


def test_one_bad_collection_does_not_lose_the_others(popsplanet_payload):
    class Flaky:
        def __init__(self):
            self.calls = 0

        def get_json(self, url):
            self.calls += 1
            if "broken" in url:
                raise RuntimeError("boom")
            return popsplanet_payload

    options = dict(OPTIONS, collections=["broken", "beyblade-x-booster"])
    results = list(ShopifyChecker("popsplanet", options, Flaky()).check())
    assert len(results) == 3


def test_product_without_handle_is_skipped(fake_client):
    payload = {"products": [{"id": 1, "title": "No handle", "variants": [{"price": "1.00", "available": True}]}]}
    assert list(_checker(fake_client([payload])).check()) == []


def test_title_exclude_drops_only_the_old_generation(popsplanet_payload, fake_client):
    """toysnowman mixes generations in one collection, so title is the only
    lever. "BBX" is Takara Tomy's branding for Beyblade X and MUST survive —
    an earlier `beyblade x` include-filter silently dropped it. Only
    Beyblade Burst is a different line."""
    payload = {"products": [
        {"handle": "x", "title": "Beyblade X Tide Whale 5-80E Booster Pack",
         "variants": [{"price": "93.00", "available": True}]},
        {"handle": "bbx", "title": "Beyblade BBX Beat Tyranno Knife Shinobi Battle Top",
         "variants": [{"price": "164.00", "available": True}]},
        {"handle": "burst", "title": "Beyblade Burst QuadStrike Thunder Edge Battle Set",
         "variants": [{"price": "462.00", "available": True}]},
    ]}
    options = dict(OPTIONS, title_exclude=["beyblade burst"])
    results = list(ShopifyChecker("toysnowman", options, fake_client([payload])).check())
    assert [r.product_id for r in results] == ["x", "bbx"]


def test_title_exclude_wins_over_include(fake_client):
    payload = {"products": [
        {"handle": "a", "title": "Beyblade X Official Winder Launcher Accessory",
         "variants": [{"price": "93.00", "available": True}]},
        {"handle": "b", "title": "Beyblade X Tide Whale 5-80E Booster Pack",
         "variants": [{"price": "93.00", "available": True}]},
    ]}
    options = dict(OPTIONS, title_include=["beyblade x"], title_exclude=["launcher"])
    results = list(ShopifyChecker("t", options, fake_client([payload])).check())
    assert [r.product_id for r in results] == ["b"]


def test_no_title_filters_keeps_everything(popsplanet_payload, fake_client):
    results = list(_checker(fake_client([popsplanet_payload])).check())
    assert len(results) == 3
