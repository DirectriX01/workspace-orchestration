"""Unit tests pinning natural-language temporal resolution.

All cases use a FIXED ``now`` of Wednesday 2026-07-01 12:00 in Asia/Kolkata so
that week/weekday/month boundaries are deterministic.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.core.temporal import TimeRange, resolve_temporal

TZ = "Asia/Kolkata"
_TZINFO = ZoneInfo(TZ)
NOW = datetime(2026, 7, 1, 12, 0, tzinfo=_TZINFO)  # a Wednesday


def _sod(year: int, month: int, day: int) -> datetime:
    """Start of the given day, tz-aware."""
    return datetime(year, month, day, 0, 0, 0, 0, tzinfo=_TZINFO)


def _eod(year: int, month: int, day: int) -> datetime:
    """End of the given day, tz-aware."""
    return datetime(year, month, day, 23, 59, 59, 999999, tzinfo=_TZINFO)


# (phrase, expected_start, expected_end)
CASES = [
    # single-day, relative-to-now boundaries
    ("today", _sod(2026, 7, 1), _eod(2026, 7, 1)),
    ("tomorrow", _sod(2026, 7, 2), _eod(2026, 7, 2)),
    ("yesterday", _sod(2026, 6, 30), _eod(2026, 6, 30)),
    # whole weeks: Mon..Sun containing / following / preceding now
    ("this week", _sod(2026, 6, 29), _eod(2026, 7, 5)),
    ("next week", _sod(2026, 7, 6), _eod(2026, 7, 12)),
    ("last week", _sod(2026, 6, 22), _eod(2026, 6, 28)),
    # phrase embedded in surrounding words
    ("for next week", _sod(2026, 7, 6), _eod(2026, 7, 12)),
    # "next {weekday}" = first such weekday STRICTLY AFTER the coming Sunday
    ("next Tuesday", _sod(2026, 7, 7), _eod(2026, 7, 7)),
    ("next Wednesday", _sod(2026, 7, 8), _eod(2026, 7, 8)),
    ("next Monday", _sod(2026, 7, 6), _eod(2026, 7, 6)),
    # "this {weekday}" = that weekday within the current Mon..Sun week
    ("this Friday", _sod(2026, 7, 3), _eod(2026, 7, 3)),
    ("this Monday", _sod(2026, 6, 29), _eod(2026, 6, 29)),
    # weekend = coming Sat-Sun
    ("this weekend", _sod(2026, 7, 4), _eod(2026, 7, 5)),
    # day windows
    ("in 3 days", _sod(2026, 7, 4), _eod(2026, 7, 4)),
    ("next 3 days", _sod(2026, 7, 1), _eod(2026, 7, 4)),
    ("last 7 days", _sod(2026, 6, 24), _eod(2026, 7, 1)),
    # whole calendar months
    ("this month", _sod(2026, 7, 1), _eod(2026, 7, 31)),
    ("last month", _sod(2026, 6, 1), _eod(2026, 6, 30)),
    ("next month", _sod(2026, 8, 1), _eod(2026, 8, 31)),
    # explicit date via fuzzy parse (year filled from now)
    ("July 15", _sod(2026, 7, 15), _eod(2026, 7, 15)),
    ("meeting on July 15", _sod(2026, 7, 15), _eod(2026, 7, 15)),
]


@pytest.mark.parametrize("phrase,expected_start,expected_end", CASES)
def test_resolve_temporal_boundaries(
    phrase: str, expected_start: datetime, expected_end: datetime
) -> None:
    result = resolve_temporal(phrase, NOW, TZ)
    assert result is not None, f"{phrase!r} should resolve"
    assert result.start == expected_start, f"{phrase!r} start"
    assert result.end == expected_end, f"{phrase!r} end"
    # every returned datetime is timezone-aware
    assert result.start.tzinfo is not None
    assert result.end.tzinfo is not None


@pytest.mark.parametrize(
    "phrase",
    ["flibbertigibbet", "the quick brown fox", "hello there friend", "", "   "],
)
def test_unparseable_returns_none(phrase: str) -> None:
    assert resolve_temporal(phrase, NOW, TZ) is None


def test_label_echoes_original_phrase() -> None:
    # label preserves original casing / surrounding words verbatim
    result = resolve_temporal("For Next Week", NOW, TZ)
    assert result is not None
    assert result.label == "For Next Week"
    # case-insensitive matching still resolves to next week
    assert result.start == _sod(2026, 7, 6)
    assert result.end == _eod(2026, 7, 12)


def test_returns_timerange_instance() -> None:
    result = resolve_temporal("today", NOW, TZ)
    assert isinstance(result, TimeRange)


def test_now_converted_into_user_tz() -> None:
    # A UTC ``now`` equal to the frozen IST instant must resolve identically.
    utc_now = NOW.astimezone(ZoneInfo("UTC"))
    result = resolve_temporal("today", utc_now, TZ)
    assert result is not None
    assert result.start == _sod(2026, 7, 1)
    assert result.end == _eod(2026, 7, 1)
