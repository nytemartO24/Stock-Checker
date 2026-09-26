"""Two things that were pinging and should not: scalps, and distant dates.

Both were removed on 2026-09-16 at the user's request. Both keep DETECTING —
the flag, the note, the log line and the recorded state are all unchanged. Only
the interruption is gone, which is the distinction these tests pin down: a test
that just asserted "no alert" would pass equally well if the whole mechanism
had been deleted.
"""

import json

import yaml

from core.storage import SiteState
from sites.amazon import apply_delivery_window, beyond_window
from sites.amazon.prices import ReferencePrices
from sites.base import StockResult
from tests.conftest import ROOT

AMAZON = yaml.safe_load(
    (ROOT / "config" / "sites.yaml").read_text(encoding="utf-8"))["sites"]["amazon"]


def _state(tmp_path, entries: dict) -> SiteState:
    path = tmp_path / "amazon.json"
    path.write_text(json.dumps(entries), encoding="utf-8")
    return SiteState(path)


# --------------------------------------------------------------- scalps ----

def test_config_no_longer_alerts_on_suspected_scalps():
    assert AMAZON["alert_on_suspected_scalp"] is False


def test_a_scalp_is_still_detected_and_still_explained(tmp_path):
    """Muting the ping must not mute the reasoning: the dashboard and the log
    still need to say why a 700 kr booster is not a bargain."""
    references = ReferencePrices(tmp_path / "ref.json")
    for price in (150.0, 160.0):  # two distinct Amazon-sold sightings
        references.observe("B0TEST", price, is_amazon_seller=True)

    verdict = references.assess("B0TEST", 766.25, AMAZON["scalp_multiplier"])
    assert verdict.suspected is True
    assert "suspected scalp" in verdict.note
    assert verdict.reference_sek == 150.0


def test_a_muted_scalp_does_not_reach_you_through_a_date_change(tmp_path):
    """`alertable` has to gag the site-requested alert too, or the scalp comes
    back through the side door every time Amazon nudges its estimate."""
    state = _state(tmp_path, {"se:B0TEST": {"in_stock": False}})
    result = StockResult(
        product_id="se:B0TEST", product_name="Thing", url="https://x", in_stock=True,
        alertable=False, alert_reason="delivery date moved earlier: A → B")
    assert state.alert_kind(result) is None


# ---------------------------------------------------------------- dates ----

def test_beyond_window_agrees_with_the_availability_rule():
    max_days = AMAZON["max_delivery_days"]
    assert beyond_window(max_days + 1, max_days) is True
    assert beyond_window(max_days, max_days) is False
    # No date, or no configured window, is not "beyond" anything.
    assert beyond_window(None, max_days) is False
    assert beyond_window(999, 0) is False
    assert beyond_window(999, None) is False

    in_stock, note = apply_delivery_window(True, max_days + 1, "1 January", max_days)
    assert in_stock is False and "beyond the" in note
    assert apply_delivery_window(True, max_days, "1 January", max_days) == (True, None)


def test_a_distant_date_creeping_earlier_does_not_alert(tmp_path, monkeypatch):
    """The case that motivated this: a date two years out walking from June to
    March announced itself every time, for something you still cannot have."""
    # 300, not 500: a date beyond 400 days fails the plausibility screen and is
    # discarded before any of this, so it would prove nothing.
    urls, results = _run(tmp_path, monkeypatch, delivery_in_days=300)
    assert urls, "the stub never ran"
    assert all(r.delivery_date for r in results), "the date never parsed"
    for result in results:
        assert result.alert_reason is None
        assert result.in_stock is False
        assert any("beyond the" in note for note in result.notes)


def test_a_date_that_lands_inside_the_window_still_alerts(tmp_path, monkeypatch):
    """Nothing is lost by staying quiet: the step that finally brings the date
    within reach is measured against the far baseline and DOES ping."""
    _run(tmp_path, monkeypatch, delivery_in_days=300)          # anchors far out
    _urls, results = _run(tmp_path, monkeypatch, delivery_in_days=10)
    assert results and all(r.alert_reason for r in results)
    assert all("moved earlier" in r.alert_reason for r in results)


# --------------------------------------------------------------- harness ---

def _run(tmp_path, monkeypatch, *, delivery_in_days: int):
    """One check pass over a single ASIN whose page promises a date N days out.

    Reuses the browser stubs from the market-coverage tests so this exercises
    the REAL `_check_one`, including the delivery-state bookkeeping — the part
    that decides whether staying quiet loses the alert forever.
    """
    import datetime
    import types

    from sites import amazon as amazon_module
    from sites.amazon import AmazonChecker
    from tests.test_amazon_markets import (PINNED, FakeClient, FakePage,
                                           FakePlaywright)

    when = datetime.date.today() + datetime.timedelta(days=delivery_in_days)
    page_html = (
        '<html lang="en-gb"><body><span id="glow-ingress-line2">Sweden</span>'
        '<span id="productTitle">Beyblade X Thing</span>'
        '<input id="add-to-cart-button" value="Add to Basket">'
        f'<div id="deliveryBlockMessage">{when.day} '
        f'{when.strftime("%B")} {when.year}</div>'
        '</body></html>'
    )

    seen: list[str] = []

    class Page(FakePage):
        def content(self):
            return page_html

    monkeypatch.setattr(amazon_module.amazon_browser, "open_market",
                        lambda *_a, **_k: (types.SimpleNamespace(close=lambda: None),
                                           Page(seen), PINNED))
    monkeypatch.setattr(amazon_module.amazon_browser, "safe_goto",
                        lambda page, url, market: page.goto(url))
    monkeypatch.setattr(amazon_module, "sync_playwright", FakePlaywright)

    options = dict(AMAZON, markets=["se"], watchlist=["B0TEST"])
    checker = AmazonChecker("amazon", options, FakeClient(), tmp_path)
    return seen, list(checker.check())
