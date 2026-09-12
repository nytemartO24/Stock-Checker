"""Regressions for bugs found reviewing the Amazon module before it landed.

Each of these was a way to get a confidently-wrong answer, which is the
failure mode this project cares about most.
"""

import json

from core.storage import SiteState
from sites.amazon import parse_product
from sites.amazon.markets import MARKETS
from sites.base import StockResult

# Every real product page carries the glow ingress naming the pinned
# destination. Omitting it from a fixture is what let the original
# whole-page country check look correct while being dead code.
GLOW = '<span id="glow-ingress-line2">Sweden</span>'


def _page(body: str) -> str:
    return f'<html lang="en-gb"><body>{GLOW}{body}</body></html>'


def test_wrong_destination_detected_despite_glow_ingress():
    """The guard must read the country out of the BANNER. Asking whether
    'Sweden' appears anywhere on the page is always true once the location
    is pinned, so the check never fired."""
    html = _page(
        '<span id="productTitle">Thing</span>'
        '<p>International Shopping Transition Alert We are showing you items that '
        'dispatch to United States. To see items that dispatch to a different '
        'country, change your delivery address.</p>'
        '<input id="add-to-cart-button" value="Add to Basket">'
    )
    parsed = parse_product(html, MARKETS["de"], delivery_country="Sweden")
    assert parsed.untrusted is not None
    assert "United States" in parsed.untrusted
    assert parsed.in_stock is False


def test_requested_destination_still_reads_normally_with_glow_ingress():
    """Cross-border pages are the goal, not a failure — .de dispatching to
    Sweden is exactly the question being asked."""
    html = _page(
        '<span id="productTitle">Thing</span>'
        '<p>International Shopping Transition Alert We are showing you items that '
        'dispatch to Sweden. To see items that dispatch to a different country, '
        'change your delivery address.</p>'
        '<input id="add-to-cart-button" value="Add to Basket">'
    )
    parsed = parse_product(html, MARKETS["de"], delivery_country="Sweden")
    assert parsed.untrusted is None
    assert parsed.in_stock is True


def test_banner_second_clause_is_not_mistaken_for_the_destination():
    """'...dispatch to a different country' must not parse as the country."""
    html = _page(
        '<span id="productTitle">Thing</span>'
        '<p>International Shopping Transition Alert We are showing you items that '
        'dispatch to Sweden. To see items that dispatch to a different country, '
        'change your delivery address.</p>'
    )
    assert parse_product(html, MARKETS["de"], delivery_country="Sweden").untrusted is None


def test_banner_with_unreadable_destination_is_untrusted():
    """If we can't tell where the page is about, we can't trust it."""
    html = _page(
        '<span id="productTitle">Thing</span>'
        '<p>International Shopping Transition Alert something we cannot parse</p>'
        '<input id="add-to-cart-button" value="Add to Basket">'
    )
    parsed = parse_product(html, MARKETS["de"], delivery_country="Sweden")
    assert parsed.untrusted is not None
    assert "unknown country" in parsed.untrusted


def test_storage_refuses_to_prune_against_an_empty_view(tmp_path):
    """Seeing nothing is a failure signature, not proof everything vanished.
    An emptied watchlist wiped all 24 stored entries before this guard."""
    path = tmp_path / "amazon.json"
    state = SiteState(path)
    for i in range(3):
        state.record(StockResult(f"se:A{i}", f"A{i}", "https://x.test", True))
    state.save()

    reopened = SiteState(path)
    assert reopened.prune(set()) == 0
    reopened.save()
    assert len(json.loads(path.read_text(encoding="utf-8"))) == 3


def test_prune_still_works_with_a_non_empty_view(tmp_path):
    path = tmp_path / "amazon.json"
    state = SiteState(path)
    for i in range(3):
        state.record(StockResult(f"se:A{i}", f"A{i}", "https://x.test", True))
    # Two calls: prune() now requires an absence to persist for a second run
    # before deleting, because deleting on the first miss caused two alert
    # storms. The orphan-cleanup intent this test guards is unchanged.
    assert state.prune({"se:A0"}) == 0
    assert state.prune({"se:A0"}) == 2
