from pathlib import Path

import pytest

from core.config import load_config
from core.notifier import DiscordNotifier, format_stock_alert, truncate
from sites.base import StockResult


def test_alert_includes_price_seller_notes_and_url():
    result = StockResult(
        product_id="B0DN6YLGRX",
        product_name="Beyblade X Sterling Wolf 3-80FB UX Starter Pack",
        url="https://www.amazon.de/dp/B0DN6YLGRX",
        in_stock=True,
        price_text="€68.63",
        seller="London Lane Company",
        notes=["suspected scalp: 5.3x reference price"],
    )
    message = format_stock_alert("amazon-de", result)
    assert "In stock on amazon-de" in message
    assert "€68.63" in message
    assert "sold by London Lane Company" in message
    assert "suspected scalp: 5.3x reference price" in message
    assert message.endswith("https://www.amazon.de/dp/B0DN6YLGRX")


def test_alert_omits_empty_fields():
    result = StockResult(product_id="h", product_name="Thing", url="https://x.test/h", in_stock=True)
    lines = format_stock_alert("popsplanet", result).splitlines()
    assert len(lines) == 2  # headline + url, no empty detail line


def test_truncate_collapses_whitespace_and_ellipsises():
    assert truncate("a   b") == "a b"
    assert truncate("x" * 100).endswith("…")
    assert len(truncate("x" * 100)) == 70


def test_dry_run_never_posts(caplog):
    notifier = DiscordNotifier("https://discord.test/hook", dry_run=True)
    assert notifier.send("hello") is False


def test_missing_webhook_does_not_raise():
    notifier = DiscordNotifier("", dry_run=False)
    assert notifier.send("hello") is False


def test_config_merges_defaults_per_key(tmp_path: Path):
    path = tmp_path / "sites.yaml"
    path.write_text(
        "defaults:\n"
        "  rate_limit:\n"
        "    min_delay_seconds: 1.0\n"
        "    max_delay_seconds: 3.0\n"
        "    max_retries: 3\n"
        "sites:\n"
        "  a:\n"
        "    type: shopify\n"
        "    domain: a.test\n"
        "    rate_limit:\n"
        "      max_delay_seconds: 9.0\n"
        "  b:\n"
        "    type: shopify\n"
        "    enabled: false\n"
        "    domain: b.test\n",
        encoding="utf-8",
    )
    sites = {s.name: s for s in load_config(path)}
    # Overriding one dimension must not discard the others.
    assert sites["a"].max_delay == 9.0
    assert sites["a"].min_delay == 1.0
    assert sites["a"].max_retries == 3
    assert sites["a"].options["domain"] == "a.test"
    assert sites["b"].enabled is False


def test_site_without_type_is_rejected(tmp_path: Path):
    path = tmp_path / "sites.yaml"
    path.write_text("sites:\n  a:\n    domain: a.test\n", encoding="utf-8")
    with pytest.raises(ValueError, match="no 'type'"):
        load_config(path)


def test_real_config_loads_and_is_wired_to_known_types():
    from sites import CHECKER_TYPES

    sites = load_config(Path(__file__).resolve().parent.parent / "config" / "sites.yaml")
    assert sites, "config/sites.yaml defines no sites"
    for site in sites:
        assert site.type in CHECKER_TYPES
        assert site.options.get("domain")
        assert site.options.get("collections")
        # An unpinned market makes `currency` a guess rather than a fact.
        assert site.options.get("country"), f"{site.name} must pin a country"
        assert site.options.get("currency"), f"{site.name} must declare a currency"
