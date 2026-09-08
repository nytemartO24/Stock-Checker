"""Classify a product by its title, so a price can be sanity-checked even
when that exact ASIN has no reference price yet.

WHY THIS EXISTS: the reference-price store only learns from Amazon-sold
offers. A product whose only sighting anywhere is a third-party listing —
which is precisely the scalper case — never gets a baseline, so it could
only ever be reported as "price unverified". This gives those products a
fallback.

The classification is not guesswork. Beyblade X product names are highly
regular, and the price data from the two Shopify stores (162 products,
normalized to SEK) separates cleanly once you notice that the expensive
items are MULTI-ITEM BUNDLES, identifiable by the "&" (or Italian " e ")
joining several beyblade names in one title:

    tier         n     min     p50     max
    booster     33      93     112     180
    starter     66      93     180     202
    dual        42     164     225     338
    bundle       6     356     391     605
    set          4     128     480     498
    accessory    3      93     143     214

Without bundle detection the `starter` tier spanned 93-605 and was useless
as a reference; with it, singles cluster tightly and the fallback becomes
meaningful.

The ceilings themselves live in `config/sites.yaml`, not here — they are
calibration data that will drift as the product line grows, while the
classification rules are logic.
"""

from __future__ import annotations

import re

# " & " joins bundled products on both stores; popsplanet (Italian) uses
# " e " for the same thing, and the licensed collab packs use " vs. ".
# Require a capital after " e " so it can't match a stray word inside a
# product name.
# The case-insensitive flag is scoped to the "vs." alternative only: applied
# globally it also defeats the (?=[A-Z]) guard below, so " e stuff" starts
# counting as a separator and single products get classified as dual packs.
_SEPARATORS = re.compile(r"\s&\s|(?i:\s+vs\.?\s+)|\s+e\s+(?=[A-Z])")

TIERS = ("set", "bundle", "dual", "starter", "booster", "accessory", "unknown")


def item_count(title: str) -> int:
    """How many distinct products the title appears to name."""
    return 1 + len(_SEPARATORS.findall(title or ""))


def classify(title: str) -> str:
    """Bucket a product title into a price tier.

    Order matters: a stadium set outranks its item count, and item count
    outranks the booster/starter wording, because a title like
    "Cobalt Drake 4-60F & Mirage Clock 9-65B & Shelter Drake 5-70O" is
    three boosters sold together and priced like a bundle, not a booster.
    """
    low = (title or "").lower()
    count = item_count(title)

    if "stadium" in low or "battle set" in low or "xtreme battle" in low:
        return "set"
    if count >= 3:
        return "bundle"
    if count == 2 or "dual pack" in low or "double pack" in low or "twin pack" in low:
        return "dual"
    if "starter" in low:
        return "starter"
    if "booster" in low:
        return "booster"
    if "launcher" in low or "grip" in low:
        return "accessory"
    return "unknown"


def ceiling_for(title: str, ceilings: dict) -> tuple[str, float | None]:
    """Return (tier, ceiling in SEK) for a title, or (tier, None) if the
    tier has no configured ceiling — `unknown` deliberately has none, so an
    unclassifiable product falls through to "price unverified" rather than
    being judged against a number we invented."""
    tier = classify(title)
    raw = (ceilings or {}).get(tier)
    try:
        return tier, float(raw) if raw is not None else None
    except (TypeError, ValueError):
        return tier, None
