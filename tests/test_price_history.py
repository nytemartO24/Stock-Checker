"""Tests for the price history.

The behaviour worth pinning is append-ON-CHANGE: recording every observation
would be ~20,000 points a day across ~420 products to say "still 149 kr", and a
history that grows that fast is one nobody keeps.
"""

from __future__ import annotations

import json

import pytest

from core.price_history import MAX_POINTS, PriceHistory


@pytest.fixture
def history(tmp_path):
    return PriceHistory(tmp_path / "price_history.json")


class TestObserve:
    def test_first_price_is_recorded(self, history):
        history.observe("toysnowman", "scale-shark", 149.0, "SEK", True, "Scale Shark")
        stats = history.stats("toysnowman", "scale-shark")
        assert stats["points"] == 1
        assert stats["low"] == 149.0

    def test_unchanged_price_is_not_appended(self, history):
        for _ in range(50):
            history.observe("toysnowman", "scale-shark", 149.0, "SEK", True)
        assert history.stats("toysnowman", "scale-shark")["points"] == 1

    def test_change_is_appended(self, history):
        history.observe("toysnowman", "scale-shark", 149.0, "SEK", True)
        history.observe("toysnowman", "scale-shark", 92.0, "SEK", True)
        stats = history.stats("toysnowman", "scale-shark")
        assert stats["points"] == 2
        assert stats["low"] == 92.0
        assert stats["high"] == 149.0
        assert stats["current"] == 92.0

    def test_stock_change_at_same_price_is_recorded(self, history):
        # A price seen only while unbuyable is not an opportunity, so the two
        # states have to be distinguishable when the history is read back.
        history.observe("toysnowman", "x", 149.0, "SEK", True)
        history.observe("toysnowman", "x", 149.0, "SEK", False)
        assert history.stats("toysnowman", "x")["points"] == 2

    def test_missing_price_records_nothing(self, history):
        history.observe("amazon", "se:B0FCYT9DG6", None, "SEK", False)
        assert history.stats("amazon", "se:B0FCYT9DG6") is None

    def test_low_carries_its_stock_state(self, history):
        history.observe("rarewaves", "5010996385222", 134.0, "SEK", True)
        history.observe("rarewaves", "5010996385222", 88.0, "SEK", False)
        stats = history.stats("rarewaves", "5010996385222")
        assert stats["low"] == 88.0
        assert stats["low_in_stock"] is False

    def test_sites_do_not_collide(self, history):
        history.observe("gameshop", "abc", 100.0, "SEK", True)
        history.observe("ginza", "abc", 200.0, "SEK", True)
        assert history.stats("gameshop", "abc")["current"] == 100.0
        assert history.stats("ginza", "abc")["current"] == 200.0

    def test_points_are_capped(self, history):
        for i in range(MAX_POINTS + 60):
            history.observe("s", "p", float(i + 1), "SEK", True)
        stats = history.stats("s", "p")
        assert stats["points"] == MAX_POINTS
        # The oldest were dropped, so the surviving minimum has moved up.
        assert stats["low"] > 1.0


class TestPersistence:
    def test_round_trip(self, tmp_path):
        path = tmp_path / "price_history.json"
        first = PriceHistory(path)
        first.observe("toysnowman", "scale-shark", 149.0, "SEK", True, "Scale Shark")
        first.save()

        second = PriceHistory(path)
        second.observe("toysnowman", "scale-shark", 92.0, "SEK", True)
        stats = second.stats("toysnowman", "scale-shark")
        assert stats["points"] == 2
        assert stats["low"] == 92.0

    def test_save_is_a_noop_when_nothing_changed(self, tmp_path):
        path = tmp_path / "price_history.json"
        history = PriceHistory(path)
        history.observe("s", "p", 10.0, "SEK", True)
        history.save()
        before = path.stat().st_mtime_ns

        again = PriceHistory(path)
        again.observe("s", "p", 10.0, "SEK", True)   # unchanged
        again.save()
        assert path.stat().st_mtime_ns == before

    def test_corrupt_file_does_not_raise(self, tmp_path):
        path = tmp_path / "price_history.json"
        path.write_text("{not json", encoding="utf-8")
        history = PriceHistory(path)          # must not raise
        history.observe("s", "p", 5.0, "SEK", True)
        assert history.stats("s", "p")["current"] == 5.0

    def test_older_shorter_points_are_tolerated(self, tmp_path):
        # Forward compatibility with a two-field point, so a format change
        # cannot make an existing history unreadable.
        path = tmp_path / "price_history.json"
        path.write_text(json.dumps(
            {"s:p": {"name": "x", "points": [["2026-01-01T00:00:00+00:00", 42.0]]}}),
            encoding="utf-8")
        stats = PriceHistory(path).stats("s", "p")
        assert stats["low"] == 42.0
        assert stats["low_in_stock"] is False
