"""Every watchlisted ASIN is checked on EVERY configured Amazon domain.

An ASIN is the same physical product whichever Amazon serves it, so which
market first surfaced one says nothing about where it can be bought. Skipping a
domain — because its listing is titled differently, is missing, or redirects
somewhere else — is how a restock on that domain goes unnoticed, and "we only
half-covered the Amazons" is a complaint this project has already had once.

These tests drive the real `AmazonChecker.check()` loop with the browser stubbed
out, so they fail if anything ever starts filtering markets per product.
"""

import types

import pytest
import yaml

from sites.amazon import AmazonChecker
from tests.conftest import ROOT

CONFIG = yaml.safe_load((ROOT / "config" / "sites.yaml").read_text(encoding="utf-8"))
AMAZON = CONFIG["sites"]["amazon"]

GLOW = '<span id="glow-ingress-line2">Sweden</span>'
PAGE = (
    f'<html lang="en-gb"><body>{GLOW}'
    '<span id="productTitle">Beyblade X Thing</span>'
    '<div id="availability">Currently unavailable</div>'
    '</body></html>'
)


class FakePage:
    """Answers like a product page that loaded fine, and remembers every URL."""

    def __init__(self, visited: list[str]) -> None:
        self._visited = visited
        self.url = ""

    def goto(self, url, **_kwargs):
        self._visited.append(url)
        self.url = url

    def wait_for_timeout(self, _ms):
        pass

    def wait_for_selector(self, _selector, **_kwargs):
        pass

    def content(self):
        return PAGE

    def locator(self, _selector):
        return types.SimpleNamespace(count=lambda: 0)


class FakeClient:
    def __init__(self) -> None:
        self.pacer = types.SimpleNamespace(wait=lambda: None)


class FakePlaywright:
    """`with sync_playwright() as p`. Dunders must be on the CLASS, which is
    why this is not another SimpleNamespace."""

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


@pytest.fixture
def visits(monkeypatch, tmp_path):
    """Run the real check loop against a stubbed browser; return the URLs asked for."""
    from sites import amazon as amazon_module

    seen: list[str] = []

    def fake_open_market(_playwright, _market, _config, **_kwargs):
        page = FakePage(seen)
        handle = types.SimpleNamespace(close=lambda: None)
        return handle, page, "Sweden", True

    monkeypatch.setattr(amazon_module.amazon_browser, "open_market", fake_open_market)
    monkeypatch.setattr(amazon_module.amazon_browser, "safe_goto",
                        lambda page, url, market: page.goto(url))
    monkeypatch.setattr(amazon_module, "sync_playwright", FakePlaywright)

    def run(options):
        checker = AmazonChecker("amazon", options, FakeClient(), tmp_path)
        results = list(checker.check())
        return seen, results, checker

    return run


def test_every_asin_is_requested_on_every_market(visits):
    """The live config, not a toy one: this is the thing that must hold."""
    urls, results, checker = visits(dict(AMAZON))

    markets, watchlist = AMAZON["markets"], AMAZON["watchlist"]
    assert len(urls) == len(markets) * len(watchlist)
    for market in markets:
        for asin in watchlist:
            assert any(f"amazon.{market}/" in url and asin in url for url in urls), (
                f"{asin} was never requested on amazon.{market}")
    # And every one of them produced a result, namespaced per market, so a
    # restock on one domain is its own event.
    assert {r.product_id for r in results} == {
        f"{market}:{asin}" for market in markets for asin in watchlist}
    assert checker.errors == 0


def test_italy_is_one_of_them(visits):
    """amazon.it was added 2026-09-16 and must actually be visited."""
    assert "it" in AMAZON["markets"]
    urls, _results, _checker = visits(dict(AMAZON))
    assert sum("amazon.it/" in url for url in urls) == len(AMAZON["watchlist"])


def test_a_market_missing_from_a_products_catalogue_is_still_checked(visits):
    """B0FCYT9DG6 was only ever DISCOVERED on .se. It is checked everywhere."""
    urls, _results, _checker = visits(dict(AMAZON))
    hosts = {url.split("/-/")[0] for url in urls if "B0FCYT9DG6" in url}
    assert hosts == {f"https://www.amazon.{m}" for m in AMAZON["markets"]}
