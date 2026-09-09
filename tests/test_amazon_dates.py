"""Delivery-date parsing and the "moved earlier" decision.

All offline. Both halves had failure modes that produced confidently-wrong
answers in news-notifier, so they are pinned here rather than trusted.
"""

import datetime
import json

import pytest

from core.storage import SiteState
from sites.amazon import apply_delivery_window
from sites.amazon.dates import DeliveryState, is_plausible, parse_date
from sites.amazon.markets import MARKETS
from sites.base import StockResult

DE = MARKETS["de"]["months"]
ES = MARKETS["es"]["months"]
PL = MARKETS["pl"]["months"]


def in_days(n: int) -> datetime.date:
    return datetime.date.today() + datetime.timedelta(days=n)


@pytest.mark.parametrize("text,months", [
    ("FREE delivery Wednesday, 24 September", DE),      # day-first, English
    ("Lieferung 24. September", DE),                    # German native
    ("September 24, 2027", DE),                         # month-first (US English)
    ("entrega el 24 de septiembre", ES),                # Spanish connector
    ("dostawa 24 września", PL),                        # Polish genitive
    ("arrives Wed, 24 Sep", DE),                        # abbreviation
    ("delivery 24th September", DE),                    # ordinal
])
def test_parses_a_date_in_each_shape(text, months):
    """Each of these once failed: the original pattern was day-first only, the
    Spanish connector was missing, and Polish nominative months could not
    match a real date (dates decline to the genitive)."""
    assert parse_date(text, months) is not None


def test_abbreviation_cannot_shadow_a_longer_month_name():
    """Names are alternated longest-first so "mar" never eats "marca"."""
    assert parse_date("dostawa 3 marca", PL).month == 3


def test_year_is_assumed_next_when_the_date_already_passed():
    """Amazon usually omits the year."""
    past = datetime.date.today() - datetime.timedelta(days=40)
    text = f"delivery {past.day} {past.strftime('%B')}"
    parsed = parse_date(text, DE)
    assert parsed is not None and parsed > datetime.date.today()


def test_implausible_dates_are_rejected():
    """A bogus date is worse than none: it becomes the baseline a future
    "moved earlier" alert fires against."""
    assert is_plausible(in_days(30)) is True
    assert is_plausible(in_days(-1)) is False      # never in the past
    assert is_plausible(in_days(500)) is False     # beyond the horizon
    assert is_plausible(None) is False


def disp(n: int) -> str:
    return in_days(n).strftime("%d %B")


def iso(n: int) -> str:
    return in_days(n).isoformat()


def test_no_baseline_yet_alerts(tmp_path):
    state = DeliveryState(tmp_path / "d.json")
    baseline, alerted = state.observe("de:A1", disp(14), iso(14))
    assert alerted is True and baseline is None


def test_slipping_later_re_anchors_silently(tmp_path):
    """The date we were told about is gone, so later improvements must be judged
    against what is actually promised now — but no ping for bad news."""
    state = DeliveryState(tmp_path / "d.json")
    state.observe("de:A1", disp(10), iso(10))
    baseline, alerted = state.observe("de:A1", disp(60), iso(60))
    assert alerted is False
    assert baseline == disp(60)                      # re-anchored on the worse date


def test_small_improvement_stays_quiet_but_keeps_the_baseline(tmp_path):
    """Amazon flickers estimates by a day; that is noise, not news."""
    state = DeliveryState(tmp_path / "d.json", min_improvement_days=7)
    state.observe("de:A1", disp(30), iso(30))
    baseline, alerted = state.observe("de:A1", disp(28), iso(28))
    assert alerted is False
    assert baseline == disp(30)                      # baseline NOT moved


def test_a_creeping_date_eventually_alerts(tmp_path):
    """The failure mode that motivated anchoring on the last ALERTED date: a
    date walking earlier one day at a time never clears the threshold in a
    single step, so comparing against the last READING would never ping."""
    state = DeliveryState(tmp_path / "d.json", min_improvement_days=7)
    state.observe("de:A1", disp(40), iso(40))
    fired = [state.observe("de:A1", disp(40 - step), iso(40 - step))[1] for step in range(1, 9)]
    assert any(fired), "a date creeping earlier must eventually alert"
    assert fired.index(True) == 6, "should fire exactly when 7 days have accumulated"


def test_losing_the_date_clears_the_baseline(tmp_path):
    """So its return reads as newly promised rather than being compared to a
    stale figure."""
    state = DeliveryState(tmp_path / "d.json")
    state.observe("de:A1", disp(20), iso(20))
    assert state.observe("de:A1", None, None) == (None, False)


