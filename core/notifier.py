"""Discord webhook sending and message formatting.

Formatting lives here rather than in the sites so that a test exercises the
*real* formatter — a test that renders its own idea of the message will
happily pass while the thing you actually receive is broken.

This module renders `result.notes` verbatim and never inspects them. That's
what keeps site-specific policy (Amazon's scalp detection) out of core.
"""

from __future__ import annotations

import logging
import os

import httpx

logger = logging.getLogger(__name__)

MAX_NAME_LENGTH = 70
DISCORD_MAX_CONTENT = 2000


def truncate(text: str, limit: int = MAX_NAME_LENGTH) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def format_stock_alert(site_name: str, result) -> str:
    """One message per newly-available product."""
    lines = [f"\U0001f6d2 **In stock on {site_name}:** {truncate(result.product_name)}"]

    detail = []
    if result.price_text:
        detail.append(result.price_text)
    if result.seller:
        detail.append(f"sold by {result.seller}")
    if detail:
        lines.append("  ·  ".join(detail))

    # Notes are the site's own commentary (e.g. a suspected-scalp warning).
    # Rendered as given; core does not interpret them.
    lines.extend(f"⚠️ {note}" for note in result.notes)
    lines.append(result.url)
    return "\n".join(lines)


def format_new_product_alert(site_name: str, result) -> str:
    """A product that has never been seen before.

    Deliberately distinct from a restock: this is "this exists now, do you
    want it?", so it states the stock position rather than assuming the thing
    is buyable, and it is worth sending even when it is not.
    """
    state = "in stock" if result.in_stock else "not in stock yet"
    lines = [f"🆕 **New product on {site_name}** ({state}): {truncate(result.product_name)}"]
    if result.price_text:
        lines.append(result.price_text)
    lines.extend(f"⚠️ {note}" for note in result.notes)
    lines.append(result.url)
    return "\n".join(lines)


class DiscordNotifier:
    """Posts to a Discord webhook, or logs what it would have posted.

    `dry_run` defaults to True so that running anything by hand — a debug
    pass, a first run, a test — cannot spam a real channel. The entrypoints
    turn it off explicitly via --send-discord, matching news-notifier's
    convention.
    """

    def __init__(self, webhook_url: str | None = None, user_id: str | None = None, *, dry_run: bool = True) -> None:
        self.webhook_url = webhook_url if webhook_url is not None else os.environ.get("DISCORD_WEBHOOK_URL", "")
        self.user_id = user_id if user_id is not None else os.environ.get("DISCORD_USER_ID", "")
        self.dry_run = dry_run

    def send(self, message: str) -> bool:
        """Returns True if a message actually went out."""
        logger.info("ALERT:\n%s", message)
        if self.dry_run:
            logger.info("[dry-run] not posting to Discord (pass --send-discord to post)")
            return False
        if not self.webhook_url:
            logger.warning("DISCORD_WEBHOOK_URL not set — skipping Discord post")
            return False

        content = f"<@{self.user_id}> {message}" if self.user_id else message
        if len(content) > DISCORD_MAX_CONTENT:
            content = content[: DISCORD_MAX_CONTENT - 1] + "…"
        try:
            response = httpx.post(
                self.webhook_url,
                json={"content": content, "allowed_mentions": {"parse": ["users"]}},
                timeout=10,
            )
            response.raise_for_status()
            return True
        except httpx.HTTPError as e:
            # A failed notification must never kill the run. Returning False
            # is load-bearing: main.py skips recording this product so the
            # next pass re-detects the same transition and retries the
            # alert. If a caller records regardless, the alert is lost.
            logger.warning("failed to send Discord notification: %s", e)
            return False
