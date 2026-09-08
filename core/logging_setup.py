"""Logging shared by both entrypoints.

Mirrors news-notifier's START/END-with-exit-code convention so a quiet run
is still distinguishable from a run that never happened.

The UTF-8 reconfigure is not cosmetic on Windows: the console defaults to
cp1252, so logging falls back to backslashreplace and an alert renders as
"\\U0001f6d2 ... first run \\u2014 seeded" instead of the message you'd
actually receive. That makes local log-reading actively misleading.
"""

from __future__ import annotations

import logging
import sys


def configure(verbose: bool = False) -> logging.Logger:
    for stream in (sys.stdout, sys.stderr):
        # Absent on a replaced/wrapped stream (pytest's capture, some CI
        # runners); losing pretty output there is harmless.
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8")
            except (ValueError, OSError):
                pass

    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    # httpx logs a line per request at INFO, which drowns our own output
    # once a site has several collections.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    return logging.getLogger("stock_checker")
