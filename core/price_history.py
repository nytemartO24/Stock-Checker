"""Long-lived price history, so "is this the cheapest it has ever been?" is answerable.

Today only Amazon keeps anything resembling history, and only the single lowest
Amazon-sold figure per ASIN, because scalp detection needed a reference. Nothing
else remembers a price it has seen — `state/<site>.json` holds the CURRENT price
and overwrites it every run — so a genuine crash in price looks exactly like an
ordinary in-stock alert.

Kept in its own file for the same reason `amazon_reference_prices.json` is:
clearing alert state must not destroy accumulated knowledge. Alert state is
disposable; this is not.

WHAT IS RECORDED, AND WHAT IS NOT: a point is appended only when the price
CHANGES. At a half-hourly cadence across ~420 products, recording every
observation would be ~20,000 points a day to say "still 149 kr" — the same
reasoning that made Amazon's reference corroboration count distinct prices
rather than sightings. Stock state travels with each point because a low price
on an unbuyable listing is not an opportunity, and the two need distinguishing
when the history is read back.

History is NEVER pruned when a product disappears from a catalogue. A delisted
product that returns is exactly when its old prices matter most.
"""

from __future__ import annotations

import json
import logging
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Per product. At change-only cadence this is years of data; the cap exists so a
# pathological flapping price cannot grow the file without bound.
MAX_POINTS = 400


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class PriceHistory:
    """Append-on-change price points, keyed "<site>:<product_id>"."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._entries: dict[str, dict[str, Any]] = {}
        if path.exists():
            try:
                self._entries = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                # A corrupt history is not worth failing a run over, but it must
                # be loud: silently starting fresh would erase months of prices.
                logger.exception("%s unreadable — starting a new history", path.name)
                self._entries = {}
        self._dirty = False

    @staticmethod
    def key(site: str, product_id: str) -> str:
        return f"{site}:{product_id}"

    def observe(self, site: str, product_id: str, price: float | None,
                currency: str | None, in_stock: bool, name: str | None = None) -> None:
        """Record a price if it differs from the last one seen."""
        if price is None:
            return
        entry = self._entries.setdefault(
            self.key(site, product_id), {"name": name, "points": []})
        if name:
            entry["name"] = name
        points = entry["points"]
        if points:
            _, last_price, last_currency, last_stock = _unpack(points[-1])
            if (last_price == price and last_currency == (currency or "")
                    and last_stock == bool(in_stock)):
                return
        points.append([_utc_now(), price, currency or "", bool(in_stock)])
        if len(points) > MAX_POINTS:
            del points[:len(points) - MAX_POINTS]
        self._dirty = True

    def stats(self, site: str, product_id: str) -> dict[str, Any] | None:
        """Lowest/highest price ever seen, and how many points back that goes.

        `low_in_stock` matters: the cheapest sighting is only actionable if it
        was buyable at the time.
        """
        entry = self._entries.get(self.key(site, product_id))
        if not entry or not entry["points"]:
            return None
        unpacked = [_unpack(p) for p in entry["points"]]
        prices = [p for _, p, _, _ in unpacked]
        low = min(prices)
        low_point = next(u for u in unpacked if u[1] == low)
        return {
            "points": len(unpacked),
            "first_at": unpacked[0][0],
            "low": low,
            "low_at": low_point[0],
            "low_currency": low_point[2],
            "low_in_stock": low_point[3],
            "high": max(prices),
            "current": unpacked[-1][1],
            "currency": unpacked[-1][2],
        }

    def save(self) -> None:
        if not self._dirty:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self._entries, ensure_ascii=False, sort_keys=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent,
                                   prefix=f".{self.path.name}.", suffix=".tmp")
        try:
            with open(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
            Path(tmp).replace(self.path)
        except Exception:
            Path(tmp).unlink(missing_ok=True)
            raise
        self._dirty = False


def _unpack(point: list) -> tuple[str, float, str, bool]:
    """Tolerate points written by an older, shorter format."""
    at = point[0]
    price = point[1]
    currency = point[2] if len(point) > 2 else ""
    in_stock = bool(point[3]) if len(point) > 3 else False
    return at, price, currency, in_stock
