#!/usr/bin/env python3
"""Show — and optionally send — what alerts would actually look like.

Answers "would this spam me with junk?" without waiting for a real restock.
It checks every enabled site live, treats everything currently in stock as
though it had just come into stock, and reports what you would have been
told.

Writes NO state: it uses a throwaway directory, so running this cannot make
the real run miss a genuine restock later.

    python scripts/test_discord.py              # print only
    python scripts/test_discord.py --send       # actually post to Discord
    python scripts/test_discord.py --send --samples 2
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.config import load_config  # noqa: E402
from core.http import PoliteClient  # noqa: E402
from core.logging_setup import configure  # noqa: E402
from core.notifier import DiscordNotifier, format_stock_alert  # noqa: E402
from sites import build_checker  # noqa: E402

DISCORD_LIMIT = 1800  # leave headroom under the 2000 hard cap
NL_ = chr(10)  # newline inside f-strings, without escapes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--send", action="store_true", help="actually post to Discord")
    parser.add_argument("--samples", type=int, default=3,
                        help="how many full sample alerts to render per site (default 3)")
    parser.add_argument("--site", help="only this site")
    args = parser.parse_args(argv)
    logger = configure(False)

    notifier = DiscordNotifier(dry_run=not args.send)
    configs = [c for c in load_config(ROOT / "config" / "sites.yaml") if c.enabled]
    if args.site:
        configs = [c for c in configs if c.name == args.site]

    throwaway = Path(tempfile.mkdtemp(prefix="stock-checker-test-"))
    summary_lines: list[str] = []
    total = 0

    for config in configs:
        with PoliteClient(min_delay=config.min_delay, max_delay=config.max_delay,
                          timeout=config.timeout, max_retries=config.max_retries) as client:
            checker = build_checker(config, client, throwaway)
            results = list(checker.check())

        # alertable matters as much as in_stock: a watchlist marks every
        # other product non-alertable, so counting raw in-stock products
        # would report a noise level the user will never experience.
        in_stock = [r for r in results if r.in_stock]
        would_alert = [r for r in in_stock if r.alertable]

        total += len(would_alert)
        logger.info("[%s] %d tracked, %d in stock, %d ALERT-ELIGIBLE (watchlist applied)",
                    config.name, len(results), len(in_stock), len(would_alert))
        if not would_alert:
            summary_lines.append(f"{NL_}**{config.name}** — nothing watchlisted is in stock ({len(in_stock)} in stock, none watched)")
            continue
        summary_lines.append(f"{NL_}**{config.name}** — {len(would_alert)} would alert (of {len(in_stock)} in stock):")
        for result in would_alert:
            tag = "  [flagged]" if result.notes else ""
            summary_lines.append(f"  · {result.product_name[:60]}  {result.price_text or ''}{tag}")

        for result in would_alert[: args.samples]:
            logger.info("--- sample alert (%s) ---\n%s", config.name,
                        format_stock_alert(config.name, result))

    header = (f"🧪 **Stock Checker test** — the {total} watchlisted product(s) currently "
              f"in stock, i.e. exactly what would have interrupted you. "
              f"This is the noise level to judge; nothing here is a real restock.")
    body = header + "\n" + "\n".join(summary_lines)
    # One message, truncated rather than split: the point is to gauge volume,
    # and a burst of test messages is itself the thing being complained about.
    if len(body) > DISCORD_LIMIT:
        body = body[:DISCORD_LIMIT] + f"\n… truncated ({total} in stock in total)"

    sent = notifier.send(body)
    logger.info("total in stock across %d site(s): %d", len(configs), total)
    if not args.send:
        logger.info("dry run — pass --send to post this to Discord")
        return 0
    if not sent:
        # Exit non-zero rather than let a silent no-op read as success. This
        # script exists to prove alerting works; an unset webhook made an
        # earlier run report "sent" while delivering nothing.
        logger.error("NOTHING WAS SENT — check DISCORD_WEBHOOK_URL in .env")
        return 1
    logger.info("posted to Discord")
    return 0


if __name__ == "__main__":
    sys.exit(main())
