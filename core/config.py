"""Loads config/sites.yaml and folds global defaults into each site.

Not in CLAUDE.md's original tree: both entrypoints need config loading, and
duplicating the defaults-merge in each of them is exactly the drift this
project is trying to avoid.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class SiteConfig:
    name: str
    type: str
    enabled: bool = True
    options: dict[str, Any] = field(default_factory=dict)
    rate_limit: dict[str, float] = field(default_factory=dict)

    @property
    def min_delay(self) -> float:
        return float(self.rate_limit.get("min_delay_seconds", 1.0))

    @property
    def max_delay(self) -> float:
        return float(self.rate_limit.get("max_delay_seconds", 3.0))

    @property
    def timeout(self) -> float:
        return float(self.rate_limit.get("timeout_seconds", 30.0))

    @property
    def max_retries(self) -> int:
        return int(self.rate_limit.get("max_retries", 3))


def load_config(path: Path) -> list[SiteConfig]:
    """Return every configured site, enabled or not.

    Callers filter on `.enabled` — keeping disabled sites in the list means
    a status command can report "configured but off" rather than silently
    showing nothing.
    """
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    defaults = raw.get("defaults") or {}
    default_rate = defaults.get("rate_limit") or {}

    sites: list[SiteConfig] = []
    for name, entry in (raw.get("sites") or {}).items():
        entry = dict(entry or {})
        site_type = entry.pop("type", None)
        if not site_type:
            raise ValueError(f"site {name!r} has no 'type'")
        enabled = bool(entry.pop("enabled", True))
        # Site-level rate limits override the global default key by key, so
        # a site can tighten one dimension without restating all of them.
        rate_limit = {**default_rate, **(entry.pop("rate_limit", None) or {})}
        sites.append(SiteConfig(name=name, type=site_type, enabled=enabled, options=entry, rate_limit=rate_limit))
    return sites
