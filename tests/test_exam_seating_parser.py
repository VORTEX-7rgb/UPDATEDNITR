"""Tests for the exam seating schedule and room layout parsers."""
from pathlib import Path
import pytest

from app.nitris.exam_seating_parser import (
    parse_exam_schedule_html,
    parse_room_sitting_details_html,
)
from app.nitris.exceptions import ExamSeatingParseError

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "exam_seating"


def _read_fixture(filename: str) -> str:
    return (FIXTURES_DIR / filename).read_text(encoding="utf-8")


def test_parse_exam_schedule_populated():
    html = _read_fixture("mid_semester_schedule.html")
    items = parse_exam_schedule_html(html)

    assert len(items) == 5

    item1 = items[0]
    assert item1.num == 1
    assert item1.day == "Day 1"
    assert item1.date_str == "21 Sep 2026"
    assert item1.time_str == "08.00 am to 10.00 am"
    assert item1.seating == "1st Seating"
    assert item1.subject == "ER2251 : Mining Geology"
    assert item1.room_no == "LA2-222"
    assert "ctl02$btnDetails" in item1.postback_target

    item5 = items[4]
    assert item5.num == 5
    assert item5.day == "Day 5"
    assert item5.date_str == "25 Sep 2026"
    assert item5.time_str == "11.00 am to 01.00 pm"
    assert item5.seating == "2nd Seating"
    assert item5.subject == "HS2331 : Introduction to Society and Culture"
    assert item5.room_no == "LA-009"
    assert "ctl06$btnDetails" in item5.postback_target


def test_parse_exam_schedule_empty_off_season():
    html = _read_fixture("mid_semester_empty.html")
    items = parse_exam_schedule_html(html)
    assert items == []


def test_parse_exam_schedule_malformed():
    with pytest.raises(ExamSeatingParseError):
        parse_exam_schedule_html("")

    with pytest.raises(ExamSeatingParseError):
        parse_exam_schedule_html("<html><body>Random text without grid</body></html>")


def test_parse_room_sitting_details_populated():
    html = _read_fixture("view_sitting_details.html")
    res = parse_room_sitting_details_html(html, target_roll="725MN1011")

    assert res.academic_year == "2026-27"
    assert res.examination == "Mid Semester"
    assert res.exam_date_seating == "21 Sep 2026 / 1st Seating"
    assert res.subject == "ER2251 : Mining Geology"
    assert res.room_no == "LA2-222"
    assert res.total_seats == 48

    assert res.student_seat is not None
    assert res.student_seat.row == 5
    assert res.student_seat.col == 2
    assert res.student_seat.total_rows == 8
    assert res.student_seat.total_cols == 6

    assert res.neighbours is not None
    # Row 5: Col 1 is 126PH0023, Col 2 is 725MN1011, Col 3 is 126EC0040
    assert res.neighbours.left == "126PH0023"
    assert res.neighbours.right == "126EC0040"
    # Col 2: Row 4 (Front) is 126EC0008, Row 6 (Behind) is 126EC0007
    assert res.neighbours.front == "126EC0008"
    assert res.neighbours.behind == "126EC0007"


def test_parse_room_sitting_corner_seat():
    html = _read_fixture("view_sitting_details.html")
    # Front-left seat: Row 1, Col 1
    res = parse_room_sitting_details_html(html, target_roll="126PH0011")

    assert res.student_seat is not None
    assert res.student_seat.row == 1
    assert res.student_seat.col == 1
    assert res.neighbours is not None
    assert res.neighbours.left is None  # Left wall/aisle
    assert res.neighbours.front is None  # Facing blackboard
    assert res.neighbours.right == "422CY5054"
    assert res.neighbours.behind == "725MN1006"


def test_parse_room_sitting_back_right_corner():
    html = _read_fixture("view_sitting_details.html")
    # Back-right seat: Row 8, Col 6
    res = parse_room_sitting_details_html(html, target_roll="126EC0061")

    assert res.student_seat is not None
    assert res.student_seat.row == 8
    assert res.student_seat.col == 6
    assert res.neighbours is not None
    assert res.neighbours.right is None  # Right wall/aisle
    assert res.neighbours.behind is None  # Rear wall
    assert res.neighbours.left == "425CY2012"
    assert res.neighbours.front == "425CY2013"


def test_parse_room_sitting_student_not_found():
    html = _read_fixture("view_sitting_details.html")
    res = parse_room_sitting_details_html(html, target_roll="999ZZ9999")

    assert res.student_seat is None
    assert res.neighbours is None
    assert res.total_seats == 48
