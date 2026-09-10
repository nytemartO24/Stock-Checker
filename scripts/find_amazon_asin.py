#!/usr/bin/env python3
"""Look up ASINs by product NAME, using the project's own Amazon session.

The gap this fills: `resolve_watchlist.py` resolves every other store from data
we already hold, but Amazon has no catalogue of its own here — this project only
ever visits the ASINs it was told about. news-notifier's discovery output covers
part of it, and only part: `products.txt` accumulates what its Hasbro-brand
newest-first search has happened to surface, so 192 ASINs across four markets
still contained NONE of Suppress Superion, Nether Incendio, Cobalt Drake, Clock
Mirage, Perseus Dark or Whip Brachio.

So those are searched for directly, once, and the ASIN goes in `sites.yaml`.
One ASIN covers se/de/fr/es (the user's own finding), so only one market is
searched — but note that market's TITLE may be a different name entirely:
amazon.se calls Seize Jaguar "Hasbro BEY BBX Browns Canyon" and Sterling Wolf
appears as "Silver Wolf". Matching therefore accepts either the name's words or
its model code.

    python scripts/find_amazon_asin.py "Suppress Superion" "Nether Incendio"
    python scripts/find_amazon_asin.py --market de --code 0-70LP "Suppress Superion"

Uses `sites.amazon.browser.open_market`, so the session is warmed up and served
the English layout — a cookieless fetch gets the market's native layout where
none of this project's selectors match (see CLAUDE.md).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

# Words that appear in nearly every Beyblade title and so cannot identify one.
NOISE = {
    "beyblade", "bbx", "bey", "x", "hasbro", "takara", "tomy", "and", "with",
    "set", "pack", "booster", "starter", "kit", "toys", "toy", "cx", "ux", "bx",
    "infinity", "spinning", "top", "tops", "battle", "battling", "game", "for",
    "ages", "type", "spinner", "launcher", "the",
}

CODE = re.compile(r"\b(\d{1,2}-\d{2}[A-Z]{1,3})\b")

# Search-result tiles, plus each tile's title and price. `data-asin` is on the
# tile itself; an empty or non-10-character value is a placeholder row.
EXTRACT = """() => {
    const out = [];
    for (const el of document.querySelectorAll('[data-asin]')) {
        const asin = el.getAttribute('data-asin');
        if (!asin || asin.length !== 10) continue;
        const h = el.querySelector('h2, [data-cy="title-recipe"], .a-text-normal');
        const price = el.querySelector('.a-price .a-offscreen');
        if (!h) continue;
        out.push({asin: asin, title: h.innerText.trim().slice(0, 130),
                  price: price ? price.innerText.trim() : null});
    }
    return out;
}"""


def tokens(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    return {w for w in words if w not in NOISE and not w.isdigit()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("names", nargs="+", help="product names to look up")
    parser.add_argument("--market", default="se")
    parser.add_argument("--code", action="append", default=[],
                        help="model code(s) to accept as a match, e.g. 0-70LP")
    args = parser.parse_args(argv)

    from playwright.sync_api import sync_playwright

    import sites.amazon.browser as browser
    from sites.amazon.markets import MARKETS

    config = yaml.safe_load((ROOT / "config" / "sites.yaml").read_text(encoding="utf-8"))
    amazon = config["sites"]["amazon"]
    wanted_codes = {c.upper() for c in args.code}
    results: dict[str, list[dict]] = {}

    with sync_playwright() as p:
        handle, page, location, pinned = browser.open_market(
            p, args.market, MARKETS[args.market],
            country=amazon.get("delivery_country", "Sweden"),
            postcode=os.environ.get("DELIVERY_POSTCODE", ""),
            headless=True)
        print(f"session: {args.market} location={location!r} pinned={pinned}\n")
        try:
            for name in args.names:
                url = (f"https://www.{MARKETS[args.market]['domain']}/-/en/s?k="
                       + "+".join(name.split()))
                browser.safe_goto(page, url, args.market)
                page.wait_for_timeout(2500)
                rows = page.evaluate(EXTRACT)
                need = tokens(name)
                hits = []
                for row in rows:
                    title_tokens = tokens(row["title"])
                    title_codes = {c.upper() for c in CODE.findall(row["title"].upper())}
                    if (need and need <= title_tokens) or (wanted_codes & title_codes):
                        hits.append(row)
                results[name] = hits
                print(f"=== {name}: {len(hits)} match(es) of {len(rows)} tiles")
                for row in hits[:4]:
                    print(f"   {row['asin']}  {str(row['price'] or '-'):>10}  "
                          f"{row['title'][:70]}")
                if not hits:
                    # Say what WAS returned: "no match" and "search returned
                    # nothing" are different problems, and only one is ours.
                    for row in rows[:3]:
                        print(f"   (no match) {row['asin']}  {row['title'][:64]}")
        finally:
            handle.close()

    print("\n=== watchlist entries ===")
    for name, hits in results.items():
        if hits:
            print(f"      - {hits[0]['asin']}  # {name}")
        else:
            print(f"      # {name}: NO ASIN FOUND on {args.market}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
