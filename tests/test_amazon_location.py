"""Two questions the location widget answers, and why they must stay separate.

amazon.se stopped applying its postcode on 2026-09-23 and was failing 96% of
runs by 2026-09-26, while de/fr/es/it stayed near-perfect. The cost was out of
all proportion to what was actually lost: one unpinned market marked every
Amazon run incomplete, which disabled pruning for ALL FIVE markets, so state
held products that had been dropped from the watchlist days earlier.

What was actually lost was the CITY. Measured on an unpinned .se session: prices
in SEK, a normal Swedish delivery promise, no international shopping banner. The
domestic store already answers for its own country; the postcode only sharpens
the estimate. On a FOREIGN market the same failure is fatal, because the country
really is unknown.
"""

import types

import pytest

from sites.amazon import AmazonChecker
from sites.amazon.browser import Destination, classify_destination
from sites.amazon.markets import MARKETS
from tests.test_amazon_markets import (AMAZON, FakeClient, FakePage,
                                       FakePlaywright)

SE, DE = MARKETS["se"], MARKETS["de"]


def _classify(text, *, market="se", country="Sweden", postcode="371 16"):
    domestic = MARKETS[market]["country"].strip().lower() == country.strip().lower()
    return classify_destination(text, market, domestic=domestic,
                                country=country, postcode=postcode)


# ------------------------------------------------------- the applied cases ---

def test_domestic_postcode_applied_is_exact():
    got = _classify("Karlskrona 371 16")
    assert (got.usable, got.exact, got.note) == (True, True, None)


def test_foreign_country_applied_is_exact():
    got = _classify("Sweden", market="de")
    assert (got.usable, got.exact, got.note) == (True, True, None)


# ------------------------------------------------------ the degraded cases ---

def test_domestic_without_a_postcode_is_usable_but_not_exact():
    """The whole point: amazon.se already answers for Sweden."""
    got = _classify("Update location")
    assert got.usable is True
    assert got.exact is False
    assert "country-level" in got.note
    # The note must name what was lost, or it reads as an unexplained warning.
    assert "371 16" in got.note


def test_foreign_without_a_country_is_NOT_usable():
    """Here the country genuinely is unknown — amazon.de may be quoting Germany."""
    got = _classify("Update location", market="de")
    assert got.usable is False
    assert got.exact is False
    assert "other than Sweden" in got.note


def test_an_empty_widget_reads_the_same_way():
    assert _classify("").usable is True           # domestic
    assert _classify("", market="de").usable is False


def test_an_unset_postcode_does_not_match_everything():
    """A blank postcode must not substring-match its way to `exact`: "" is in
    every string, and that would have reported an unpinned market as pinned."""
    got = _classify("Update location", postcode="")
    assert got.exact is False
    assert got.usable is True                     # still domestic, still fine


# ------------------------------------------------- what the checker does ------

def _run(monkeypatch, tmp_path, destination, market="se"):
    from sites import amazon as amazon_module

    seen: list[str] = []
    monkeypatch.setattr(amazon_module.amazon_browser, "open_market",
                        lambda *_a, **_k: (types.SimpleNamespace(close=lambda: None),
                                           FakePage(seen), destination))
    monkeypatch.setattr(amazon_module.amazon_browser, "safe_goto",
                        lambda page, url, market: page.goto(url))
    monkeypatch.setattr(amazon_module, "sync_playwright", FakePlaywright)

    options = dict(AMAZON, markets=[market], watchlist=["B0TEST"])
    checker = AmazonChecker("amazon", options, FakeClient(), tmp_path)
    return list(checker.check()), checker


def test_an_imprecise_market_still_counts_as_seen(monkeypatch, tmp_path):
    """errors is what stops main.py pruning. A missing postcode must not stop it,
    or dropped watchlist entries linger in state indefinitely."""
    degraded = Destination("Update location", usable=True, exact=False,
                           note="postcode not applied on the domestic market")
    results, checker = _run(monkeypatch, tmp_path, degraded)
    assert checker.errors == 0, "an imprecise destination is not a failure to see"
    assert results, "and the products are still reported"
    assert all("postcode not applied" in note for r in results for note in r.notes)


def test_a_wrong_country_market_still_blocks_pruning(monkeypatch, tmp_path):
    """The guard that matters is untouched."""
    unusable = Destination("Update location", usable=False, exact=False,
                           note="delivery location not applied")
    _results, checker = _run(monkeypatch, tmp_path, unusable, market="de")
    assert checker.errors >= 1


def test_a_pinned_market_carries_no_note(monkeypatch, tmp_path):
    pinned = Destination("Karlskrona 371 16", usable=True, exact=True)
    results, checker = _run(monkeypatch, tmp_path, pinned)
    assert checker.errors == 0
    assert all(not r.notes or "location" not in " ".join(r.notes) for r in results)


# --------------------------------------- every exit returns a Destination ----

class _NoOpenerPage:
    """A page where the location opener simply is not there.

    The failure this pins down was found by a live run, not a test:
    set_delivery_location had FIVE exits and only the last one was converted to
    return a Destination, so every early path crashed open_market with
    "'str' object has no attribute 'exact'". A stub this small would have caught
    it, which is the point.
    """

    url = "https://www.amazon.se/-/en/"

    def locator(self, _selector):
        return types.SimpleNamespace(count=lambda: 0, first=None)

    def wait_for_timeout(self, _ms):
        pass


@pytest.mark.parametrize("market,country", [("se", "Sweden"), ("de", "Sweden")])
def test_every_exit_returns_a_destination(market, country):
    from sites.amazon.browser import set_delivery_location

    got = set_delivery_location(_NoOpenerPage(), market, MARKETS[market],
                                country, "371 16")
    assert isinstance(got, Destination)
    # And it still judges correctly: no widget at all on the domestic market is
    # imprecise; on a foreign one it is unusable.
    assert got.exact is False
    assert got.usable is (market == "se")
