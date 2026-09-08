"""Regressions for bugs found in the 2026-09-08 review of the initial build.

Each test names the failure it prevents, because the failure modes here are
all "quietly wrong later" rather than "crashes now".
"""

import json

import pytest

import main
from core.config import SiteConfig
from core.http import PoliteClient
from core.notifier import DiscordNotifier
from core.storage import SiteState
from sites.base import SiteChecker, StockResult
from sites.shopify import PAGE_SIZE, ShopifyChecker


def R(product_id="p1", *, in_stock=True):
    return StockResult(product_id, product_id, f"https://x.test/{product_id}", in_stock)


class StubChecker(SiteChecker):
    def __init__(self, results, errors=0):
        super().__init__("stub", {}, None)
        self._results = results
        self.errors = errors

    def check(self):
        yield from self._results


def _config():
    return SiteConfig(name="stub", type="shopify", options={}, rate_limit={})


def _run(monkeypatch, tmp_path, checker, notifier=None):
    monkeypatch.setattr(main, "build_checker", lambda config, client, state_dir=None: checker)
    return main.check_site(_config(), tmp_path, notifier or DiscordNotifier("", dry_run=True))


def test_partial_run_does_not_prune(monkeypatch, tmp_path):
    """A failed collection must not delete the products it would have
    reported — they'd re-alert as new on the next successful run."""
    _run(monkeypatch, tmp_path, StubChecker([R("p1"), R("p2"), R("p3"), R("p4")]))
    state_file = tmp_path / "stub.json"
    assert set(json.loads(state_file.read_text(encoding="utf-8"))) == {"p1", "p2", "p3", "p4"}

    # Next run: a collection fails, so only p1/p2 are seen.
    _run(monkeypatch, tmp_path, StubChecker([R("p1"), R("p2")], errors=1))
    assert set(json.loads(state_file.read_text(encoding="utf-8"))) == {"p1", "p2", "p3", "p4"}

    # And a clean run still prunes genuinely-gone products.
    _run(monkeypatch, tmp_path, StubChecker([R("p1"), R("p2")]))
    assert set(json.loads(state_file.read_text(encoding="utf-8"))) == {"p1", "p2"}


def test_failed_send_leaves_state_so_the_alert_retries(monkeypatch, tmp_path):
    """A Discord outage must not consume the restock notification."""
    state_file = tmp_path / "stub.json"
    _run(monkeypatch, tmp_path, StubChecker([R("p1", in_stock=False)]))

    class FailingNotifier(DiscordNotifier):
        def __init__(self):
            super().__init__("https://discord.test/hook", dry_run=False)
            self.attempts = 0

        def send(self, message):
            self.attempts += 1
            return False

    notifier = FailingNotifier()
    _run(monkeypatch, tmp_path, StubChecker([R("p1", in_stock=True)]), notifier)
    assert notifier.attempts == 1
    # Still recorded as out of stock, so the transition is still pending.
    assert json.loads(state_file.read_text(encoding="utf-8"))["p1"]["in_stock"] is False
    assert SiteState(state_file).alert_kind(R("p1", in_stock=True)) == "restock"


def test_max_retries_zero_does_not_crash():
    """max_retries: 0 in config means 'don't retry', not 'never request'."""
    assert PoliteClient(max_retries=0).max_retries == 1


def test_one_bad_product_does_not_drop_the_rest(popsplanet_payload):
    """Per CLAUDE.md, isolation is per-product, not just per-collection."""
    # `variants: 5` raises on iteration; a bare string raises on .get().
    # A null `variants` deliberately does NOT raise — it degrades to
    # "no variants, so out of stock", which is the right reading.
    payload = {"products": ["not-a-dict", {"handle": "bad", "title": "Bad", "variants": 5}]
               + popsplanet_payload["products"]}

    class Client:
        def get_json(self, url):
            return payload

    options = {"domain": "x.test", "country": "SE", "collections": ["c"]}
    checker = ShopifyChecker("x", options, Client())
    results = list(checker.check())
    assert len(results) == 3          # the three good products survived
    assert checker.errors == 2        # and both failures were recorded


def test_truncated_collection_is_flagged_as_incomplete():
    """Hitting the page cap silently would let prune delete the remainder."""
    full_page = {"products": [
        {"handle": f"h{i}", "title": f"T{i}", "variants": [{"price": "1.00", "available": True}]}
        for i in range(PAGE_SIZE)
    ]}

    class Client:
        def get_json(self, url):
            return full_page

    checker = ShopifyChecker("x", {"domain": "x.test", "collections": ["c"]}, Client())
    list(checker.check())
    assert checker.errors == 1
