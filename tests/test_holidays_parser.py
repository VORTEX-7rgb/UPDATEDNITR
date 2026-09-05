"""Unit tests for ASP.NET calendar holiday parser."""
from pathlib import Path
import pytest

from app.nitris.holidays_parser import parse_holidays_html, HolidayEntry, HolidaysPage
from app.nitris.exceptions import HolidaysParseError

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "holidays" / "calendar_september_2026.html"


def test_parse_holidays_html_valid():
    html = FIXTURE_PATH.read_text(encoding="utf-8")
    page = parse_holidays_html(html)

    assert page.month == 9
    assert page.year == 2026
    assert page.month_label == "September 2026"
    assert page.prev_event_argument == "V9709"
    assert page.next_event_argument == "V9770"
    assert len(page.holidays) == 5

    # Check parsed holidays
    h0 = page.holidays[0]
    assert h0.day == 4
    assert h0.name == "Janmashtami"
    assert h0.month == 9
    assert h0.year == 2026
    assert not h0.is_trailing

    h1 = page.holidays[1]
    assert h1.day == 14
    assert h1.name == "Ganesh Chaturthi"
    assert not h1.is_trailing

    h2 = page.holidays[2]
    assert h2.day == 15
    assert h2.name == "Nuakhai"
    assert not h2.is_trailing

    h3 = page.holidays[3]
    assert h3.day == 17
    assert h3.name == "Vishwakarma Puja"
    assert not h3.is_trailing

    # Trailing holiday: Oct 2 Gandhi Jayanti shown on September calendar
    h4 = page.holidays[4]
    assert h4.day == 2
    assert h4.name == "Mahatma Gandhis Birthday"
    assert h4.month == 9
    assert h4.year == 2026
    assert h4.is_trailing


def test_parse_holidays_excludes_weekends_and_regular_days():
    html = FIXTURE_PATH.read_text(encoding="utf-8")
    page = parse_holidays_html(html)

    days = [h.day for h in page.holidays]
    # Weekend days: 5, 6, 12, 13, 19, 20, 26, 27
    for weekend_day in (5, 6, 12, 13, 19, 20, 26, 27):
        assert weekend_day not in days, f"Weekend day {weekend_day} should not be in holidays"

    # Regular days: 1, 2 (Sep 2 is not a holiday, only trailing Oct 2 is), 3, 7, 8, etc.
    assert 1 not in days
    assert 3 not in days
    assert 7 not in days


def test_parse_holidays_missing_calendar():
    with pytest.raises(HolidaysParseError, match="not found"):
        parse_holidays_html("<html><body>No calendar here</body></html>")


def test_parse_holidays_invalid_header():
    bad_html = """
    <table id="ContentPlaceHolder3_cal1">
      <tr><td colspan="7">
        <table><tr><td>Bad Month Header 2099</td></tr></table>
      </td></tr>
    </table>
    """
    with pytest.raises(HolidaysParseError, match="Could not parse month/year"):
        parse_holidays_html(bad_html)
