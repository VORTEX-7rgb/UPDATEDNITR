"""Golden tests for NITRIS Home.aspx timetable parser."""
from __future__ import annotations

from pathlib import Path
import pytest
from app.nitris.parser import parse_home_page, parse_timetable_from_home, TimetableSlot
from app.nitris.exceptions import HomeParseError

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "home_redacted.html"


def test_parse_redacted_fixture():
    """Verify that parse_home_page parses all 28 entries from the golden fixture."""
    html = FIXTURE_PATH.read_text(encoding="utf-8")
    result = parse_home_page(html)

    slots = result.timetable
    # 23 class slots + 5 lunch slots (Mon-Fri) = 28 total slots
    assert len(slots) == 28

    # Verify Monday slots
    mon_slots = [s for s in slots if s.day == "Monday"]
    assert len(mon_slots) == 8  # 4 morning + 1 lunch + 3 lab
    assert mon_slots[0].subject == "ER2251"
    assert mon_slots[0].start_time == "08:00"
    assert mon_slots[0].end_time == "08:55"
    assert mon_slots[0].room == ""
    assert mon_slots[0].subject_name == "Mining Geology"
    assert mon_slots[0].course_type == "Theory"

    assert mon_slots[1].subject == "MN2101"
    assert mon_slots[1].room == "205"

    # Verify LUNCH on Monday (period 5)
    mon_lunch = next(s for s in mon_slots if s.is_break)
    assert mon_lunch.subject == "LUNCH"
    assert mon_lunch.start_time == "12:00"
    assert mon_lunch.end_time == "13:15"

    # Verify LUNCH correctly tracked into Tuesday via rowspan
    tue_slots = [s for s in slots if s.day == "Tuesday"]
    assert len(tue_slots) == 8  # 4 morning + 1 lunch + 3 lab
    tue_lunch = next(s for s in tue_slots if s.is_break)
    assert tue_lunch.subject == "LUNCH"
    assert tue_lunch.start_time == "12:00"
    assert tue_lunch.end_time == "13:15"

    # Verify Tuesday afternoon lab starts at 13:15 (NOT shifted into lunch!)
    tue_lab = next(s for s in tue_slots if s.subject == "ER2271")
    assert tue_lab.start_time == "13:15"
    assert tue_lab.period_index == 6

    # Verify Wednesday has morning empty, lunch break, and 2 afternoon labs
    wed_slots = [s for s in slots if s.day == "Wednesday"]
    assert len(wed_slots) == 3  # 1 lunch + 2 lab
    wed_lunch = next(s for s in wed_slots if s.is_break)
    assert wed_lunch.start_time == "12:00"
    ec_lab = [s for s in wed_slots if s.subject == "EC2700"]
    assert len(ec_lab) == 2
    assert ec_lab[0].start_time == "13:15"
    assert ec_lab[0].room == "EC403"


def test_parse_empty_html_raises_error():
    """Verify that invalid HTML raises HomeParseError."""
    with pytest.raises(HomeParseError):
        parse_timetable_from_home("<html><body>No timetable here</body></html>")
