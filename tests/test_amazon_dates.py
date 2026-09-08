"""Delivery-date parsing and the "moved earlier" decision.

All offline. Both halves had failure modes that produced confidently-wrong
answers in news-notifier, so they are pinned here rather than trusted.
"""

import datetime

import pytest

from core.storage import SiteState
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


def test_no_baseline_yet_alerts(tmp_path):
    state = DeliveryState(tmp_path / "d.json")
    baseline, improved = state.assess("de:A1", "24 September", DE)
    assert improved is True and baseline is None


def test_slipping_later_re_anchors_silently(tmp_path):
    """The date we told you about is gone, so later improvements must be
    judged against what is actually promised now — but you are not pinged for
    bad news."""
    state = DeliveryState(tmp_path / "d.json")
    near, far = in_days(10), in_days(60)
    state.record("de:A1", near.strftime("%d %B"), None, True)
    baseline, improved = state.assess("de:A1", far.strftime("%d %B"), DE)
    assert improved is False
    assert baseline == far.strftime("%d %B")       # re-anchored on the worse date


def test_small_improvement_stays_quiet_but_keeps_the_baseline(tmp_path):
    """Amazon flickers estimates by a day; that is noise, not news."""
    state = DeliveryState(tmp_path / "d.json", min_improvement_days=7)
    start = in_days(30)
    state.record("de:A1", start.strftime("%d %B"), None, True)
    baseline, improved = state.assess("de:A1", (start - datetime.timedelta(days=2)).strftime("%d %B"), DE)
    assert improved is False
    assert baseline == start.strftime("%d %B")     # baseline NOT moved


def test_a_creeping_date_eventually_alerts(tmp_path):
    """The failure mode that motivated anchoring on the last ALERTED date: a
    date walking earlier one day at a time never clears the threshold in a
    single step, so comparing against the last READING would never ping."""
    state = DeliveryState(tmp_path / "d.json", min_improvement_days=7)
    start = in_days(40)
    state.record("de:A1", start.strftime("%d %B"), None, True)

    fired = []
    for step in range(1, 9):
        current = (start - datetime.timedelta(days=step)).strftime("%d %B")
        baseline, improved = state.assess("de:A1", current, DE)
        state.record("de:A1", current, baseline, improved)
        fired.append(improved)
    assert any(fired), "a date creeping earlier must eventually alert"
    assert fired.index(True) == 6, "should fire exactly when 7 days have accumulated"


def test_losing_the_date_clears_the_baseline(tmp_path):
    """So its return reads as newly promised rather than being compared to a
    stale figure."""
    state = DeliveryState(tmp_path / "d.json")
    state.record("de:A1", in_days(20).strftime("%d %B"), None, True)
    baseline, improved = state.assess("de:A1", None, DE)
    assert (baseline, improved) == (None, False)


def test_upgrading_does_not_fire_for_everything_already_dated(tmp_path):
    """An entry written before alerted_date existed is seeded from the last
    seen date, so adding the field must not produce a burst."""
    path = tmp_path / "d.json"
    path.write_text('{"de:A1": {"date": "24 September"}}', encoding="utf-8")
    _, improved = DeliveryState(path).assess("de:A1", "24 September", DE)
    assert improved is False


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
