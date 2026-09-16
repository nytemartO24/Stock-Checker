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
    "toysnowman": "Shopify handle",
    "gameshop": "WooCommerce slug",
    "ginza": "Ginza numeric id",
    "rarewaves": "EAN",
    "lereservoir": "EAN",
}

AMAZON_CATALOG = Path("/root/news-notifier/pilot/eu_multimarket/state")

# Fallback only. The real list is whatever `markets:` says in sites.yaml, read
# by amazon_markets() below — hard-coding it here is how adding a market to the
# config quietly failed to extend discovery with it.
DEFAULT_AMAZON_MARKETS = ("se", "de", "fr", "es")
SITES_YAML = ROOT / "config" / "sites.yaml"


def amazon_markets(path: Path = SITES_YAML) -> tuple[str, ...]:
    """The markets sites.yaml actually checks."""
    try:
        import yaml
        config = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        markets = config.get("sites", {}).get("amazon", {}).get("markets")
        if markets:
            return tuple(str(m).strip() for m in markets if str(m).strip())
    except Exception as e:  # a missing PyYAML must not break the resolver
        print(f"  (could not read markets from {path}: {e})", file=sys.stderr)
    return DEFAULT_AMAZON_MARKETS

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
#
# "and"/"och" are NOT in here, deliberately. Amazon's marketing titles say
# "...Top and Launcher, ... Battle Tops and Games..." — two joins in a single
# product's title — so counting them classified EVERY Amazon listing as a
# bundle. That silently removed Amazon from the dashboard's cross-store price
# comparison, which is the one place a 4x scalp is visible. Only "&", "vs" and
# "+" actually join two products in these catalogues.
BUNDLE_JOIN = re.compile(r"\s(?:&|vs\.?)\s|\+", re.I)


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
    # ONE "&" or "vs" is enough now that "and" is gone: those genuinely join
    # two products ("Circle Ghost 4-60LR & Hack Viking 4-55O", "Grogu 3-60F
    # vs. Snowtrooper 3-80N"), whereas the old threshold of two let a
    # two-item collab pack pass as a single.
    return hits > 1 or len(BUNDLE_JOIN.findall(title or "")) >= 1


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


def load_amazon_catalogue(base: Path, markets: tuple[str, ...] | None = None
                          ) -> dict[str, dict[str, str]]:
    """{market: {asin: title}} from news-notifier's discovery output.

    Its format is `ASIN  # Title`, and a fully-commented line means tracking was
    paused rather than the product removed — so a line with no ASIN is skipped,
    not treated as a title.

    Per-market because the TITLES differ per market and that is worth showing
    (amazon.es translates "Nether Incendio" to "Nether Fire"). It is NOT how the
    watchlist is decided: see merge_amazon_catalogue.
    """
    out: dict[str, dict[str, str]] = {}
    for market in (markets or amazon_markets()):
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


def merge_amazon_catalogue(per_market: dict[str, dict[str, str]]
                           ) -> tuple[dict[str, str], dict[str, list[str]]]:
    """Fold the markets into ONE catalogue: {asin: title}, {asin: [markets]}.

    AN ASIN IS AN ASIN. The same ten characters is the same physical product on
    every Amazon domain, so which market's crawler happened to surface it says
    nothing about where it can be bought — and the checker visits every market
    for every watchlisted ASIN regardless. Matching per market instead made a
    product discovered only on .de look like partial coverage, and reported it
    as four separate misses on a wanted product we were in fact watching
    everywhere.

    Every title is still searched, because they differ by market: amazon.se
    calls Seize Jaguar "BEY BBX Browns Canyon" and amazon.es translates Nether
    Incendio to "Nether Fire", so a name that matches nowhere else may match
    there. The LONGEST title wins as the one displayed, being the most
    descriptive, but a match against any of them is a match.
    """
    titles: dict[str, str] = {}
    seen_on: dict[str, list[str]] = {}
    for market, catalogue in per_market.items():
        for asin, title in catalogue.items():
            seen_on.setdefault(asin, []).append(market)
            if len(title or "") > len(titles.get(asin, "")):
                titles[asin] = title
    return titles, {asin: sorted(ms) for asin, ms in seen_on.items()}


def amazon_alias_titles(per_market: dict[str, dict[str, str]]) -> dict[str, str]:
    """{"<asin>#<market>": title} — every market's name for every ASIN.

    Fed to `match` as a catalogue so a wanted name is tested against ALL of an
    ASIN's names, then collapsed back to the bare ASIN by the caller.
    """
    return {f"{asin}#{market}": title
            for market, catalogue in per_market.items()
            for asin, title in catalogue.items()}


# A Beyblade X model code — 4-50UF, 9-65B, 3-80FB, 0-70LP. Printed on the
# product itself and IDENTICAL in every language at every retailer, which makes
# it the one cross-catalogue key that needs no barcode.
CODE = re.compile(r"\b(\d{1,2}-\d{2}[A-Z]{0,3})\b")


def codes(title: str) -> set[str]:
    return set(CODE.findall((title or "").upper()))


