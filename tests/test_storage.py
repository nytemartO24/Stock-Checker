"""The new-vs-unchanged decision, which is the thing that decides whether
you get spammed."""

import json

from core.storage import SiteState
from sites.base import StockResult


def result(product_id="p1", *, in_stock=True, alertable=True, name="Thing"):
    return StockResult(
        product_id=product_id,
        product_name=name,
        url=f"https://example.test/products/{product_id}",
        in_stock=in_stock,
        alertable=alertable,
    )


def test_first_run_seeds_silently(tmp_path):
    """An empty state file must not fire one alert per in-stock product."""
    state = SiteState(tmp_path / "s.json")
    assert state.is_first_run
    assert state.should_notify(result()) is False


def test_restock_transition_notifies(tmp_path):
    path = tmp_path / "s.json"
    first = SiteState(path)
    first.record(result(in_stock=False))
    first.save()

    second = SiteState(path)
    assert second.is_first_run is False
    assert second.should_notify(result(in_stock=True)) is True


def test_staying_in_stock_does_not_re_notify(tmp_path):
    path = tmp_path / "s.json"
    first = SiteState(path)
    first.record(result(in_stock=True))
    first.save()
    assert SiteState(path).should_notify(result(in_stock=True)) is False


def test_new_product_already_in_stock_notifies(tmp_path):
    path = tmp_path / "s.json"
    first = SiteState(path)
    first.record(result("other", in_stock=True))
    first.save()
    assert SiteState(path).should_notify(result("brand-new", in_stock=True)) is True


def test_unalertable_result_is_never_notified(tmp_path):
    """The site's own veto (Amazon scalp detection) wins over the transition."""
    path = tmp_path / "s.json"
    first = SiteState(path)
    first.record(result(in_stock=False))
    first.save()
    assert SiteState(path).should_notify(result(in_stock=True, alertable=False)) is False


def test_prune_drops_unwatched_entries(tmp_path):
    path = tmp_path / "s.json"
    state = SiteState(path)
    state.record(result("keep"))
    state.record(result("drop"))
    assert state.prune({"keep"}) == 1
    state.save()
    assert set(json.loads(path.read_text(encoding="utf-8"))) == {"keep"}


def test_first_seen_survives_updates(tmp_path):
    path = tmp_path / "s.json"
    state = SiteState(path)
    state.record(result(in_stock=False))
    original = state._entries["p1"]["first_seen"]
    state.record(result(in_stock=True))
    assert state._entries["p1"]["first_seen"] == original


def test_corrupt_state_is_treated_as_first_run(tmp_path):
    """A truncated file must not crash the run or alert on everything."""
    path = tmp_path / "s.json"
    path.write_text("{not json", encoding="utf-8")
    state = SiteState(path)
    assert state.is_first_run is True
    assert state.should_notify(result()) is False


def test_save_is_atomic_and_leaves_no_temp_files(tmp_path):
    path = tmp_path / "s.json"
    state = SiteState(path)
    state.record(result())
    state.save()
    assert json.loads(path.read_text(encoding="utf-8"))["p1"]["in_stock"] is True
    assert [p.name for p in tmp_path.iterdir()] == ["s.json"]
