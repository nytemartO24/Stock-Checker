#!/usr/bin/env python3
"""Entrypoint: check every enabled site once, then exit.

This is the cron-facing mode. The polling loop for local dev lives in
run_loop.py, which calls run_once() from here so both modes share one code
path — two implementations of "check everything" would drift.

Usage:
    python main.py                    # dry run: logs what it would send
    python main.py --send-discord     # actually notify
    python main.py --site popsplanet  # one site only
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from core.config import SiteConfig, load_config
from core.http import PoliteClient
from core.logging_setup import configure
from core.notifier import DiscordNotifier, format_new_product_alert, format_stock_alert
from core.storage import SiteState
from sites import build_checker

ROOT = Path(__file__).parent
CONFIG_PATH = ROOT / "config" / "sites.yaml"
STATE_DIR = ROOT / "state"

logger = logging.getLogger("stock_checker")


def check_site(config: SiteConfig, state_dir: Path, notifier: DiscordNotifier) -> tuple[int, int]:
    """Check one site. Returns (products_seen, alerts_sent).

    Never raises: a site that blows up is logged and skipped so the rest of
    the run continues.
    """
    state = SiteState(state_dir / f"{config.name}.json")
    seen: set[str] = set()
    alerts = 0
    in_stock = 0

    with PoliteClient(
        min_delay=config.min_delay,
        max_delay=config.max_delay,
        timeout=config.timeout,
        max_retries=config.max_retries,
    ) as client:
        checker = build_checker(config, client, state_dir)
        for result in checker.check():
            seen.add(result.product_id)
            in_stock += result.in_stock
            kind = state.alert_kind(result)
            # A brand-new product bypasses the watchlist on purpose (see
            # SiteState.alert_kind); this is the switch if that gets noisy.
            if kind == "new" and not config.options.get("alert_on_new_products", True):
                kind = None
            if kind:
                render = format_new_product_alert if kind == "new" else format_stock_alert
                sent = notifier.send(render(config.name, result))
                if sent or notifier.dry_run:
                    alerts += 1  # in dry-run, count what a real run would send
                else:
                    # Leave the previous entry untouched so the next pass
                    # re-detects this transition. Recording it here would
                    # mark the restock as already-known and lose the alert
                    # permanently to a transient Discord outage.
                    logger.warning(
                        "[%s] alert for %s not delivered — leaving state so the next run retries",
                        config.name, result.product_id,
                    )
                    continue
            state.record(result)

    if state.is_first_run:
        logger.info("[%s] first run — seeded %d product(s) without notifying", config.name, len(seen))
    if checker.errors:
        # Pruning against an incomplete view deletes products that were
        # merely missed; they then re-alert as new when they reappear.
        logger.warning(
            "[%s] %d error(s) this run — skipping prune to avoid deleting products we simply didn't see",
            config.name, checker.errors,
        )
    else:
        state.prune(seen)
    state.save()
    logger.info("[%s] %d product(s), %d in stock, %d alert(s)", config.name, len(seen), in_stock, alerts)
    return len(seen), alerts


def run_once(only: str | None = None, *, send_discord: bool = False) -> int:
    notifier = DiscordNotifier(dry_run=not send_discord)
    configs = [c for c in load_config(CONFIG_PATH) if c.enabled]
    if only:
        configs = [c for c in configs if c.name == only]
        if not configs:
            logger.error("no enabled site named %r", only)
            return 1

    total_alerts = 0
    failures = 0
    for config in configs:
        try:
            _, alerts = check_site(config, STATE_DIR, notifier)
            total_alerts += alerts
        except Exception:
            failures += 1
            logger.exception("[%s] site check failed", config.name)

    logger.info("run complete: %d alert(s) across %d site(s), %d failure(s)",
                total_alerts, len(configs), failures)
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--send-discord", action="store_true", help="actually post to Discord instead of logging")
    parser.add_argument("--site", help="check only this site")
    parser.add_argument("--verbose", action="store_true", help="debug logging")
    args = parser.parse_args(argv)

    configure(args.verbose)
    logger.info("=== START stock_checker (send_discord=%s) ===", args.send_discord)
    status = run_once(args.site, send_discord=args.send_discord)
    logger.info("=== END stock_checker (exit %d) ===", status)
    return status


if __name__ == "__main__":
    sys.exit(main())
