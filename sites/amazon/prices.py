"""Price parsing and the reference-price store behind scalp detection.

Pure logic, no browser and no network, so it is fully testable offline —
which matters because this is the part that decides whether you get told
about a restock.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from core.parsing import detect_currency, parse_price  # noqa: F401  (re-export)

logger = logging.getLogger(__name__)

# Static, deliberately approximate. Scalping on this product line runs 4-5x,
# so a rate being a few percent stale cannot flip a 2x verdict. Keeping it
# static avoids a live FX dependency that could fail a run.
FX_TO_SEK = {
    "SEK": 1.0,
    "EUR": 11.30,
    "PLN": 2.65,
    "DKK": 1.52,
    "GBP": 13.30,
    "CAD": 7.90,
    "USD": 10.50,
}

# Corroboration threshold, counted in DISTINCT observed prices. One sighting
# could itself be the scalp price, so a reference stays provisional (and says
# so in the alert) until independent evidence arrives. Counting raw sightings
# would be meaningless: at a half-hourly cadence, re-reading the same
# unchanged price clears the flag within an hour without corroborating
# anything.
CORROBORATION_DISTINCT_PRICES = 2

# Cap on how many distinct prices we remember per ASIN. We only need to know
# whether there is more than one; the list is evidence, not history.
MAX_DISTINCT_PRICES = 5


def to_sek(value: float | None, currency: str | None) -> float | None:
    if value is None or not currency:
        return None
    rate = FX_TO_SEK.get(currency.upper())
    if rate is None:
        logger.warning("no FX rate for %r — cannot normalize %s", currency, value)
        return None
    return value * rate


@dataclass
class ScalpVerdict:
    """Why a price was or wasn't flagged. `note` is rendered in the alert."""

    suspected: bool
    note: str | None = None
    reference_sek: float | None = None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ReferencePrices:
    """Lowest normalized price ever seen from an Amazon-sold offer, per ASIN.

    Stored separately from per-market alert state so clearing state to
    re-seed alerts doesn't destroy accumulated price knowledge.

    Only Amazon-sold offers contribute. A third-party price is not evidence
    of what a product costs — it is exactly the thing being judged.
    """

    def __init__(self, path: Path, *, overrides: dict[str, float] | None = None) -> None:
        self.path = path
        self.overrides = {k.upper(): float(v) for k, v in (overrides or {}).items()}
        self._entries: dict[str, dict] = {}
        if path.exists():
            try:
                self._entries = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("%s unreadable (%s) — starting empty", path, e)

    def observe(self, asin: str, price_sek: float | None, *, is_amazon_seller: bool | None) -> None:
        """Record an Amazon-sold sighting. Ignores everything else."""
        if price_sek is None or is_amazon_seller is not True:
            return
        rounded = round(price_sek, 2)
        entry = self._entries.get(asin)
        if entry is None:
            self._entries[asin] = {
                "sek": rounded,
                "distinct_prices": [rounded],
                "observations": 1,
                "first_seen": _utc_now(),
                "last_updated": _utc_now(),
            }
            return
        entry["observations"] = entry.get("observations", 1) + 1
        entry["last_updated"] = _utc_now()
        # Corroboration needs DISTINCT evidence. Re-reading the same
        # unchanged price on the next run is one observation seen twice,
        # not a second opinion, so repetition must not clear `provisional`.
        distinct = entry.setdefault("distinct_prices", [entry["sek"]])
        if rounded not in distinct and len(distinct) < MAX_DISTINCT_PRICES:
            distinct.append(rounded)
        # Lowest wins: the cheapest Amazon has genuinely sold it for is the
        # best available proxy for its real price.
        if rounded < entry["sek"]:
            entry["sek"] = rounded

    def reference_for(self, asin: str) -> tuple[float | None, bool]:
        """Returns (reference in SEK, is_provisional)."""
        override = self.overrides.get(asin.upper())
        if override is not None:
            return override, False
        entry = self._entries.get(asin)
        if entry is None:
            return None, True
        distinct = entry.get("distinct_prices") or [entry["sek"]]
        return entry["sek"], len(distinct) < CORROBORATION_DISTINCT_PRICES

    def assess(self, asin: str, price_sek: float | None, multiplier: float,
               *, tier: str | None = None, tier_ceiling: float | None = None) -> ScalpVerdict:
        """Judge a price, preferring the strongest evidence available.

        In order: a manual override, then the lowest Amazon-sold price ever
        seen for this exact ASIN, then a tier ceiling derived from the
        product's title (see tiers.py). The tier fallback exists because a
        product whose only sightings are third-party — the scalper case —
        never earns a per-ASIN reference and would otherwise be permanently
        unjudgeable.

        A missing reference NEVER suppresses: silently swallowing a real
        restock is the dangerous failure, while a false positive costs
        nothing but a glance.
        """
        reference, provisional = self.reference_for(asin)
        if price_sek is None:
            return ScalpVerdict(False, None, reference)

        if reference is not None:
            ratio = price_sek / reference
            if ratio <= multiplier:
                return ScalpVerdict(False, None, reference)
            # "~N SEK" not "N SEK": the alert shows the price as the store
            # displays it (EUR on a German page), so an unexplained second
            # currency reads as a bug. The tilde and the word normalised say
            # this is a converted comparison, not a second quoted price.
            note = (
                f"suspected scalp: ~{price_sek:,.0f} SEK normalised, {ratio:.1f}x the "
                f"reference {reference:,.0f} SEK (threshold {multiplier:g}x)"
            )
            if provisional:
                note += " — reference is provisional, based on a single observed price"
            return ScalpVerdict(True, note, reference)

        if tier_ceiling:
            # Deliberately a CEILING, not a median: this is weaker evidence
            # than a real observation, so it is calibrated to avoid crying
            # wolf on a legitimately pricier item in the same tier. It still
            # catches the 4-5x scalps that motivated all this.
            ratio = price_sek / tier_ceiling
            if ratio <= multiplier:
                return ScalpVerdict(False, None, tier_ceiling)
            return ScalpVerdict(
                True,
                f"suspected scalp: ~{price_sek:,.0f} SEK normalised, {ratio:.1f}x the "
                f"typical ceiling for a {tier} ({tier_ceiling:,.0f} SEK) — no "
                f"per-product reference yet, so this is judged on product type alone",
                tier_ceiling,
            )

        return ScalpVerdict(False, "price unverified — no reference price for this product yet", None)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self._entries, indent=2, ensure_ascii=False, sort_keys=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=f".{self.path.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(payload + "\n")
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
