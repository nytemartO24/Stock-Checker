"""Registry of site-checker types.

A plain dict rather than import-time discovery: with two types, scanning the
package for subclasses would be more machinery than it saves, and it makes
"which sites exist" something you read rather than infer. Adding a retailer
is still one new file plus one line here.
"""

from __future__ import annotations

from core.config import SiteConfig
from core.http import PoliteClient
from sites.base import SiteChecker
from sites.shopify import ShopifyChecker

CHECKER_TYPES: dict[str, type[SiteChecker]] = {
    "shopify": ShopifyChecker,
}


def build_checker(config: SiteConfig, client: PoliteClient) -> SiteChecker:
    try:
        checker_type = CHECKER_TYPES[config.type]
    except KeyError:
        raise ValueError(
            f"site {config.name!r} has unknown type {config.type!r}; "
            f"known types: {sorted(CHECKER_TYPES)}"
        ) from None
    return checker_type(config.name, config.options, client)
