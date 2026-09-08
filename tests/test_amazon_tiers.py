"""Title-based price tiers, and the fallback they feed.

The tier ceilings were calibrated from 162 real products; these tests pin
the classification rules that make those numbers meaningful, plus the
precedence between a real observation and a tier estimate.
"""

import pytest

from sites.amazon.prices import ReferencePrices
from sites.amazon.tiers import ceiling_for, classify, item_count

CEILINGS = {"booster": 190, "starter": 210, "dual": 350, "bundle": 620, "set": 520, "accessory": 230}

# Titles lifted verbatim from the two Shopify catalogues and a real Amazon
# listing, so the rules are pinned against text that actually occurs.
@pytest.mark.parametrize("title,expected", [
    ("Beyblade X - Booster: Cobalt Drake 4-60F", "booster"),
    ("Beyblade X Dagger Dran 4-70Q Booster Pack", "booster"),
    ("Beyblade X Claw Leon 5-60P Starter Pack Set", "starter"),
    ("Beyblade X Sterling Wolf 3-80FB UX Starter Pack Set with Endurance Type Top", "starter"),
    ("Beyblade X Clamp Crab 9-65S & Crest Leon 4-55A Dual Pack", "dual"),
    ("Beyblade X - Transformers Dual Pack: Optimus Prime 4-60P e Megatron 4-80", "dual"),
    ("Beyblade X Cobalt Drake 4-60F & Mirage Clock 9-65B & Shelter Drake 5-70O", "bundle"),
    ("Beyblade X Xtreme Battle Set", "set"),
    ("Beyblade X Sneak Attack Battle Set Beystadium", "set"),
    ("Beyblade X String Launcher Grip", "accessory"),
    ("Some Unrelated Toy", "unknown"),
])
def test_classify(title, expected):
    assert classify(title) == expected


def test_bundles_outrank_the_booster_wording():
    """Three boosters sold together are priced like a bundle, not a booster.
    Without this, the starter tier spanned 93-605 SEK and was useless."""
    title = "Beyblade X Gust Bat 3-85GP & Savage Bear 5-60F & Curse Mummy 7-55W Booster"
    assert item_count(title) == 3
    assert classify(title) == "bundle"


def test_italian_separator_counts_as_a_bundle_join():
    """popsplanet writes Italian, where ' e ' joins the products."""
    assert item_count("Optimus Prime 4-60P e Megatron 4-80") == 2


def test_lowercase_e_inside_a_name_is_not_a_separator():
    """Requiring a capital after ' e ' stops a stray word splitting a title."""
    assert item_count("Beyblade X Roar Tyranno 9-60FG booster e stuff") == 1


def test_unknown_tier_has_no_ceiling():
    """An unclassifiable product must fall through to 'price unverified'
    rather than being judged against a number we invented."""
    assert ceiling_for("Some Unrelated Toy", CEILINGS) == ("unknown", None)


def test_ceiling_lookup():
    tier, ceiling = ceiling_for("Beyblade X Claw Leon 5-60P Starter Pack Set", CEILINGS)
    assert (tier, ceiling) == ("starter", 210.0)


def test_tier_fallback_flags_the_motivating_scalper(tmp_path):
    """B0DN6YLGRX: a starter pack at 766 SEK, whose only observations
    anywhere are third-party, so it has no per-ASIN reference."""
    refs = ReferencePrices(tmp_path / "r.json")
    tier, ceiling = ceiling_for("Beyblade X Sterling Wolf 3-80FB UX Starter Pack Set", CEILINGS)
    verdict = refs.assess("B0DN6YLGRX", 766.25, 2.0, tier=tier, tier_ceiling=ceiling)
    assert verdict.suspected is True
    assert "starter" in verdict.note
    assert "no per-product reference yet" in verdict.note


def test_tier_fallback_does_not_flag_a_fair_price(tmp_path):
    refs = ReferencePrices(tmp_path / "r.json")
    assert refs.assess("X", 200.0, 2.0, tier="starter", tier_ceiling=210).suspected is False
    # Ceilings are lenient on purpose — weak evidence must not cry wolf.
    assert refs.assess("X", 420.0, 2.0, tier="starter", tier_ceiling=210).suspected is False
    assert refs.assess("X", 421.0, 2.0, tier="starter", tier_ceiling=210).suspected is True


def test_observed_reference_beats_the_tier_estimate(tmp_path):
    """A real Amazon-sold sighting is stronger evidence and must win."""
    refs = ReferencePrices(tmp_path / "r.json")
    refs.observe("X", 120.0, is_amazon_seller=True)
    refs.observe("X", 124.0, is_amazon_seller=True)
    verdict = refs.assess("X", 300.0, 2.0, tier="starter", tier_ceiling=210)
    assert verdict.suspected is True
    assert "reference" in verdict.note and "typical ceiling" not in verdict.note


def test_no_reference_and_no_tier_still_never_suppresses(tmp_path):
    refs = ReferencePrices(tmp_path / "r.json")
    verdict = refs.assess("X", 9999.0, 2.0, tier="unknown", tier_ceiling=None)
    assert verdict.suspected is False
    assert "no reference price" in verdict.note
