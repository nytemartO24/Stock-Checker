"""Per-marketplace facts: domain, currency, and unavailability phrasing.

These are facts about Amazon rather than user choices, so they live in code
while `sites.yaml` picks WHICH markets to check and which ASINs to watch.

Ported from news-notifier's marketplaces.py, minus the month tables — this
project needs availability, price and seller, not delivery dates.

LANGUAGE: product URLs use Amazon's "/-/en/" override so pages render in
English, and the English phrases below are what actually match. The native
lists are a fallback for markets that ignore the override — `pl` provably
does, serving pl-pl. Don't delete them.
"""

from __future__ import annotations

# Verified against real pages in the news-notifier pilot.
ENGLISH_UNAVAILABLE_SIGNALS = [
    "currently unavailable",
    "we don't know when or if this item will be back in stock",
    "temporarily out of stock",
    "we are working hard to be back in stock",
    "this item cannot be shipped",
]

# "Won't ship to your destination" is a different answer from "sold out",
# but for this project both mean the same thing: you cannot buy it.
NOT_DELIVERABLE_SIGNAL = "cannot be dispatched to your selected delivery location"

_MARKETS = {
    "se": {"domain": "amazon.se", "currency": "SEK", "country": "Sweden", "flag": "🇸🇪",
           "native_unavailable": ["tillfälligt slut i lager", "inte tillgänglig"]},
    "de": {"domain": "amazon.de", "currency": "EUR", "country": "Germany", "flag": "🇩🇪",
           "native_unavailable": ["derzeit nicht verfügbar", "zurzeit nicht auf lager"]},
    "fr": {"domain": "amazon.fr", "currency": "EUR", "country": "France", "flag": "🇫🇷",
           "native_unavailable": ["actuellement indisponible", "temporairement en rupture de stock"]},
    "es": {"domain": "amazon.es", "currency": "EUR", "country": "Spain", "flag": "🇪🇸",
           "native_unavailable": ["no disponible", "temporalmente sin stock"]},
    "nl": {"domain": "amazon.nl", "currency": "EUR", "country": "Netherlands", "flag": "🇳🇱",
           "native_unavailable": ["niet beschikbaar", "tijdelijk niet op voorraad"]},
    "be": {"domain": "amazon.com.be", "currency": "EUR", "country": "Belgium", "flag": "🇧🇪",
           "native_unavailable": ["niet beschikbaar", "actuellement indisponible"]},
    "it": {"domain": "amazon.it", "currency": "EUR", "country": "Italy", "flag": "🇮🇹",
           "native_unavailable": ["non disponibile", "temporaneamente non disponibile"]},
    # Poland ignores the /-/en/ override and serves pl-pl, so its native
    # phrasing is load-bearing rather than a fallback. "obecnie niedostępny"
    # was confirmed against real pages (5 hits in the pilot's live run).
    "pl": {"domain": "amazon.pl", "currency": "PLN", "country": "Poland", "flag": "🇵🇱",
           "native_unavailable": ["obecnie niedostępny", "chwilowo niedostępny"]},
}


def _merge(config: dict) -> dict:
    merged = dict(config)
    merged["unavailable_signals"] = ENGLISH_UNAVAILABLE_SIGNALS + config["native_unavailable"]
    return merged


MARKETS = {code: _merge(config) for code, config in _MARKETS.items()}
