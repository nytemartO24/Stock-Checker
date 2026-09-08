#!/usr/bin/env python3
"""One-off migration: seed Amazon reference prices from news-notifier.

The pilot recorded `price`, `seller_text` and `is_amazon_seller` per ASIN
per market. Every Amazon-sold observation in there is a free reference
price, which skips the cold-start window where a product has no baseline
and its price can only be reported as unverified.

Only Amazon-sold entries contribute, so a third-party price can never
become a baseline — the scalped listing that motivated all this has only
third-party observations and is correctly ignored.

Not part of the runtime: run it once, by hand.

    python scripts/seed_reference_prices.py [--pilot-state DIR] [--apply]

Defaults to a dry run that prints what it would record.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sites.amazon.markets import MARKETS  # noqa: E402
from sites.amazon.prices import ReferencePrices, detect_currency, parse_price, to_sek  # noqa: E402

DEFAULT_PILOT_STATE = Path(
    r"C:\AI\Website Stock Checker\server-backup\news-notifier\pilot\eu_multimarket\state"
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--pilot-state", type=Path, default=DEFAULT_PILOT_STATE,
                        help="news-notifier pilot state dir (contains <market>/delivery_state.json)")
    parser.add_argument("--out", type=Path, default=ROOT / "state" / "amazon_reference_prices.json")
    parser.add_argument("--apply", action="store_true", help="write the file (default: dry run)")
    args = parser.parse_args(argv)

    if not args.pilot_state.exists():
        print(f"pilot state not found: {args.pilot_state}")
        return 1

    references = ReferencePrices(args.out)
    considered = recorded = skipped_third_party = skipped_no_price = 0

    for market, config in MARKETS.items():
        state_file = args.pilot_state / market / "delivery_state.json"
        if not state_file.exists():
            continue
        entries = json.loads(state_file.read_text(encoding="utf-8"))
        for asin, entry in entries.items():
            considered += 1
            if entry.get("is_amazon_seller") is not True:
                skipped_third_party += 1
                continue
            price_text = entry.get("price") or ""
            value = parse_price(price_text)
            # The pilot stored the displayed string, so the currency is read
            # off it exactly as at runtime rather than assumed per market.
            price_sek = to_sek(value, detect_currency(price_text, config["currency"]))
            if price_sek is None:
                skipped_no_price += 1
                continue
            references.observe(asin, price_sek, is_amazon_seller=True)
            recorded += 1
            print(f"  {market} {asin:<12} {price_text:>12} -> {price_sek:8.2f} SEK")

    print(f"\nconsidered {considered}, recorded {recorded}, "
          f"skipped {skipped_third_party} third-party / {skipped_no_price} priceless")
    seeded = {asin: references.reference_for(asin) for asin in sorted(references._entries)}
    print(f"\n{len(seeded)} ASIN(s) now have a reference:")
    for asin, (value, provisional) in seeded.items():
        print(f"  {asin:<12} {value:8.2f} SEK{'  (provisional)' if provisional else ''}")

    if args.apply:
        references.save()
        print(f"\nwrote {args.out}")
    else:
        print("\ndry run — pass --apply to write")
    return 0


if __name__ == "__main__":
    sys.exit(main())
