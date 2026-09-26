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
from sites.amazon.browser import Destination
from tests.conftest import ROOT

PINNED = Destination("Karlskrona 371 16", usable=True, exact=True)

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
        return handle, page, PINNED

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


def test_a_page_with_no_title_is_a_failure_not_an_unavailable(monkeypatch, tmp_path):
    """An unrendered page has no add-to-cart button and no availability copy, so
    it parses exactly like a sold-out listing. Recording that would make "we
    never read the page" the baseline a real restock fires against — which is
    what amazon.it did on every cron read the day it was added."""
    from sites import amazon as amazon_module

    seen: list[str] = []

    class Blank(FakePage):
        def content(self):
            # A plausible shell: the chrome renders, the product does not.
            return '<html lang="en-gb"><body><div id="nav-belt"></div></body></html>'

    monkeypatch.setattr(amazon_module.amazon_browser, "open_market",
                        lambda *_a, **_k: (types.SimpleNamespace(close=lambda: None),
                                           Blank(seen), PINNED))
    monkeypatch.setattr(amazon_module.amazon_browser, "safe_goto",
                        lambda page, url, market: page.goto(url))
    monkeypatch.setattr(amazon_module, "sync_playwright", FakePlaywright)

    options = dict(AMAZON, markets=["it"], watchlist=["B0TEST"])
    checker = AmazonChecker("amazon", options, FakeClient(), tmp_path)
    results = list(checker.check())

    assert results == [], "a titleless page must not become a StockResult"
    assert seen, "it still visited the page"
    # errors is what stops main.py pruning state against an incomplete view, so
    # a run of these cannot quietly delete the products it failed to read.
    assert checker.errors == 1


class _StuckThenFine(FakePage):
    """First navigation sticks on chrome-error; a FRESH page works.

    Models the real behaviour this project documented for the warm-up: Amazon's
    spurious download prompt aborts a navigation and leaves the page at
    chrome-error://chromewebdata/, where every further goto on THAT page lands on
    chrome-error again. So a same-page retry cannot recover and a new page can.
    """

    def __init__(self, visited, context):
        super().__init__(visited)
        self.context = context
        self.stuck = True
        self.closed = False

    def goto(self, url, **_kwargs):
        self._visited.append(url)
        self.url = "chrome-error://chromewebdata/" if self.stuck else url

    def close(self):
        self.closed = True


class _Context:
    """Hands out pages; only the first one is stuck."""

    def __init__(self, visited):
        self._visited = visited
        self.pages = []

    def new_page(self):
        page = _StuckThenFine(self._visited, self)
        page.stuck = not self.pages  # the first page is the broken one
        self.pages.append(page)
        return page


def test_a_stuck_navigation_is_recovered_by_replacing_the_page(monkeypatch, tmp_path):
    """The old code re-issued the goto on the SAME page, which lands on
    chrome-error again — a retry that could never work. That cost the ASIN and,
    because it counts as an error, blocked pruning for the whole site."""
    from sites import amazon as amazon_module

    visited: list[str] = []
    context = _Context(visited)
    first = context.new_page()

    monkeypatch.setattr(amazon_module.amazon_browser, "open_market",
                        lambda *_a, **_k: (types.SimpleNamespace(close=lambda: None),
                                           first, PINNED))
    monkeypatch.setattr(amazon_module.amazon_browser, "safe_goto",
                        lambda page, url, market: page.goto(url))
    monkeypatch.setattr(amazon_module, "sync_playwright", FakePlaywright)

    options = dict(AMAZON, markets=["se"], watchlist=["B0TEST"])
    checker = AmazonChecker("amazon", options, FakeClient(), tmp_path)
    results = list(checker.check())

    assert first.closed, "the broken page must be closed, not retried"
    assert len(context.pages) == 2, "a replacement page must be created"
    assert results, "and the product is then read successfully"
    assert checker.errors == 0, "a recovered navigation is not a failure to see"
