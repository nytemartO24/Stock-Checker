"""Delivery-date parsing and the "moved earlier" decision.

Ported from news-notifier's track_delivery_multi.py. It came back because a
long estimate is itself a form of unavailability: an Amazon listing can have
an add-to-cart button and a date three months out, which is not meaningfully
in stock. Knowing the date is part of knowing whether you can have the thing.

Pure logic — no browser, no network — so the parsing and the alert decision
are testable offline, which matters because both have subtle failure modes
that produced confidently-wrong answers in the original.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import re
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

# A delivery estimate is never in the past, and Amazon does not quote arrivals
# years out. Anything outside this window is a parse artefact (a stray "2019"
# in marketing copy, a mis-anchored day number), so it is rejected rather than
# stored and alerted on.
MAX_DELIVERY_HORIZON_DAYS = 400

# How much earlier a date must move to be worth reporting. Amazon nudges its
# estimates by a day constantly; a date flickering between 22 and 23 February
# is noise, not news.
DEFAULT_MIN_IMPROVEMENT_DAYS = 7

# Ordinal suffixes and connectors that sit between the day number and the
# month across these locales: English "1st", French "1er", Spanish "15 de
# enero", Italian "1°". Without the Spanish connector "15 de enero" cannot
# match at all, which is part of why .es once returned nothing.
_ORDINAL = r"(?:st|nd|rd|th|er|ère|ème|°|º|ª)?"
_CONNECTOR = r"(?:de\s+|d'|di\s+)?"

_PATTERN_CACHE: dict[frozenset, re.Pattern] = {}


def build_date_pattern(months: dict) -> re.Pattern:
    """Match a date in either order, in any configured language.

    Day-first ("21 January 2027", "21. Januar", "15 de enero", "12 sierpnia")
    covers every EU locale here; month-first ("January 21, 2027") is how
    Amazon renders US-English dates and appears on the "/-/en/" override in
    some markets. The original pattern handled day-first only.

    Names are alternated longest-first so an abbreviation can never shadow the
    full name it prefixes ("mar" vs "marca"/"marzo").
    """
    names = "|".join(re.escape(n) for n in sorted(months, key=len, reverse=True))
    day_first = (
        rf"(?<!\d)(?P<d1>\d{{1,2}})\.?{_ORDINAL}\s+{_CONNECTOR}"
        rf"(?P<m1>{names})\.?,?\s*(?:de\s+)?(?P<y1>\d{{4}})?(?!\d)"
    )
    month_first = (
        rf"(?P<m2>{names})\.?\s+(?<!\d)(?P<d2>\d{{1,2}})\.?{_ORDINAL},?"
        rf"\s*(?P<y2>\d{{4}})?(?!\d)"
    )
    return re.compile(rf"(?:{day_first})|(?:{month_first})", re.IGNORECASE)


def pattern_for(months: dict) -> re.Pattern:
    """Cached: this is called for every parse, including once per stored entry."""
    key = frozenset(months.items())
    if key not in _PATTERN_CACHE:
        _PATTERN_CACHE[key] = build_date_pattern(months)
    return _PATTERN_CACHE[key]


def date_from_match(match: re.Match, months: dict) -> datetime.date | None:
    day = match.group("d1") or match.group("d2")
    month_name = match.group("m1") or match.group("m2")
    year = match.group("y1") or match.group("y2")
    if not day or not month_name:
        return None
    month = months.get(month_name.lower())
    if not month:
        return None
    today = datetime.date.today()
    try:
        parsed = datetime.date(int(year) if year else today.year, month, int(day))
    except ValueError:
        return None
    # No year printed (Amazon usually omits it) and the date already passed
    # this year -> it must mean next year.
    if not year and parsed < today:
        parsed = parsed.replace(year=parsed.year + 1)
    return parsed


def parse_date(text: str | None, months: dict) -> datetime.date | None:
    if not text:
        return None
    match = pattern_for(months).search(text)
    return date_from_match(match, months) if match else None


def is_plausible(parsed: datetime.date | None) -> bool:
    if parsed is None:
        return False
    return 0 <= (parsed - datetime.date.today()).days <= MAX_DELIVERY_HORIZON_DAYS


def days_until(parsed: datetime.date | None) -> int | None:
    return None if parsed is None else (parsed - datetime.date.today()).days


def _utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


class DeliveryState:
    """Last-seen and last-ALERTED delivery date per market:asin.

    The baseline for "did this improve" is the date last ALERTED about, not the
    date last seen, and that distinction is the whole point. Comparing against
    the last reading has two failure modes and Amazon exhibits both: a date
    flickering between 22 and 23 February pings on every flicker, and — less
    obvious, worse — a date creeping earlier one day at a time never pings at
    all, because no single step clears the threshold. Anchoring on the last
    alerted date lets small moves accumulate until they are worth reporting,
    then re-anchors.

    Kept separate from the alert state core owns, like reference prices, so
    clearing one does not destroy the other.
    """

    def __init__(self, path: Path, *, min_improvement_days: int = DEFAULT_MIN_IMPROVEMENT_DAYS) -> None:
        self.path = path
        self.min_improvement_days = min_improvement_days
        self._entries: dict[str, dict] = {}
        if path.exists():
            try:
                self._entries = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("%s unreadable (%s) — starting empty", path, e)

    def assess(self, key: str, current: str | None, months: dict) -> tuple[str | None, bool]:
        """Return (baseline shown to the user, worth alerting).

        Rules, ported unchanged:
          no date now          -> drop the baseline, so a date reappearing
                                  reads as newly promised
          no baseline yet      -> alert (it just became promised)
          slipped later        -> re-anchor SILENTLY; the date we told you
                                  about is no longer on offer, so future
                                  improvements are judged against what is
                                  actually promised now
          earlier by >= N days -> alert, re-anchor
          earlier by < N days  -> stay quiet AND keep the old baseline, so
                                  further moves accumulate
        """
        entry = self._entries.get(key, {})
        # An absent key (not None) means state written before this field
        # existed; seed from the last seen date so upgrading does not fire for
        # every product that already had one.
        baseline = entry.get("alerted_date") if "alerted_date" in entry else entry.get("date")

        new_parsed = parse_date(current, months)
        base_parsed = parse_date(baseline, months)

        if new_parsed is None:
            return None, False
        if base_parsed is None:
            return baseline, True
        if new_parsed > base_parsed:
            logger.info("%s: date slipped later (%r -> %r), re-anchoring quietly",
                        key, baseline, current)
            return current, False

        days_earlier = (base_parsed - new_parsed).days
        if days_earlier >= self.min_improvement_days:
            return baseline, True
        if days_earlier:
            logger.info("%s: only %d day(s) earlier than the last alerted %r "
                        "(threshold %d) — quiet, keeping the baseline so moves accumulate",
                        key, days_earlier, baseline, self.min_improvement_days)
        return baseline, False

    def record(self, key: str, current: str | None, baseline: str | None, alerted: bool) -> None:
        self._entries[key] = {
            "date": current,
            # What the next run measures against.
            "alerted_date": current if alerted else baseline,
            "last_seen": _utc_now(),
        }

    def prune(self, keep: set[str]) -> int:
        stale = set(self._entries) - keep
        if not keep and self._entries:
            return 0
        for key in stale:
            del self._entries[key]
        return len(stale)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self._entries, indent=2, ensure_ascii=False, sort_keys=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=f".{self.path.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(payload + "\n")
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
