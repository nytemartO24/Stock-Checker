"""Text parsing shared by more than one site module.

`parse_price` and `detect_currency` started in sites/amazon/. WooCommerce needs
exactly the same job — "267 kr" is the same problem as "SEK766.25" — and
CLAUDE.md's bar for sharing something is two real sites needing it, so they
moved here rather than being imported across sites or duplicated.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)


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


