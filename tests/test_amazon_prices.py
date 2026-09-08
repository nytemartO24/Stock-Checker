"""Price parsing, currency detection and the scalp verdict.

All offline. This is the logic that decides whether a restock reaches you,
so it gets tested harder than the plumbing around it.
"""

import pytest

from sites.amazon.prices import (
    CORROBORATION_DISTINCT_PRICES,
    ReferencePrices,
    detect_currency,
    parse_price,
    to_sek,
)


@pytest.mark.parametrize("text,expected", [
    ("SEK766.25", 766.25),
    ("SEK\xa0766.25", 766.25),
    ("€68.63", 68.63),
    ("68,63 €", 68.63),          # comma decimal
    ("1.234,56 €", 1234.56),     # dot thousands, comma decimal
    ("1,234.56", 1234.56),       # comma thousands, dot decimal
    ("kr1.234", 1234.0),         # lone dot with 3 trailing digits = thousands
    ("kr123", 123.0),
    ("12,99 zł", 12.99),
    ("", None),
    (None, None),
    ("Currently unavailable", None),
])
def test_parse_price(text, expected):
    assert parse_price(text) == expected


@pytest.mark.parametrize("text,expected", [
    ("SEK766.25", "SEK"),
    ("kr185.00", "SEK"),
    ("€68.63", "EUR"),
    ("68,63 EUR", "EUR"),
    ("12,99 zł", "PLN"),
    ("$19.99", "USD"),
])
def test_detect_currency(text, expected):
    assert detect_currency(text, "EUR") == expected


def test_currency_detection_beats_the_market_default():
    """The bug this prevents: amazon.de pinned to Sweden quotes SEK, and
    treating that as EUR would inflate it ~11x and flag every cross-border
    listing as a scalp."""
    assert detect_currency("SEK766.25", "EUR") == "SEK"
    assert to_sek(parse_price("SEK766.25"), detect_currency("SEK766.25", "EUR")) == 766.25


def test_currency_falls_back_when_the_page_says_nothing():
    assert detect_currency("766.25", "EUR") == "EUR"


def test_unknown_currency_yields_no_normalized_price():
    """Better to have no number than a wrong one — a bogus SEK value would
    become a reference price."""
    assert to_sek(10.0, "XYZ") is None


def test_reference_only_records_amazon_sold(tmp_path):
    refs = ReferencePrices(tmp_path / "r.json")
    refs.observe("A1", 5000.0, is_amazon_seller=False)
    refs.observe("A2", 5000.0, is_amazon_seller=None)
    assert refs.reference_for("A1") == (None, True)
    assert refs.reference_for("A2") == (None, True)

    refs.observe("A1", 120.0, is_amazon_seller=True)
    assert refs.reference_for("A1")[0] == 120.0


def test_reference_keeps_the_lowest_seen(tmp_path):
    refs = ReferencePrices(tmp_path / "r.json")
    refs.observe("A1", 150.0, is_amazon_seller=True)
    refs.observe("A1", 120.0, is_amazon_seller=True)
    refs.observe("A1", 900.0, is_amazon_seller=True)
    assert refs.reference_for("A1")[0] == 120.0


def test_scalp_flagged_against_reference(tmp_path):
    """The real case: Silver Wolf at 766 SEK against a ~122 SEK street price."""
    refs = ReferencePrices(tmp_path / "r.json")
    # Two DISTINCT prices, so the reference is corroborated rather than
    # merely re-read.
    refs.observe("B0DN6YLGRX", 122.0, is_amazon_seller=True)
    refs.observe("B0DN6YLGRX", 124.0, is_amazon_seller=True)
    assert CORROBORATION_DISTINCT_PRICES == 2

    verdict = refs.assess("B0DN6YLGRX", 766.25, 2.0)
    assert verdict.suspected is True
    assert "6.3x" in verdict.note
    assert "normalised" in verdict.note
    assert "provisional" not in verdict.note


def test_fair_price_not_flagged(tmp_path):
    refs = ReferencePrices(tmp_path / "r.json")
    refs.observe("A1", 120.0, is_amazon_seller=True)
    refs.observe("A1", 120.0, is_amazon_seller=True)
    assert refs.assess("A1", 130.0, 2.0).suspected is False
    assert refs.assess("A1", 240.0, 2.0).suspected is False   # exactly at threshold
    assert refs.assess("A1", 241.0, 2.0).suspected is True


def test_single_sighting_reference_is_labelled_provisional(tmp_path):
    """Cold-start poisoning guard: one observation might itself be a scalp."""
    refs = ReferencePrices(tmp_path / "r.json")
    refs.observe("A1", 120.0, is_amazon_seller=True)
    verdict = refs.assess("A1", 900.0, 2.0)
    assert verdict.suspected is True
    assert "provisional" in verdict.note


def test_no_reference_never_suppresses(tmp_path):
    """Silently swallowing a real restock is the dangerous failure."""
    refs = ReferencePrices(tmp_path / "r.json")
    verdict = refs.assess("UNKNOWN", 9999.0, 2.0)
    assert verdict.suspected is False
    assert "no reference price" in verdict.note


def test_manual_override_wins_and_is_never_provisional(tmp_path):
    refs = ReferencePrices(tmp_path / "r.json", overrides={"b0dn6ylgrx": 130.0})
    refs.observe("B0DN6YLGRX", 5000.0, is_amazon_seller=True)
    assert refs.reference_for("B0DN6YLGRX") == (130.0, False)
    assert refs.assess("B0DN6YLGRX", 766.25, 2.0).suspected is True


def test_references_survive_a_round_trip(tmp_path):
    path = tmp_path / "r.json"
    refs = ReferencePrices(path)
    refs.observe("A1", 120.0, is_amazon_seller=True)
    refs.save()
    assert ReferencePrices(path).reference_for("A1")[0] == 120.0


def test_corrupt_reference_file_does_not_crash(tmp_path):
    path = tmp_path / "r.json"
    path.write_text("{broken", encoding="utf-8")
    assert ReferencePrices(path).reference_for("A1") == (None, True)


def test_repeating_one_price_does_not_corroborate_it(tmp_path):
    """At a half-hourly cadence, re-reading the same unchanged price would
    otherwise clear `provisional` within an hour having confirmed nothing."""
    refs = ReferencePrices(tmp_path / "r.json")
    for _ in range(6):
        refs.observe("A1", 120.0, is_amazon_seller=True)
    value, provisional = refs.reference_for("A1")
    assert value == 120.0
    assert provisional is True
    assert "provisional" in refs.assess("A1", 900.0, 2.0).note


def test_a_second_distinct_price_corroborates(tmp_path):
    refs = ReferencePrices(tmp_path / "r.json")
    refs.observe("A1", 120.0, is_amazon_seller=True)
    refs.observe("A1", 131.0, is_amazon_seller=True)
    assert refs.reference_for("A1") == (120.0, False)
    assert "provisional" not in refs.assess("A1", 900.0, 2.0).note


def test_distinct_price_list_is_capped(tmp_path):
    from sites.amazon.prices import MAX_DISTINCT_PRICES

    refs = ReferencePrices(tmp_path / "r.json")
    for i in range(20):
        refs.observe("A1", 100.0 + i, is_amazon_seller=True)
    assert len(refs._entries["A1"]["distinct_prices"]) == MAX_DISTINCT_PRICES
