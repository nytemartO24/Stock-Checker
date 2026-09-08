"""Polling loop for local dev mode.

Production runs single-shot under cron (see main.py), which owns the timing
there. This exists so you can leave something running locally without
installing a cron entry.

The interval is measured from the END of one pass to the start of the next.
Measuring from the start would let a slow pass overlap the next one, which
is the same pile-up news-notifier needed flock to prevent.
"""

from __future__ import annotations

import logging
import time
from typing import Callable

logger = logging.getLogger(__name__)


def poll(run: Callable[[], int], interval_seconds: float, *, max_passes: int | None = None) -> int:
    """Call `run` forever (or `max_passes` times), sleeping between passes.

    `max_passes` exists so tests can drive this without a timeout hack.
    Returns the last pass's exit status.
    """
    status = 0
    passes = 0
    while max_passes is None or passes < max_passes:
        started = time.monotonic()
        try:
            status = run()
        except KeyboardInterrupt:
            logger.info("interrupted — stopping")
            return status
        except Exception:
            # A crashing pass must not kill the loop; the next one may well
            # succeed (transient network, a site briefly erroring).
            logger.exception("pass failed")
            status = 1
        passes += 1
        if max_passes is not None and passes >= max_passes:
            break
        elapsed = time.monotonic() - started
        logger.info("pass took %.1fs — sleeping %.0fs", elapsed, interval_seconds)
        try:
            time.sleep(interval_seconds)
        except KeyboardInterrupt:
            logger.info("interrupted — stopping")
            return status
    return status