def match(catalogue: dict[str, str], name: str, wanted_names: list[str],
          extra_codes: set[str] | None = None
          ) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """(singles, bundles) matching `name` by words OR by model code.

    THE CODE PASS IS NOT REDUNDANT. Amazon lists Sterling Wolf as "Silver Wolf",
    so a word match finds it at three stores and misses it on all four Amazon
    markets — a silent gap in exactly the place the user most wants covered. The
    code (3-80FB) is on the product and does not vary, so once any store reveals
    it, every other catalogue can be searched by it regardless of what that
    retailer decided to call the thing.
    """
    need = tokens(name)
    singles, bundles = [], []
    for identifier, title in catalogue.items():
        by_words = bool(need) and need <= tokens(title)
        by_code = bool(extra_codes) and bool(extra_codes & codes(title))
        if by_words or by_code:
            (bundles if looks_like_bundle(title, wanted_names) else singles).append(
                (identifier, " ".join((title or "").split())))
    return sorted(singles), sorted(bundles)


def learn_codes(catalogues: dict[str, dict[str, str]], name: str,
                wanted_names: list[str]) -> set[str]:
    """Model codes for `name`, learned from any catalogue that names it plainly.

    Taken from SINGLES only: a bundle title carries four codes and three belong
    to other products, so learning from one would drag unrelated items into
    every subsequent match.
    """
    found: set[str] = set()
    for catalogue in catalogues.values():
        singles, _ = match(catalogue, name, wanted_names)
        for _, title in singles:
            found |= codes(title)
    return found


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
    markets = amazon_markets()
    catalogues = {site: load_site_catalogue(state_dir, site) for site in SITE_IDENTIFIERS}
    per_market = load_amazon_catalogue(Path(args.amazon_catalog), markets)
    # ONE Amazon catalogue, not one per market — see merge_amazon_catalogue.
    amazon, seen_on = merge_amazon_catalogue(per_market)
    aliases = amazon_alias_titles(per_market)

    for site, cat in catalogues.items():
        print(f"  {site:<12} {len(cat):>4} products in state  ({SITE_IDENTIFIERS[site]})")
    print(f"  {'amazon':<12} {len(amazon):>4} ASINs known    (discovered on "
          f"{', '.join(per_market) or 'no markets'}; each is checked on ALL of "
          f"{', '.join(markets)})")

    per_site: dict[str, dict[str, list]] = {s: {"single": [], "bundle": []} for s in catalogues}
    amazon_hits: dict[str, list] = {"single": [], "bundle": []}
    misses: dict[str, list[str]] = {}

    # ALIASES, not the merged catalogue: a model code has to be learnable from
    # whichever market happens to print it. Feeding only the longest title per
    # ASIN would lose 3-80FB whenever the market that spells it out is not the
    # market with the wordiest marketing copy.
    all_catalogues = {**catalogues, "amazon": aliases}
    for name in wanted:
        print(f"\n=== {name} ===")
        learned = learn_codes(all_catalogues, name, wanted)
        if learned:
            print(f"   (model code: {', '.join(sorted(learned))})")
        found_anywhere = False
        for site, cat in catalogues.items():
            singles, bundles = match(cat, name, wanted, learned)
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
        # Matched against EVERY market's title for the ASIN, then collapsed back
        # to the bare ASIN: a hit under any market's name is a hit, everywhere.
        alias_singles, alias_bundles = match(aliases, name, wanted, learned)
        found: dict[str, tuple[str, bool]] = {}
        for key, title in alias_singles:
            found[key.split("#", 1)[0]] = (title, False)
        for key, title in alias_bundles:
            asin = key.split("#", 1)[0]
            # A single beats a bundle: if any market lists it as the product on
            # its own, that is what it is.
            found.setdefault(asin, (title, True))
        for asin, (title, is_bundle) in sorted(found.items()):
            where = ",".join(seen_on.get(asin, []))
            print(f"   {'amazon':<12} {'[BUNDLE] ' if is_bundle else ''}{asin}"
                  f"   (discovered on {where})")
            print(f"                {title[:74]}")
            amazon_hits["bundle" if is_bundle else "single"].append((asin, name, title))
            found_anywhere = True
        if not found:
            misses.setdefault(name, []).append("amazon")
        if not found_anywhere:
            print("   NOT FOUND ANYWHERE — not stocked by any tracked store, or "
                  "listed under a name that shares no words with this one")

    print("\n\n=== coverage ===")
    for name in wanted:
        absent = misses.get(name, [])
        # Amazon counts ONCE. It is one catalogue reached through several
        # domains, not several catalogues.
        stores = len(catalogues) + 1
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
        for asin, name, _ in amazon_hits["single"] + amazon_hits["bundle"]:
            asins.setdefault(asin, name)
        if asins:
            print(f"\n  # amazon (one ASIN covers {'/'.join(markets)})")
            for asin, name in sorted(asins.items(), key=lambda kv: kv[1]):
                print(f"      - {asin}  # {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
