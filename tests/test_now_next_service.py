"""Tests for now_next_service IST calculation and edge cases."""
from __future__ import annotations

from datetime import datetime, time
from pathlib import Path
import pytest
from zoneinfo import ZoneInfo

from app.config import IST
from app.db.models import TimetableEntry
from app.nitris.parser import parse_home_page
from app.services.now_next_service import (
    resolve_now_and_next,
    format_now_next_message,
    format_day_schedule,
)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "home_redacted.html"


@pytest.fixture
def timetable_entries() -> list[TimetableEntry]:
    """Convert parsed fixture slots into TimetableEntry mock models."""
    html = FIXTURE_PATH.read_text(encoding="utf-8")
    slots = parse_home_page(html).timetable
    entries = []
    for s in slots:
        e = TimetableEntry(
            id=1,
            user_id=100,
            weekday=s.weekday,
            period_index=s.period_index,
            start_time=datetime.strptime(s.start_time, "%H:%M").time(),
            end_time=datetime.strptime(s.end_time, "%H:%M").time(),
            subject_code=s.subject,
            room=s.room,
            is_break=s.is_break,
            subject_name=s.subject_name,
            course_type=s.course_type,
            synced_at=datetime.now(IST),
        )
        entries.append(e)
    return entries


def test_mid_class(timetable_entries):
    """Monday 09:30 IST -> in MN2101 class, next is MN2105."""
    now_ist = datetime(2026, 8, 17, 9, 30, tzinfo=IST)  # Monday 09:30
    res = resolve_now_and_next(timetable_entries, now_ist)

    assert res.current_class is not None
    assert res.current_class.subject_code == "MN2101"
    assert res.current_class.room == "205"
    assert res.next_class is not None
    assert res.next_class.subject_code == "MN2105"
    assert res.minutes_until_next == 30


def test_lunch_break(timetable_entries):
    """Monday 12:30 IST -> in lunch break, next is MN2701 at 13:15."""
    now_ist = datetime(2026, 8, 17, 12, 30, tzinfo=IST)  # Monday 12:30
    res = resolve_now_and_next(timetable_entries, now_ist)

    assert res.is_lunch_break is True
    assert res.current_class is None
    assert res.next_class is not None
    assert res.next_class.subject_code == "MN2701"
    assert res.next_class.start_time == time(13, 15)
    assert res.minutes_until_next == 45


def test_free_period(timetable_entries):
    """Wednesday 10:00 IST -> free period, next is EC2700 at 13:15."""
    now_ist = datetime(2026, 8, 19, 10, 0, tzinfo=IST)  # Wednesday 10:00
    res = resolve_now_and_next(timetable_entries, now_ist)

    assert res.current_class is None
    assert res.is_lunch_break is False
    assert res.next_class is not None
    assert res.next_class.subject_code == "EC2700"
    assert res.next_class.start_time == time(13, 15)


def test_night_rollover_to_tomorrow(timetable_entries):
    """Monday 22:00 IST -> all done today, next is Tuesday 08:00 MN2101."""
    now_ist = datetime(2026, 8, 17, 22, 0, tzinfo=IST)  # Monday 22:00
    res = resolve_now_and_next(timetable_entries, now_ist)

    assert res.is_day_done is True
    assert res.current_class is None
    assert res.next_class is not None
    assert res.next_class.subject_code == "MN2101"
    assert res.next_class.weekday == 1  # Tuesday
    assert res.next_class_day_offset == 1
    assert res.minutes_until_next == 10 * 60  # 10 hours


def test_midnight_monday(timetable_entries):
    """Monday 00:01 IST -> early morning, first class is today at 08:00 ER2251."""
    now_ist = datetime(2026, 8, 17, 0, 1, tzinfo=IST)  # Monday 00:01
    res = resolve_now_and_next(timetable_entries, now_ist)

    assert res.is_before_first_class is True
    assert res.current_class is None
    assert res.next_class is not None
    assert res.next_class.subject_code == "ER2251"
    assert res.next_class.weekday == 0  # Monday
    assert res.next_class_day_offset == 0
    assert res.minutes_until_next == 7 * 60 + 59  # 7h 59m


def test_weekend_to_monday(timetable_entries):
    """Saturday 14:00 IST -> weekend, next is Monday 08:00 ER2251."""
    now_ist = datetime(2026, 8, 22, 14, 0, tzinfo=IST)  # Saturday 14:00
    res = resolve_now_and_next(timetable_entries, now_ist)

    assert res.is_weekend is True
    assert res.current_class is None
    assert res.next_class is not None
    assert res.next_class.subject_code == "ER2251"
    assert res.next_class.weekday == 0  # Monday
    assert res.next_class_day_offset == 2  # in 2 days (Monday)


def test_utc_conversion_correctness(timetable_entries):
    """Test that passing a UTC-aware datetime automatically converts to IST."""
    # 04:00 UTC on Monday == 09:30 IST on Monday (mid-class)
    now_utc = datetime(2026, 8, 17, 4, 0, tzinfo=ZoneInfo("UTC"))
    res = resolve_now_and_next(timetable_entries, now_utc)

    assert res.current_class is not None
    assert res.current_class.subject_code == "MN2101"
    assert res.evaluated_at_ist.hour == 9
    assert res.evaluated_at_ist.minute == 30


def test_formatters(timetable_entries):
    """Verify HTML message formatters generate valid HTML output."""
    now_ist = datetime(2026, 8, 17, 9, 30, tzinfo=IST)
    res = resolve_now_and_next(timetable_entries, now_ist)
    msg = format_now_next_message(res)

    assert "CURRENT CLASS:" in msg
    assert "MN2101" in msg
    assert "Room #205" in msg
    assert "NEXT CLASS" in msg
    assert "MN2105" in msg

    day_msg = format_day_schedule(timetable_entries, 0)
    assert "MONDAY TIMETABLE" in day_msg
    assert "LUNCH BREAK" in day_msg
    assert "ER2251" in day_msg
