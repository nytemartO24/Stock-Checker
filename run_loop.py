#!/usr/bin/env python3
"""Entrypoint: poll every enabled site on an interval (local dev mode).

Production uses main.py under cron instead — cron owns the timing there, so
there is no scheduler to configure or keep alive.

Usage:
    python run_loop.py --interval 900
    python run_loop.py --interval 900 --send-discord
"""

from __future__ import annotations

import argparse
import logging
import sys

from core.logging_setup import configure
from core.scheduler import poll
from main import run_once

logger = logging.getLogger("stock_checker")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--interval", type=float, default=900.0, help="seconds between passes (default 900)")
    parser.add_argument("--send-discord", action="store_true", help="actually post to Discord")
    parser.add_argument("--site", help="check only this site")
    parser.add_argument("--verbose", action="store_true", help="debug logging")
    args = parser.parse_args(argv)

    configure(args.verbose)
    logger.info("=== START stock_checker loop (interval=%ss) ===", args.interval)
    return poll(lambda: run_once(args.site, send_discord=args.send_discord), args.interval)


if __name__ == "__main__":
    sys.exit(main())
