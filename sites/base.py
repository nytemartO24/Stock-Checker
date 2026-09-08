"""The interface every site module implements, and the result type.

DIVERGENCE FROM CLAUDE.md's original sketch: the interface is
`check() -> Iterator[StockResult]` over a whole site, not
`check(product_config) -> StockResult` per product. Shopify answers an
entire collection in ONE request, so a per-product interface would either
re-fetch the same JSON once per product or hide a cache inside the module.
Amazon's per-ASIN model still fits — it just yields repeatedly. Streaming
results also means one product's failure can't lose the ones already found.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator


@dataclass
class StockResult:
    """One product's state at one site, as observed in a single check."""

    product_id: str
    """Stable identity *within* the site — ASIN, SKU, Shopify handle. Used as
    the state key, so it must not change when a title or URL does."""

    product_name: str
    url: str
    in_stock: bool
    """Purchasable OR pre-orderable. Both count."""

    price_text: str | None = None
    """As displayed, e.g. "€68.63" — for humans."""

    price_value: float | None = None
    """Normalized numeric value, for comparison."""

    currency: str | None = None
    seller: str | None = None
    """Context in the alert, never a gate — a third-party seller is not by
    itself a problem (see CLAUDE.md's scalper section)."""

    delivery_date: str | None = None
    """When the retailer says it would arrive, as displayed. For Amazon a long
    estimate is itself a form of unavailability — an add-to-cart button and a
    date three months out is not meaningfully in stock."""

    alertable: bool = True
    """A site may veto notification for a site-specific reason. core/ does
    not ask why."""

    alert_reason: str | None = None
    """A site may REQUEST an alert it alone can judge, stating why — the
    mirror of `alertable`. Amazon uses it for "delivery date moved earlier",
    which is not a stock transition and so is invisible to core's own rules.
    core sends it and renders the reason without interpreting it."""

    notes: list[str] = field(default_factory=list)
    """Free-text commentary rendered verbatim in the alert."""


class SiteChecker(ABC):
    """One retailer. Constructed with its config and a client; `check()` is
    the only thing core calls."""

    def __init__(self, name: str, options: dict, client, state_dir: Path | None = None) -> None:
        self.name = name
        self.options = options
        self.client = client
        self.state_dir = state_dir
        """Where a site may keep auxiliary state of its own, beyond the
        alert state core/storage.py owns (Amazon's reference prices)."""
        self.errors = 0
        """Count of things this run failed to see. Non-zero means the yielded
        results are an INCOMPLETE view of the site, which callers must know:
        pruning state against an incomplete view deletes products that are
        merely missing, and they then re-alert as new when they reappear."""

    @abstractmethod
    def check(self) -> Iterator[StockResult]:
        """Yield one result per product found.

        Implementations must not raise for a single bad product — log it and
        continue, so one failure can't take the rest of the site down.
        """
