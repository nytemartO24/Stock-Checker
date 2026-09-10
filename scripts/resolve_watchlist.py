#!/usr/bin/env python3
"""Turn a list of product NAMES into per-store watchlist entries.

The problem this solves: the user knows what they want by name ("Shark Scale"),
but no store's watchlist takes a name. Each keys products its own way — Shopify
handle, WooCommerce slug, Ginza numeric id, EAN, ASIN — and CLAUDE.md records
why title matching was dropped for the watchlists themselves: names are not
word-order stable and a term also matches bundles merely containing the product.

So names are matched ONCE, here, and the exact identifiers are what get written
into `sites.yaml`. Re-run it whenever the wanted list or a catalogue changes.

It reads what the project already knows and makes NO new requests: every tracked
site's `state/<site>.json` already maps identifier -> title for its whole
catalogue, and news-notifier's `state/<market>/products.txt` maps ASIN -> title
for every product its catalogue scraper has ever seen per market. That is the
entire lookup, for free.

    python scripts/resolve_watchlist.py
    python scripts/resolve_watchlist.py --wanted config/wanted_products.txt --yaml

Bundles are reported SEPARATELY rather than mixed in, because they are a real
but different answer: at toysnowman, Tread Croc existed only inside a four-item
bundle, and one went by unnoticed — so a bundle containing a wanted product is
worth watching, while being told "found it" when the only hit is a 600 kr
four-pack would be misleading.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Sites whose state files carry their whole tracked catalogue, and what their
# watchlist entries actually are. Amazon is absent on purpose: its state holds
# only the ASINs already watched, never a catalogue, so it is resolved from
# news-notifier's discovery output instead (see AMAZON_CATALOG).
SITE_IDENTIFIERS = {
    "popsplanet": "Shopify handle",
    "toysnowman": "Shopify handle",
    "gameshop": "WooCommerce slug",
    "ginza": "Ginza numeric id",
    "rarewaves": "EAN",
    "lereservoir": "EAN",
}

AMAZON_CATALOG = Path("/root/news-notifier/pilot/eu_multimarket/state")
AMAZON_MARKETS = ("se", "de", "fr", "es")

# Words that carry no identity: they appear in most titles and would make a
# one-word wanted name match everything.
NOISE = {
    "beyblade", "bbx", "bey", "x", "hasbro", "takara", "tomy", "the", "and",
    "with", "set", "pack", "booster", "starter", "kit", "toys", "toy", "cx",
    "ux", "bx", "infinity", "spinning", "top", "tops", "battle", "battling",
    "game", "games", "for", "ages", "type", "spinner", "launcher", "de", "och",
}

# A title joining several products. Same signal tiers.py uses for pricing, and
# it is why a "match" can be a 600 kr four-pack rather than the single.
BUNDLE_JOIN = re.compile(r"\s(?:&|e|vs\.?|and|och)\s|\+", re.I)


def tokens(text: str) -> set[str]:
    """Identity-bearing words of a title, lowercased."""
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    return {w for w in words if w not in NOISE and not w.isdigit()}


def looks_like_bundle(title: str, wanted_names: list[str]) -> bool:
    """True when the title names more than one product.

    Counting how many DIFFERENT wanted products a title mentions is the
    reliable half; the join pattern catches bundles of things we do not track.
    """
    hits = sum(1 for name in wanted_names if tokens(name) <= tokens(title))
    return hits > 1 or len(BUNDLE_JOIN.findall(title or "")) >= 2


def load_site_catalogue(state_dir: Path, site: str) -> dict[str, str]:
    path = state_dir / f"{site}.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    entries = data.get("products", data) if isinstance(data, dict) else {}
    return {pid: (row.get("name") or "") for pid, row in entries.items()
            if isinstance(row, dict)}


def load_amazon_catalogue(base: Path) -> dict[str, dict[str, str]]:
    """{market: {asin: title}} from news-notifier's discovery output.

    Its format is `ASIN  # Title`, and a fully-commented line means tracking was
    paused rather than the product removed — so a line with no ASIN is skipped,
    not treated as a title.
    """
    out: dict[str, dict[str, str]] = {}
    for market in AMAZON_MARKETS:
        path = base / market / "products.txt"
        if not path.exists():
            continue
        found: dict[str, str] = {}
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            asin, _, comment = line.partition("#")
            asin = asin.strip()
            if asin:
                found[asin] = comment.strip()
        out[market] = found
    return out


def match(catalogue: dict[str, str], name: str, wanted_names: list[str]
          ) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """(singles, bundles) whose titles contain every identity word of `name`."""
    need = tokens(name)
    singles, bundles = [], []
    for identifier, title in catalogue.items():
        if need and need <= tokens(title):
            (bundles if looks_like_bundle(title, wanted_names) else singles).append(
                (identifier, " ".join((title or "").split())))
    return sorted(singles), sorted(bundles)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--wanted", default=str(ROOT / "config" / "wanted_products.txt"))
    parser.add_argument("--state", default=str(ROOT / "state"))
    parser.add_argument("--amazon-catalog", default=str(AMAZON_CATALOG))
    parser.add_argument("--yaml", action="store_true",
                        help="also print watchlist blocks ready for sites.yaml")
    args = parser.parse_args(argv)

    wanted = [ln.split("#")[0].strip() for ln in
              Path(args.wanted).read_text(encoding="utf-8").splitlines()]
    wanted = [w for w in wanted if w]
    print(f"wanted products: {len(wanted)}\n")

    state_dir = Path(args.state)
    catalogues = {site: load_site_catalogue(state_dir, site) for site in SITE_IDENTIFIERS}
    amazon = load_amazon_catalogue(Path(args.amazon_catalog))
    for site, cat in catalogues.items():
        print(f"  {site:<12} {len(cat):>4} products in state  ({SITE_IDENTIFIERS[site]})")
    for market, cat in amazon.items():
        print(f"  amazon/{market:<5} {len(cat):>4} ASINs known    (from news-notifier discovery)")

    per_site: dict[str, dict[str, list]] = {s: {"single": [], "bundle": []} for s in catalogues}
    per_market: dict[str, dict[str, list]] = {m: {"single": [], "bundle": []} for m in amazon}
    misses: dict[str, list[str]] = {}

    for name in wanted:
        print(f"\n=== {name} ===")
        found_anywhere = False
        for site, cat in catalogues.items():
            singles, bundles = match(cat, name, wanted)
            for identifier, title in singles:
                print(f"   {site:<12} {identifier}")
                print(f"                {title[:74]}")
                per_site[site]["single"].append((identifier, name, title))
                found_anywhere = True
            for identifier, title in bundles:
                print(f"   {site:<12} [BUNDLE] {identifier[:60]}")
                print(f"                {title[:74]}")
                per_site[site]["bundle"].append((identifier, name, title))
                found_anywhere = True
            if not singles and not bundles:
                misses.setdefault(name, []).append(site)
        for market, cat in amazon.items():
            singles, bundles = match(cat, name, wanted)
            for asin, title in singles:
                print(f"   amazon/{market:<5} {asin}")
                print(f"                {title[:74]}")
                per_market[market]["single"].append((asin, name, title))
                found_anywhere = True
            for asin, title in bundles:
                print(f"   amazon/{market:<5} [BUNDLE] {asin}")
                per_market[market]["bundle"].append((asin, name, title))
                found_anywhere = True
            if not singles and not bundles:
                misses.setdefault(name, []).append(f"amazon/{market}")
        if not found_anywhere:
            print("   NOT FOUND ANYWHERE — not stocked by any tracked store, or "
                  "listed under a name that shares no words with this one")

    print("\n\n=== coverage ===")
    for name in wanted:
        absent = misses.get(name, [])
        stores = len(catalogues) + len(amazon)
        print(f"  {name:<20} found in {stores - len(absent)}/{stores} catalogues")

    if args.yaml:
        print("\n\n=== watchlist blocks for config/sites.yaml ===")
        for site in catalogues:
            rows = per_site[site]["single"] + per_site[site]["bundle"]
            if not rows:
                continue
            print(f"\n  # {site} ({SITE_IDENTIFIERS[site]})")
            seen = set()
            for identifier, name, title in rows:
                if identifier in seen:
                    continue
                seen.add(identifier)
                tag = " [bundle]" if (identifier, name, title) in per_site[site]["bundle"] else ""
                quote = '"' if identifier.isdigit() else ""
                print(f"      - {quote}{identifier}{quote}  # {name}{tag}")
        asins: dict[str, str] = {}
        for market in per_market:
            for asin, name, _ in per_market[market]["single"] + per_market[market]["bundle"]:
                asins.setdefault(asin, name)
        if asins:
            print("\n  # amazon (one ASIN covers se/de/fr/es)")
            for asin, name in sorted(asins.items(), key=lambda kv: kv[1]):
                print(f"      - {asin}  # {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
