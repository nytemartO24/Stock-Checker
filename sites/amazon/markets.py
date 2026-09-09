"""Per-marketplace facts: domain, currency, unavailability phrasing, months.

These are facts about Amazon rather than user choices, so they live in code
while `sites.yaml` picks WHICH markets to check and which ASINs to watch.

Ported from news-notifier's marketplaces.py, including the month tables —
they were dropped when this module was first scoped to availability only, and
came back when it turned out a long delivery estimate is itself a form of
being unavailable.

LANGUAGE — the thing that used to be wrong
------------------------------------------
Product URLs use Amazon's "/-/en/" override, so pages render in ENGLISH.
Every market except `se` once declared only NATIVE month names ("januar",
"février"), so on an English page nothing could match: the date search
failed, the signal search failed, everything came back UNKNOWN. `se` was the
only market whose table happened to contain English names, which is exactly
why `se` was the only market that worked.

The fix is to stop guessing which language wins: every market gets the union
of the English and its native tables. If the override works, English matches;
if Amazon ignores it for some session, the native entries do. `pl` proves the
native side still matters — Poland ignores the override and serves pl-pl.
"""

from __future__ import annotations

# Amazon abbreviates months in delivery promises fairly often ("Wednesday,
# 6 Aug"), so abbreviations are first-class entries, not a nicety.
ENGLISH_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7,
    "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}

# Verified against real pages in the news-notifier pilot.
ENGLISH_UNAVAILABLE_SIGNALS = [
    "currently unavailable",
    "we don't know when or if this item will be back in stock",
    "temporarily out of stock",
    "we are working hard to be back in stock",
    "this item cannot be shipped",
]

# NOT ported from news-notifier: its no_date_signals ("release date has not
# been announced", "coming soon"). It needed them to tell NO DATE YET apart
# from UNKNOWN in its outcome taxonomy. Here a product with no delivery block
# simply has delivery_date=None, which says the same thing — so carrying the
# list would be config that looks live and is never read. Its NO OFFER case
# ("See All Buying Options" with no add-to-cart) is likewise already covered:
# no buyable button means in_stock=False.

NOT_DELIVERABLE_SIGNAL = "cannot be dispatched to your selected delivery location"

_MARKETS = {
    "se": {"domain": "amazon.se", "currency": "SEK", "country": "Sweden", "flag": "🇸🇪",
           "native_unavailable": ["tillfälligt slut i lager", "inte tillgänglig"],
           "native_months": {"januari": 1, "februari": 2, "mars": 3, "april": 4, "maj": 5,
                             "juni": 6, "juli": 7, "augusti": 8, "september": 9,
                             "oktober": 10, "november": 11, "december": 12}},
    "de": {"domain": "amazon.de", "currency": "EUR", "country": "Germany", "flag": "🇩🇪",
           "native_unavailable": ["derzeit nicht verfügbar", "zurzeit nicht auf lager"],
           "native_months": {"januar": 1, "februar": 2, "märz": 3, "april": 4, "mai": 5,
                             "juni": 6, "juli": 7, "august": 8, "september": 9,
                             "oktober": 10, "november": 11, "dezember": 12}},
    "fr": {"domain": "amazon.fr", "currency": "EUR", "country": "France", "flag": "🇫🇷",
           "native_unavailable": ["actuellement indisponible", "temporairement en rupture de stock"],
           "native_months": {"janvier": 1, "février": 2, "mars": 3, "avril": 4, "mai": 5,
                             "juin": 6, "juillet": 7, "août": 8, "septembre": 9,
                             "octobre": 10, "novembre": 11, "décembre": 12}},
    "es": {"domain": "amazon.es", "currency": "EUR", "country": "Spain", "flag": "🇪🇸",
           "native_unavailable": ["no disponible", "temporalmente sin stock"],
           "native_months": {"enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5,
                             "junio": 6, "julio": 7, "agosto": 8, "septiembre": 9,
                             "setiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12}},
    "nl": {"domain": "amazon.nl", "currency": "EUR", "country": "Netherlands", "flag": "🇳🇱",
           "native_unavailable": ["niet beschikbaar", "tijdelijk niet op voorraad"],
           "native_months": {"januari": 1, "februari": 2, "maart": 3, "april": 4, "mei": 5,
                             "juni": 6, "juli": 7, "augustus": 8, "september": 9,
                             "oktober": 10, "november": 11, "december": 12}},
    "be": {"domain": "amazon.com.be", "currency": "EUR", "country": "Belgium", "flag": "🇧🇪",
           "native_unavailable": ["niet beschikbaar", "actuellement indisponible"],
           # Belgium is bilingual, so both Dutch and French names are in play.
           "native_months": {"januari": 1, "februari": 2, "maart": 3, "mei": 5, "juni": 6,
                             "juli": 7, "augustus": 8, "oktober": 10,
                             "janvier": 1, "février": 2, "mars": 3, "avril": 4, "juin": 6,
                             "juillet": 7, "août": 8, "septembre": 9, "octobre": 10,
                             "novembre": 11, "décembre": 12, "september": 9,
                             "november": 11, "december": 12}},
    "it": {"domain": "amazon.it", "currency": "EUR", "country": "Italy", "flag": "🇮🇹",
           "native_unavailable": ["non disponibile", "temporaneamente non disponibile"],
           "native_months": {"gennaio": 1, "febbraio": 2, "marzo": 3, "aprile": 4,
                             "maggio": 5, "giugno": 6, "luglio": 7, "agosto": 8,
                             "settembre": 9, "ottobre": 10, "novembre": 11, "dicembre": 12}},
    # Poland ignores the /-/en/ override and serves pl-pl, so its native
    # phrasing is load-bearing rather than a fallback. Polish dates decline
    # the month into the genitive — "12 sierpnia", never "12 sierpień" — so
    # the nominative forms alone could not match a single real date. Both are
    # listed. "obecnie niedostępny" was confirmed against real pages.
    "pl": {"domain": "amazon.pl", "currency": "PLN", "country": "Poland", "flag": "🇵🇱",
           "native_unavailable": ["obecnie niedostępny", "chwilowo niedostępny"],
           "native_months": {"stycznia": 1, "lutego": 2, "marca": 3, "kwietnia": 4,
                             "maja": 5, "czerwca": 6, "lipca": 7, "sierpnia": 8,
                             "września": 9, "października": 10, "listopada": 11,
                             "grudnia": 12,
                             "styczeń": 1, "luty": 2, "marzec": 3, "kwiecień": 4,
                             "czerwiec": 6, "lipiec": 7, "sierpień": 8, "wrzesień": 9,
                             "październik": 10, "listopad": 11, "grudzień": 12}},
}


def _merge(config: dict) -> dict:
    """Fold the shared English tables together with a market's native ones.

    English wins on key collisions, but every collision that exists today is
    between two spellings of the same month (Dutch/French "mars" is 3 either
    way), so the merge order is not load-bearing.
    """
    merged = dict(config)
    merged["months"] = {**config["native_months"], **ENGLISH_MONTHS}
    merged["unavailable_signals"] = ENGLISH_UNAVAILABLE_SIGNALS + config["native_unavailable"]
    return merged


MARKETS = {code: _merge(config) for code, config in _MARKETS.items()}
