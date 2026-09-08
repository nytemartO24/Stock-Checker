"""Registry of site-checker types.

A plain dict rather than import-time discovery: with a handful of types,
scanning the package for subclasses would be more machinery than it saves,
and it makes "which sites exist" something you read rather than infer.
Adding a retailer is still one new module plus one line here.
"""

from __future__ import annotations

from pathlib import Path

from core.config import SiteConfig
from core.http import PoliteClient
from sites.base import SiteChecker
from sites.shopify import ShopifyChecker
from sites.woocommerce import WooCommerceChecker

CHECKER_TYPES: dict[str, type[SiteChecker]] = {
    "shopify": ShopifyChecker,
    # Not lazy like amazon: this only needs BeautifulSoup, which is a declared
    # dependency and cheap to import. Amazon is lazy because it pulls in
    # Playwright.
    "woocommerce": WooCommerceChecker,
}


def _load_amazon() -> type[SiteChecker]:
    """Imported lazily: it pulls in Playwright and BeautifulSoup, which the
    Shopify-only path has no use for. Keeping it out of module import means
    the tests and a Shopify-only run don't need a browser installed."""
    from sites.amazon import AmazonChecker

    return AmazonChecker


LAZY_CHECKER_TYPES = {"amazon": _load_amazon}


def build_checker(config: SiteConfig, client: PoliteClient, state_dir: Path | None = None) -> SiteChecker:
    checker_type = CHECKER_TYPES.get(config.type)
    if checker_type is None:
        loader = LAZY_CHECKER_TYPES.get(config.type)
        if loader is None:
            raise ValueError(
                f"site {config.name!r} has unknown type {config.type!r}; "
                f"known types: {sorted(list(CHECKER_TYPES) + list(LAZY_CHECKER_TYPES))}"
            )
        checker_type = loader()
    return checker_type(config.name, config.options, client, state_dir)
