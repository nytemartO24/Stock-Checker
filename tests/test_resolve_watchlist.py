"""The resolver treats Amazon as ONE catalogue reached through many domains.

Before 2026-09-16 it matched each market separately, which made a product
discovered only on .de look like partial coverage and reported it as four
separate misses on a product we were in fact watching everywhere. The checker
never behaved that way — only the reporting did — but the reporting is what a
human reads before deciding a watchlist is complete.
"""

import yaml

from scripts import resolve_watchlist as resolver
from tests.conftest import ROOT

PER_MARKET = {
    "se": {"B0AAA": "Hasbro BEY BBX Browns Canyon"},          # park codename
    "de": {"B0AAA": "Beyblade X Starter Pack Seize Jaguar HN UX Infinity",
           "B0BBB": "Beyblade X Scale Shark 4-50UF Booster Pack"},
    "es": {"B0AAA": "Beyblade X Seize Jaguar HN UX"},
}


def test_markets_come_from_the_config_not_a_constant():
    """Hard-coding them is how adding amazon.it quietly failed to extend
    discovery with it."""
    configured = yaml.safe_load(
        (ROOT / "config" / "sites.yaml").read_text(encoding="utf-8")
    )["sites"]["amazon"]["markets"]
    assert list(resolver.amazon_markets()) == list(configured)
    assert "it" in resolver.amazon_markets()


def test_an_unreadable_config_falls_back_rather_than_exploding(tmp_path):
    assert resolver.amazon_markets(tmp_path / "nope.yaml") == \
        resolver.DEFAULT_AMAZON_MARKETS


def test_markets_merge_into_one_catalogue():
    titles, seen_on = resolver.merge_amazon_catalogue(PER_MARKET)
    assert set(titles) == {"B0AAA", "B0BBB"}
    # The longest title wins as the one displayed — the most descriptive.
    assert titles["B0AAA"] == "Beyblade X Starter Pack Seize Jaguar HN UX Infinity"
    assert seen_on == {"B0AAA": ["de", "es", "se"], "B0BBB": ["de"]}


def test_a_name_matches_under_any_markets_title():
    """amazon.se calls Seize Jaguar "Browns Canyon". A resolver that searched
    only the merged display title would still find it; one that searched only
    .se's title would not. Both names must be searchable."""
    aliases = resolver.amazon_alias_titles(PER_MARKET)
    singles, _bundles = resolver.match(aliases, "Seize Jaguar", ["Seize Jaguar"])
    assert {key.split("#", 1)[0] for key, _title in singles} == {"B0AAA"}

    # And the codename is still reachable as its own entry, so nothing about
    # .se's naming is lost in the merge.
    assert "B0AAA#se" in aliases
    assert aliases["B0AAA#se"] == "Hasbro BEY BBX Browns Canyon"


def test_discovery_on_one_market_is_not_partial_coverage():
    """B0BBB is known only to .de. That is a fact about the crawler, not about
    where the product can be bought, and it must not read as a gap."""
    _titles, seen_on = resolver.merge_amazon_catalogue(PER_MARKET)
    assert seen_on["B0BBB"] == ["de"]
    aliases = resolver.amazon_alias_titles(PER_MARKET)
    singles, _ = resolver.match(aliases, "Shark Scale", ["Shark Scale"])
    assert {key.split("#", 1)[0] for key, _ in singles} == {"B0BBB"}


def test_a_model_code_is_learned_from_whichever_market_prints_it():
    """The wordiest title is not necessarily the one carrying the code, so the
    code pass must see every market's name, not just the one displayed."""
    per_market = {
        # Longest title, but no model code anywhere in it.
        "de": {"B0CCC": "Beyblade X Kreisel Set mit Starter und Launcher fuer Kinder"},
        "se": {"B0CCC": "BEY BBX Sterling Wolf 3-80FB"},
    }
    titles, _ = resolver.merge_amazon_catalogue(per_market)
    assert not resolver.codes(titles["B0CCC"]), "the displayed title has no code"

    aliases = resolver.amazon_alias_titles(per_market)
    learned = resolver.learn_codes({"amazon": aliases}, "Sterling Wolf", ["Sterling Wolf"])
    assert "3-80FB" in learned