def test_a_yearless_baseline_cannot_fire_nonsense(tmp_path):
    """The bug this design prevents, which WAS guaranteed on a schedule.

    Amazon shows dates without a year, and the parser assumes next year for a
    date already past — so a stored "22 September" silently became September
    NEXT year once that day went by, ~350 days out, and every real date then
    looked like an enormous improvement. Plausibility screening cannot catch
    it: 350 days is inside the 400-day window. Comparing resolved dates
    removes the ambiguity rather than trying to detect it.
    """
    path = tmp_path / "d.json"
    # State as an older version wrote it: display strings only, no resolved date.
    path.write_text(json.dumps({"de:A1": {"date": disp(-30), "alerted_date": disp(-30)}}),
                    encoding="utf-8")
    baseline, alerted = DeliveryState(path).observe("de:A1", disp(14), iso(14))
    assert alerted is False, "an uncomparable baseline must not fire an alert"
    assert baseline == disp(14), "it should re-anchor quietly on the fresh date"


def test_upgrading_does_not_fire_for_everything_already_dated(tmp_path):
    """Entries written before resolved dates were stored must not produce a
    burst of alerts the first time the new code runs."""
    path = tmp_path / "d.json"
    path.write_text(json.dumps({f"de:A{i}": {"date": disp(20), "alerted_date": disp(20)}
                                for i in range(5)}), encoding="utf-8")
    state = DeliveryState(path)
    fired = [state.observe(f"de:A{i}", disp(20), iso(20))[1] for i in range(5)]
    assert not any(fired)


def test_the_resolved_date_is_persisted(tmp_path):
    """Without this the next run is back to re-parsing a yearless string."""
    path = tmp_path / "d.json"
    state = DeliveryState(path)
    state.observe("de:A1", disp(14), iso(14))
    state.save()
    stored = json.loads(path.read_text(encoding="utf-8"))["de:A1"]
    assert stored["date_iso"] == iso(14)
    assert stored["alerted_iso"] == iso(14)


def _result(**kw):
    base = dict(product_id="de:A1", product_name="Thing", url="https://x.test", in_stock=True)
    return StockResult(**{**base, **kw})


def test_site_requested_alert_is_its_own_kind(tmp_path):
    path = tmp_path / "s.json"
    seed = SiteState(path)
    seed.record(_result())
    seed.save()
    assert SiteState(path).alert_kind(_result(alert_reason="date moved earlier")) == "site"


def test_a_vetoed_listing_cannot_alert_via_its_date(tmp_path):
    """A suppressed scalp must not reach you by the side door of a date
    change."""
    path = tmp_path / "s.json"
    seed = SiteState(path)
    seed.record(_result())
    seed.save()
    assert SiteState(path).alert_kind(
        _result(alert_reason="date moved earlier", alertable=False)) is None


# --- the delivery window: a long estimate is a form of unavailability -------

def test_estimate_inside_the_window_stays_in_stock():
    assert apply_delivery_window(True, 14, "22 September", 90) == (True, None)
    assert apply_delivery_window(True, 90, "x", 90) == (True, None)   # boundary is inclusive


def test_estimate_beyond_the_window_is_not_available_yet():
    in_stock, note = apply_delivery_window(True, 180, "5 March", 90)
    assert in_stock is False
    assert "180 days out" in note and "90-day window" in note
    # Nothing is hidden: the real date is still named.
    assert "5 March" in note


def test_the_window_never_promotes_an_unavailable_listing():
    assert apply_delivery_window(False, 5, "x", 90) == (False, None)


def test_no_date_means_the_window_cannot_apply():
    """Most watched items are unavailable and have no delivery block at all."""
    assert apply_delivery_window(True, None, None, 90) == (True, None)


def test_window_disabled_by_zero_or_absent():
    assert apply_delivery_window(True, 999, "x", 0) == (True, None)
    assert apply_delivery_window(True, 999, "x", None) == (True, None)


def test_coming_inside_the_window_reads_as_a_restock(tmp_path):
    """The reason for setting in_stock False rather than muting the alert: the
    state machine then does the work."""
    path = tmp_path / "s.json"
    far, _ = apply_delivery_window(True, 180, "5 March", 90)
    seed = SiteState(path)
    seed.record(_result(in_stock=far))
    seed.save()

    near, note = apply_delivery_window(True, 30, "8 October", 90)
    assert (near, note) == (True, None)
    assert SiteState(path).alert_kind(_result(in_stock=near)) == "restock"
