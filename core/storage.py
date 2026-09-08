"""Last-known state per product, and the new-vs-unchanged decision.

This module owns the question "is this result worth notifying about?".
Site modules must not answer it themselves — that's how duplicate-alert
logic ends up implemented four slightly different ways.

Note the separate `alertable` gate on StockResult: a site may veto a
notification for a site-specific reason (Amazon's scalp detection) without
this module learning what that reason is.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from sites.base import StockResult

logger = logging.getLogger(__name__)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SiteState:
    """Per-site state file: {product_id: {in_stock, name, price_value, ...}}.

    A brand-new state file **seeds silently**: with nothing stored, every
    in-stock product looks like a fresh restock and you'd get one alert per
    product on first run, which is noise rather than news. Seeding is
    logged so the baseline is visible.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.is_first_run = not path.exists()
        self._entries: dict[str, dict] = {}
        if not self.is_first_run:
            try:
                self._entries = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as e:
                # A corrupt state file must not stop the run. Treating it as
                # a first run re-seeds rather than alerting on everything.
                logger.warning("%s unreadable (%s) — treating as first run", path, e)
                self.is_first_run = True

    def should_notify(self, result: StockResult) -> bool:
        """True when this result is a transition worth telling the user about.

        Out-of-stock -> in-stock is the event. Staying in stock is not, or
        every run would re-alert for the same thing.
        """
        if not result.in_stock or not result.alertable:
            return False
        if self.is_first_run:
            return False
        previous = self._entries.get(result.product_id)
        if previous is None:
            # Genuinely new product, already purchasable — worth knowing.
            return True
        return not previous.get("in_stock", False)

    def record(self, result: StockResult) -> None:
        previous = self._entries.get(result.product_id, {})
        self._entries[result.product_id] = {
            "name": result.product_name,
            "url": result.url,
            "in_stock": result.in_stock,
            "price_text": result.price_text,
            "price_value": result.price_value,
            "currency": result.currency,
            "seller": result.seller,
            "notes": list(result.notes),
            "first_seen": previous.get("first_seen", _utc_now()),
            "last_seen": _utc_now(),
        }

    def prune(self, keep: set[str]) -> int:
        """Drop entries no longer being watched.

        news-notifier's live state carried 22-24 entries against a 6-ASIN
        whitelist — orphans from earlier iterations, growing unbounded and
        carrying stale prices. Cheap to prevent, tedious to clean up later.
        """
        stale = set(self._entries) - keep
        for product_id in stale:
            del self._entries[product_id]
        if stale:
            logger.info("%s: pruned %d entry/entries no longer watched", self.path.name, len(stale))
        return len(stale)

    def save(self) -> None:
        """Write atomically — a crash mid-write must not corrupt the file."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self._entries, indent=2, ensure_ascii=False, sort_keys=True)
        fd, tmp_path = tempfile.mkstemp(dir=self.path.parent, prefix=f".{self.path.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(payload + "\n")
            os.replace(tmp_path, self.path)
        except BaseException:
            Path(tmp_path).unlink(missing_ok=True)
            raise
