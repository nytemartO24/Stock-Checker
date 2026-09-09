"""ginza.se search-API parsing, against a real captured response.

The fixture deliberately covers one product of each availability shape,
because availability here is not a field — it is inferred from the button the
shop renders, and getting that inference wrong is silent.
"""

import json
from pathlib import Path

import pytest

from sites.ginza import GinzaChecker

FIXTURES = Path(__file__).parent / "fixtures"
OPTIONS = {"search": "beyblade", "currency": "SEK", "currency_symbol": "kr"}


class FakeClient:
    def __init__(self, pages):
        self.pages = list(pages)
        self.calls = []

    def get_json(self, url, headers=None):
        self.calls.append((url, headers or {}))
        return self.pages.pop(0) if self.pages else {"Products": [], "totalCount": 0}


@pytest.fixture
def ginza_payload() -> dict:
    return json.loads((FIXTURES / "ginza_search.json").read_text(encoding="utf-8"))


def _checker(client, **overrides):
    return GinzaChecker("ginza", {**OPTIONS, **overrides}, client)


def test_parses_the_real_response(ginza_payload):
    results = list(_checker(FakeClient([ginza_payload])).check())
    assert len(results) == 4
    for r in results:
        assert r.product_id.isdigit(), "ids are Ginza's own numeric identifiers"
        assert r.url.startswith("https://www.ginza.se/product/")
        assert r.currency == "SEK"
        assert r.price_value and r.price_value > 0


def test_availability_comes_from_the_buy_button(ginza_payload):
    """ProductStockStatus is EMPTY for exactly the products you cannot buy, so
    the button is the only usable signal: btn-add-to-cart means orderable,
    btn-watchlist means not."""
    by_id = {r.product_id: r for r in _checker(FakeClient([ginza_payload])).check()}
    for product in ginza_payload["Products"]:
        expected = "btn-add-to-cart" in (product["BuyButtonHtml"] or "")
        assert by_id[str(product["ProductIdentifier"])].in_stock is expected


def test_price_is_extracted_from_rendered_html(ginza_payload):
    """ProductPrice arrives as a markup fragment, not a number."""
    first = ginza_payload["Products"][0]
    assert "<" in first["ProductPrice"], "fixture should still contain markup"
    result = next(iter(_checker(FakeClient([ginza_payload])).check()))
    assert result.price_text.startswith("kr")


def test_the_referer_must_match_the_search_term():
    """The endpoint returns an EMPTY BODY with no Referer and zero results with
    a mismatched one — both silent. Pinning this stops a future edit dropping
    the header and reading the result as 'nothing in stock'."""
    client = FakeClient([{"Products": [], "totalCount": 0}])
    list(_checker(client, search="beyblade").check())
    url, headers = client.calls[0]
    assert "s=beyblade" in url
    assert headers["Referer"] == "https://www.ginza.se/search/1?s=beyblade"


def test_an_empty_result_set_is_an_error_not_an_empty_catalogue():
    """Both silent failure modes land here, and 'the shop sells none of this'
    would prune all state and re-alert on everything when it recovered."""
    checker = _checker(FakeClient([{"Products": [], "totalCount": 0}]))
    assert list(checker.check()) == []
    assert checker.errors == 1


def test_watchlist_gates_alerts_but_not_tracking(ginza_payload):
    watched = str(ginza_payload["Products"][0]["ProductIdentifier"])
    results = {r.product_id: r for r in _checker(FakeClient([ginza_payload]), watchlist=[watched]).check()}
    assert len(results) == 4                                  # all tracked
    assert results[watched].alertable is True
    assert sum(1 for r in results.values() if r.alertable) == 1


def test_title_exclude_drops_the_back_catalogue(ginza_payload):
    title = ginza_payload["Products"][0]["ProductTitle"]
    term = title.split()[-1].lower()
    results = list(_checker(FakeClient([ginza_payload]), title_exclude=[term]).check())
    assert all(term not in r.product_name.lower() for r in results)


def test_delivery_lead_time_is_carried_but_never_parsed_as_a_date(ginza_payload):
    """"Leveranstid: fran 5 vardagar" is a lead time. It is worth showing —
    orderable-in-5-days differs from orderable-but-uncertain — but it must not
    reach Amazon's date comparison."""
    results = list(_checker(FakeClient([ginza_payload])).check())
    carried = [r for r in results if r.delivery_date]
    assert carried, "fixture should include at least one delivery lead time"
    for r in carried:
        assert isinstance(r.delivery_date, str)
