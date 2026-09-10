#!/usr/bin/env python3
"""Work out what Ginza's codenamed products actually ARE, via their barcodes.

Ginza lists Beyblades under Hasbro's US-national-park pre-release codenames —
"BEYBLADE Bbx Kobuk Valley" is Scale Shark 4-50UF — so no name-based match can
ever resolve its catalogue, and `resolve_watchlist.py` correctly finds nothing
there. This is the bridge CLAUDE.md has wanted since the naming problem was
first written down, and it is cheap now that two pieces exist:

  * a Ginza PRODUCT PAGE carries the EAN (the search API does not);
  * `state/rarewaves.json` is already keyed BY EAN with the real Hasbro name,
    because that store publishes barcodes as its product ids.

So: one page fetch per Ginza product, pull the barcode, look it up in rarewaves.
No guessing, no name similarity — the barcode either matches or it does not.

    python scripts/resolve_ginza.py            # resolve everything in state
    python scripts/resolve_ginza.py 952144     # just these ids

Writes nothing; prints a mapping to paste, and says plainly which products it
could not resolve rather than implying the map is complete.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.http import PoliteClient  # noqa: E402

BASE = "https://www.ginza.se"

# A 13-digit EAN, wherever the page carries it. Product pages expose it in
# several places (a spec table, JSON-LD, a meta tag) and which one varies, so
# this looks for the number itself and requires the 501099 Hasbro prefix or a
# plausible GTIN shape rather than trusting one selector.
EAN_ANY = re.compile(r"\b(\d{13})\b")


def load_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data.get("products", data) if isinstance(data, dict) else {}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("ids", nargs="*", help="Ginza product ids (default: all in state)")
    parser.add_argument("--state", default=str(ROOT / "state"))
    args = parser.parse_args(argv)

    state = Path(args.state)
    ginza = load_json(state / "ginza.json")
    rarewaves = load_json(state / "rarewaves.json")
    # rarewaves keys ARE barcodes; that is the whole reason this works.
    by_ean = {ean: (row.get("name") or "") for ean, row in rarewaves.items()
              if isinstance(row, dict)}
    print(f"ginza products in state: {len(ginza)}")
    print(f"rarewaves EAN -> name entries: {len(by_ean)}\n")

    ids = args.ids or sorted(ginza)
    resolved, unresolved = [], []
    with PoliteClient(min_delay=1.5, max_delay=3.5, timeout=30) as client:
        for pid in ids:
            title = " ".join((ginza.get(pid, {}).get("name") or "").split())
            url = f"{BASE}/product/{pid}/"
            try:
                body = client.get(url).text
            except Exception as e:
                unresolved.append((pid, title, f"fetch failed: {type(e).__name__}"))
                print(f"  {pid:<9} FETCH FAILED {type(e).__name__}  {title[:44]}")
                continue

            # Prefer a barcode rarewaves knows; fall back to reporting whatever
            # 13-digit codes the page had, so a miss is diagnosable.
            candidates = [e for e in dict.fromkeys(EAN_ANY.findall(body))]
            known = [e for e in candidates if e in by_ean]
            if known:
                ean = known[0]
                real = by_ean[ean]
                resolved.append((pid, title, ean, real))
                print(f"  {pid:<9} {ean}  {title[:32]:<34} = {real[:44]}")
            else:
                hasbro = [e for e in candidates if e.startswith("501099")]
                note = (f"barcode {hasbro[0]} not in rarewaves" if hasbro
                        else f"no EAN on page ({len(candidates)} 13-digit numbers)")
                unresolved.append((pid, title, note))
                print(f"  {pid:<9} UNRESOLVED  {title[:32]:<34} {note}")

    print(f"\nresolved {len(resolved)}/{len(ids)}")
    if resolved:
        print("\n=== ginza id -> real product (paste into sites.yaml comments) ===")
        for pid, title, ean, real in resolved:
            print(f"      - \"{pid}\"  # {real[:58]}")
            print(f"                 # ginza calls it: {title[:56]} (EAN {ean})")
    if unresolved:
        print("\n=== unresolved — these need another route ===")
        for pid, title, note in unresolved:
            print(f"  {pid:<9} {title[:44]:<46} {note}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
