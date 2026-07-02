"""Natural-language temporal phrase resolution.

Turns phrases like ``"next week"``, ``"this Friday"``, ``"in 3 days"`` or an
explicit date such as ``"July 15"`` into a concrete :class:`TimeRange`.

All computation happens in the user's timezone and every returned datetime is
timezone-aware.  Resolution is case-insensitive and tolerant of surrounding
words (e.g. ``"for next week"`` still resolves to next week).  Ranges span
``[start-of-day, end-of-day]``; single-moment phrases keep a full-day span.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from dateutil import parser as dateutil_parser

__all__ = ["TimeRange", "resolve_temporal"]

_WEEKDAYS: dict[str, int] = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}


@dataclass
class TimeRange:
    """A resolved temporal window.

    ``start``/``end`` are timezone-aware datetimes (or ``None`` when the phrase
    was resolvable to neither bound).  ``label`` echoes the original phrase.
    """

    start: datetime | None
    end: datetime | None
    label: str


def _start_of_day(moment: datetime) -> datetime:
    return moment.replace(hour=0, minute=0, second=0, microsecond=0)


def _end_of_day(moment: datetime) -> datetime:
    return moment.replace(hour=23, minute=59, second=59, microsecond=999999)


def _day_span(moment: datetime, label: str) -> TimeRange:
    return TimeRange(_start_of_day(moment), _end_of_day(moment), label)


def _span(start: datetime, end: datetime, label: str) -> TimeRange:
    return TimeRange(_start_of_day(start), _end_of_day(end), label)


def resolve_temporal(phrase: str, now: datetime, tz: str) -> TimeRange | None:
    """Resolve ``phrase`` to a :class:`TimeRange` relative to ``now`` in ``tz``.

    Returns ``None`` when the phrase contains no resolvable temporal reference.
    """

    if not phrase or not phrase.strip():
        return None

    tzinfo = ZoneInfo(tz)
    if now.tzinfo is None:
        now = now.replace(tzinfo=tzinfo)
    else:
        now = now.astimezone(tzinfo)

    label = phrase
    text = phrase.lower()

    # Monday 00:00 of the week containing ``now``.
    week_start = _start_of_day(now - timedelta(days=now.weekday()))

    # --- weekend (must precede "week": "this weekend" contains "this week") ---
    if "weekend" in text:
        if "next" in text:
            saturday = week_start + timedelta(days=7 + 5)
        elif "last" in text:
            saturday = week_start + timedelta(days=-7 + 5)
        else:
            saturday = week_start + timedelta(days=5)
        return _span(saturday, saturday + timedelta(days=1), label)

    # --- explicit weekday: "next/this/last {weekday}" ---
    for name, weekday in _WEEKDAYS.items():
        if re.search(rf"\b{name}\b", text):
            if "next" in text:
                # First such weekday strictly after the coming Sunday, i.e. the
                # occurrence inside next Mon..Sun week.
                target = week_start + timedelta(days=7 + weekday)
            elif "last" in text:
                target = week_start + timedelta(days=-7 + weekday)
            else:  # "this {weekday}" or a bare weekday -> current week
                target = week_start + timedelta(days=weekday)
            return _day_span(target, label)

    # --- whole weeks ---
    if "week" in text:
        if "next" in text:
            monday = week_start + timedelta(days=7)
        elif "last" in text:
            monday = week_start - timedelta(days=7)
        else:  # "this week" or a bare "week"
            monday = week_start
        return _span(monday, monday + timedelta(days=6), label)

    # --- relative single days ---
    if "tomorrow" in text:
        return _day_span(now + timedelta(days=1), label)
    if "yesterday" in text:
        return _day_span(now - timedelta(days=1), label)
    if "today" in text:
        return _day_span(now, label)

    # --- day windows: "in N days" / "next N days" / "last N days" ---
    match = re.search(r"\bin\s+(\d+)\s+days?\b", text)
    if match:
        return _day_span(now + timedelta(days=int(match.group(1))), label)

    match = re.search(r"\b(?:next|coming)\s+(\d+)\s+days?\b", text)
    if match:
        return _span(now, now + timedelta(days=int(match.group(1))), label)

    match = re.search(r"\b(?:last|past|previous)\s+(\d+)\s+days?\b", text)
    if match:
        return _span(now - timedelta(days=int(match.group(1))), now, label)

    # --- whole calendar months ---
    if "month" in text:
        year, month = now.year, now.month
        if "next" in text:
            month += 1
            if month > 12:
                month, year = 1, year + 1
        elif "last" in text or "previous" in text or "past" in text:
            month -= 1
            if month < 1:
                month, year = 12, year - 1
        # else "this month" / bare "month" -> current month
        last_day = calendar.monthrange(year, month)[1]
        start = datetime(year, month, 1, 0, 0, 0, 0, tzinfo=tzinfo)
        end = datetime(year, month, last_day, 23, 59, 59, 999999, tzinfo=tzinfo)
        return TimeRange(start, end, label)

    # --- explicit date fallback via fuzzy parse ---
    try:
        parsed = dateutil_parser.parse(phrase, fuzzy=True, default=_start_of_day(now))
    except (ValueError, OverflowError, TypeError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tzinfo)
    else:
        parsed = parsed.astimezone(tzinfo)
    return _day_span(parsed, label)
