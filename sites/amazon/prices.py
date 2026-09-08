"""Price parsing and the reference-price store behind scalp detection.

Pure logic, no browser and no network, so it is fully testable offline —
which matters because this is the part that decides whether you get told
about a restock.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

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


def parse_price(text: str | None) -> float | None:
    """Turn Amazon's displayed price into a number.

    Handles both decimal conventions, because the markets disagree:
    "SEK 766.25", "€68.63", "68,63 €", "1.234,56 €", "1,234.56". The rule
    is that whichever separator appears LAST is the decimal one; a lone
    separator followed by exactly two digits is decimal, otherwise it is a
    thousands grouping ("kr1.234" is 1234, not 1.234).
    """
    if not text:
        return None
    # Keep digits and separators only; currency symbols and codes vary too
    # much to enumerate ("kr", "SEK", "€", "zł", non-breaking spaces).
    cleaned = re.sub(r"[^\d,.]", "", text.replace("\xa0", ""))
    if not cleaned or not any(ch.isdigit() for ch in cleaned):
        return None

    last_comma, last_dot = cleaned.rfind(","), cleaned.rfind(".")
    if last_comma >= 0 and last_dot >= 0:
        decimal_sep = "," if last_comma > last_dot else "."
        thousands_sep = "." if decimal_sep == "," else ","
        cleaned = cleaned.replace(thousands_sep, "").replace(decimal_sep, ".")
    elif last_comma >= 0 or last_dot >= 0:
        sep = "," if last_comma >= 0 else "."
        tail = cleaned.rsplit(sep, 1)[1]
        # Exactly two trailing digits and only one separator -> decimal.
        cleaned = cleaned.replace(sep, "." if len(tail) == 2 and cleaned.count(sep) == 1 else "")

    try:
        value = float(cleaned)
    except ValueError:
        return None
    return value if value > 0 else None


# Order matters: check longer/unambiguous tokens first so "SEK" isn't
# matched by the "kr" rule and "PLN" isn't shadowed by anything.
_CURRENCY_TOKENS = [
    ("SEK", "SEK"), ("EUR", "EUR"), ("PLN", "PLN"), ("USD", "USD"), ("GBP", "GBP"),
    ("kr", "SEK"), ("zł", "PLN"), ("€", "EUR"), ("£", "GBP"), ("$", "USD"),
]


def detect_currency(text: str | None, default: str | None = None) -> str | None:
    """Read the currency OFF the price string rather than assuming it.

    Load-bearing, and not theoretical: with delivery pinned to Sweden,
    amazon.de quotes "SEK766.25", not euros. Trusting the marketplace's
    home currency would convert an already-SEK figure as though it were
    EUR and overstate it 11x — which would flag every cross-border listing
    as a scalp. Same mistake as assuming a Shopify store's currency; see
    LESSONS.md.
    """
    if text:
        lowered = text.lower()
        for token, currency in _CURRENCY_TOKENS:
            if token.lower() in lowered:
                return currency
    return default


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
