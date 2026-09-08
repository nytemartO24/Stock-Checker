#!/usr/bin/env python3
"""Audit a candidate store and emit the config block to add it.

Adding a retailer should be a paste, not an investigation. This does the
investigating: works out whether the store is Shopify, finds every
collection matching a keyword, shows what is actually IN each one, detects
the currency the requested market resolves to, and prints a
`config/sites.yaml` entry.

It deliberately does NOT pick collections for you, and does not try to
maximise product count. A store groups things by its own logic, not yours:
popsplanet files anime merchandise and launcher accessories under
"beyblade" alongside the actual toys, so chasing coverage would add
products you deliberately excluded. It reports, with sample titles, and you
choose. Pass --collections once you know which ones you want.

    python scripts/audit_store.py https://toysnowman.com --match beyblade
    python scripts/audit_store.py https://www.popsplanet.it/en \\
        --collections beyblade-x-booster,beyblade-x-starter-pack

Read-only: it fetches public JSON and writes nothing.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"}
PAGE = 250
SAMPLES = 3

# Shopify themes expose the active market's currency as a JS global. It is
# the only reliable statement of currency available — products.json gives
# bare numbers with no unit, which is exactly how a SEK price once got
# labelled CAD in this project (see LESSONS.md).
CURRENCY_RE = re.compile(r'Shopify\.currency\s*=\s*\{[^}]*"active"\s*:\s*"([A-Z]{3})"')


def split_base(url: str) -> tuple[str, str]:
    """Return (origin, base_path).

    Handles the locale prefix some stores use (popsplanet serves its English
    catalogue under /en) and tolerates being handed a collection or search
    URL rather than the store root, since that is what you actually have.
    """
    parsed = urlparse(url if "//" in url else "https://" + url)
    origin = f"{parsed.scheme or 'https'}://{parsed.netloc}"
    first = next((p for p in parsed.path.split("/") if p), "")
    base_path = f"/{first}" if len(first) == 2 and first.isalpha() else ""
    return origin, base_path


def suggested_name(origin: str) -> str:
    host = urlparse(origin).netloc.lower()
    name = re.sub(r"[^a-z0-9]+", "-", host).strip("-")
    name = re.sub(r"^www-", "", name)
    name = re.sub(r"-(com|it|se|net|org|co-uk|de|fr|es)$", "", name)
    return name or "store"


def audit(url: str, match: str, country: str, only: str | None = None) -> int:
    origin, base_path = split_base(url)
    base = origin + base_path
    wanted = [h.strip() for h in (only or "").split(",") if h.strip()]

    print(f"store     : {origin}")
    print(f"base path : {base_path or '(none)'}")

    with httpx.Client(headers=UA, timeout=30, follow_redirects=True) as client:
        try:
            response = client.get(f"{base}/collections.json?limit={PAGE}")
            response.raise_for_status()
            collections = response.json()["collections"]
        except Exception as e:
            print(f"\nNOT SHOPIFY, or /collections.json is blocked: {type(e).__name__}: {e}")
            print("This store needs its own site module. See sites/shopify.py for the")
            print("shape and CLAUDE.md's transport rules for choosing JSON vs browser.")
            return 1
        print(f"platform  : Shopify ({len(collections)} collections)")

        currency = None
        try:
            found = CURRENCY_RE.search(client.get(f"{base}/?country={country}").text)
            currency = found.group(1) if found else None
        except Exception:
            pass
        print(f"currency  : {currency or 'UNKNOWN — set by hand and verify'} (at country={country})")

        hits = [c for c in collections
                if match.lower() in (c["handle"] + " " + c.get("title", "")).lower()]
        if wanted:
            hits = [c for c in hits if c["handle"] in wanted]
            missing = set(wanted) - {c["handle"] for c in hits}
            for handle in sorted(missing):
                print(f"!! requested collection not found: {handle}")
        if not hits:
            print(f"\nNo collections matching {match!r}. Try a broader --match.")
            return 1

        print(f"\ncollections matching {match!r}: {len(hits)}")
        print("Sample titles are shown so you can judge relevance yourself — a")
        print("store's own grouping is not necessarily your filter.\n")

        seen: set[str] = set()
        prices: list[float] = []
        for c in sorted(hits, key=lambda c: c["handle"]):
            handle = c["handle"]
            try:
                response = client.get(
                    f"{base}/collections/{handle}/products.json"
                    f"?limit={PAGE}&page=1&country={country}")
                response.raise_for_status()
                products = response.json()["products"]
            except Exception as e:
                print(f"  {handle:<28} FAILED ({type(e).__name__})")
                continue

            handles = {p["handle"] for p in products}
            for p in products:
                for v in p.get("variants") or []:
                    try:
                        prices.append(float(v["price"]))
                    except (TypeError, ValueError, KeyError):
                        pass
            # Overlap is advisory only: a fully-covered collection costs one
            # request per run and yields nothing new (the checker dedupes),
            # but whether you WANT it is a judgement about content, not count.
            overlap = "" if handles - seen else "   [all also in a collection above]"
            print(f"  {handle:<28} {len(handles):>4} products{overlap}")
            for title in sorted(p["title"] for p in products)[:SAMPLES]:
                print(f"       - {title[:66]}")
            seen |= handles

        print(f"\ndistinct products across the collections shown: {len(seen)}")
        if prices:
            print(f"price range: {min(prices):.2f} - {max(prices):.2f} {currency or ''}")

        print("\n--- paste into config/sites.yaml under `sites:` ---\n")
        print(f"  {suggested_name(origin)}:")
        print("    type: shopify")
        print("    enabled: true")
        print(f"    domain: {urlparse(origin).netloc}")
        if base_path:
            print(f"    base_path: {base_path}")
        print(f"    country: {country}")
        print(f"    currency: {currency or 'CHECK_ME'}")
        print("    collections:")
        for handle in (wanted or [c["handle"] for c in sorted(hits, key=lambda c: c["handle"])]):
            print(f"      - {handle}")
        if not wanted:
            print()
            print("  # ^ EVERY match is listed, including ones you may not want.")
            print("  #   Review the samples above, then re-run with")
            print("  #   --collections a,b,c to emit just the ones you keep.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("url", help="store root, or any collection/search URL on it")
    parser.add_argument("--match", default="beyblade",
                        help="only collections whose handle/title contains this")
    parser.add_argument("--country", default="SE", help="market to price in (default SE)")
    parser.add_argument("--collections",
                        help="comma-separated handles to report and emit (default: all matches)")
    args = parser.parse_args(argv)
    return audit(args.url, args.match, args.country, args.collections)


if __name__ == "__main__":
    sys.exit(main())
