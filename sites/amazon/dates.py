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

    def observe(self, key: str, display: str | None, iso: str | None) -> tuple[str | None, bool]:
        """Record this reading and return (baseline shown to the user, alert?).

        Comparison is on the RESOLVED date, never by re-parsing the display
        string. That is not a style choice — re-parsing was a guaranteed bug.
        Amazon shows dates without a year ("22 September") and the parser
        assumes next year for one already past, so a stored baseline silently
        became ~350 days in the FUTURE the moment its day went by, and every
        real date then looked like an enormous improvement: a nonsense
        "moved earlier: 22 September -> 5 October", on a schedule.
        Plausibility screening cannot catch it either, because 350 days is
        inside the 400-day window. Storing the resolved date removes the
        ambiguity instead of trying to detect it.

        Rules, otherwise as ported:
          no date now          -> drop the baseline, so a date reappearing
                                  reads as newly promised
          no baseline yet      -> alert (it just became promised)
          slipped later        -> re-anchor SILENTLY; the date we were told
                                  about is no longer on offer
          earlier by >= N days -> alert, re-anchor
          earlier by < N days  -> quiet, KEEP the old baseline so further
                                  moves accumulate
        """
        entry = self._entries.get(key, {})
        baseline_display = entry.get("alerted_date")
        baseline_iso = entry.get("alerted_iso")

        baseline, alerted = self._decide(key, display, iso, baseline_display, baseline_iso)
        self._entries[key] = {
            "date": display,
            "date_iso": iso,
            # What the next run measures against.
            "alerted_date": display if alerted else baseline,
            "alerted_iso": iso if alerted else (baseline_iso if baseline else None),
            "last_seen": _utc_now(),
        }
        return baseline, alerted

    def _decide(self, key, display, iso, baseline_display, baseline_iso):
        if iso is None:
            return None, False
        if baseline_iso is None:
            # Either genuinely new, or state written before the resolved date
            # was stored. A display-only baseline cannot be compared, so
            # re-anchor quietly — upgrading must not fire for every product
            # that already had a date.
            if baseline_display:
                logger.info("%s: baseline %r predates resolved-date storage — "
                            "re-anchoring quietly", key, baseline_display)
                return display, False
            return None, True

        current = datetime.date.fromisoformat(iso)
        baseline = datetime.date.fromisoformat(baseline_iso)
        if current > baseline:
            logger.info("%s: date slipped later (%r -> %r), re-anchoring quietly",
                        key, baseline_display, display)
            return display, False

        days_earlier = (baseline - current).days
        if days_earlier >= self.min_improvement_days:
            return baseline_display, True
        if days_earlier:
            logger.info("%s: only %d day(s) earlier than the last alerted %r "
                        "(threshold %d) — quiet, keeping the baseline so moves accumulate",
                        key, days_earlier, baseline_display, self.min_improvement_days)
        return baseline_display, False

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
