"""Unit tests for the exam seating bot handler."""
import pytest
from app.bot.handlers.exam_seating import _kb_loading
from app.services.exam_seating_service import (
    render_exam_schedule_message,
    render_room_seating_message,
)
from app.bot.common import get_dashboard_keyboard


def test_dashboard_keyboard_has_exam_seating():
    kb = get_dashboard_keyboard()
    button_texts = [btn.text for row in kb.inline_keyboard for btn in row]
    callbacks = [btn.callback_data for row in kb.inline_keyboard for btn in row]

    assert "💺 Exam Seating" in button_texts
    assert "db_exams" in callbacks


def test_render_schedule_empty():
    res = {
        "success": True,
        "exam_type": "mid_sem",
        "roll_number": "725MN1011",
        "items": [],
    }
    text, kb = render_exam_schedule_message(res)
    assert "Examination Seating Chart (Mid Semester)" in text
    assert "725MN1011" in text
    assert "No exam schedule is currently published" in text

    btn_texts = [btn.text for row in kb.inline_keyboard for btn in row]
    assert "🔄 Switch to End Sem" in btn_texts
    assert "🔄 Refresh" in btn_texts
    assert "🏠 Dashboard" in btn_texts


def test_render_schedule_populated():
    res = {
        "success": True,
        "exam_type": "mid_sem",
        "roll_number": "725MN1011",
        "items": [
            {
                "num": 1,
                "day": "Day 1",
                "date_str": "21 Sep 2026",
                "time_str": "08.00 am to 10.00 am",
                "seating": "1st Seating",
                "subject": "ER2251 : Mining Geology",
                "room_no": "LA2-222",
                "postback_target": "ctl02$btnDetails",
            },
            {
                "num": 2,
                "day": "Day 2",
                "date_str": "22 Sep 2026",
                "time_str": "08.00 am to 10.00 am",
                "seating": "1st Seating",
                "subject": "MN2101 : Surface Mining",
                "room_no": "LA2-122",
                "postback_target": "ctl03$btnDetails",
            },
        ],
    }
    text, kb = render_exam_schedule_message(res)
    assert "<b>Scheduled Exams:</b> 2 subject(s)" in text
    assert "ER2251 : Mining Geology" in text
    assert "LA2-222" in text
    assert "MN2101 : Surface Mining" in text

    callbacks = [btn.callback_data for row in kb.inline_keyboard for btn in row]
    assert "exams_seat:1:mid_sem" in callbacks
    assert "exams_seat:2:mid_sem" in callbacks
    assert "exams_type:end_sem" in callbacks
    assert "exams_refresh:mid_sem" in callbacks


def test_render_room_seating_found():
    res = {
        "success": True,
        "exam_type": "mid_sem",
        "roll_number": "725MN1011",
        "item": {
            "num": 1,
            "subject": "ER2251 : Mining Geology",
            "room_no": "LA2-222",
            "date_str": "21 Sep 2026",
            "seating": "1st Seating",
        },
        "layout": {
            "subject": "ER2251 : Mining Geology",
            "room_no": "LA2-222",
            "exam_date_seating": "21 Sep 2026 / 1st Seating",
            "total_seats": 48,
            "student_seat": {"row": 5, "col": 2, "total_rows": 8, "total_cols": 6},
            "neighbours": {
                "left": "126PH0023",
                "right": "126EC0040",
                "front": "126EC0008",
                "behind": "126EC0007",
            },
        },
    }
    text, kb = render_room_seating_message(res)
    assert "Exam Seating Details" in text
    assert "ER2251 : Mining Geology" in text
    assert "LA2-222" in text
    assert "5th bench from the FRONT" in text
    assert "2nd seat from the LEFT aisle" in text
    assert "126PH0023" in text

    callbacks = [btn.callback_data for row in kb.inline_keyboard for btn in row]
    assert "exams_type:mid_sem" in callbacks
    assert "exams_seat_refresh:1:mid_sem" in callbacks
    assert "inbox_back_dashboard" in callbacks


def test_render_room_seating_not_found():
    res = {
        "success": True,
        "exam_type": "mid_sem",
        "roll_number": "999ZZ9999",
        "item": {
            "num": 1,
            "subject": "ER2251 : Mining Geology",
            "room_no": "LA2-222",
        },
        "layout": {
            "subject": "ER2251 : Mining Geology",
            "room_no": "LA2-222",
            "total_seats": 48,
            "student_seat": None,
            "neighbours": None,
        },
    }
    text, kb = render_room_seating_message(res)
    assert "Seat Not Found in Room Grid" in text
    assert "999ZZ9999" in text
    assert "LA2-222" in text
