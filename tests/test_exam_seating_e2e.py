"""End-to-end integration tests for the Exam Seating & Visual Seat Map feature."""
from pathlib import Path
from unittest.mock import AsyncMock, patch
import pytest

from app.nitris.exam_seating_parser import parse_exam_schedule_html, parse_room_sitting_details_html
from app.services.exam_seating_service import (
    store_user_schedule_state,
    get_user_schedule_state,
    make_room_cache_key,
    get_cached_room_layout,
    store_cached_room_layout,
    _compute_student_view_from_cached_grid,
    render_exam_schedule_message,
    render_room_seating_message,
    generate_visual_seat_map,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "exam_seating"


def _read_fixture(filename: str) -> str:
    return (FIXTURES_DIR / filename).read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_exam_seating_end_to_end_flow():
    """Simulate complete student lifecycle from schedule view to seat inspection."""
    schedule_html = _read_fixture("mid_semester_schedule.html")
    details_html = _read_fixture("view_sitting_details.html")

    user_id = 9991
    user_roll = "725MN1011"
    subpage_url = "https://nitris.nitrkl.ac.in/nitris/Student/Examination/SittingChart/Mid_Semester.aspx?tokens=abc"

    # ── Step 1: Parse and store schedule page ──────────────────────────────
    items = parse_exam_schedule_html(schedule_html)
    assert len(items) == 5

    store_user_schedule_state(user_id, subpage_url, schedule_html, items)
    cached_state = get_user_schedule_state(user_id)
    assert cached_state is not None
    saved_url, saved_html, saved_items = cached_state
    assert saved_url == subpage_url
    assert len(saved_items) == 5

    # ── Step 2: Render schedule overview ───────────────────────────────────
    sched_render_dict = {
        "success": True,
        "exam_type": "mid_sem",
        "items": saved_items,
        "roll_number": user_roll,
    }
    sched_text, sched_kb = render_exam_schedule_message(sched_render_dict)
    assert "<b>Scheduled Exams:</b> 5 subject(s)" in sched_text
    assert "ER2251 : Mining Geology" in sched_text
    assert "LA2-222" in sched_text

    btn_callbacks = [b.callback_data for row in sched_kb.inline_keyboard for b in row]
    assert "exams_seat:1:mid_sem" in btn_callbacks
    assert "exams_type:end_sem" in btn_callbacks

    # ── Step 3: Student taps Exam #1 ("ER2251", "LA2-222") ─────────────────
    item1 = saved_items[0]
    layout_result = parse_room_sitting_details_html(details_html, target_roll=user_roll)
    assert layout_result.room_no == "LA2-222"
    assert layout_result.student_seat is not None
    assert layout_result.student_seat.row == 5
    assert layout_result.student_seat.col == 2

    # Save to composite disk/memory cache
    cache_key = make_room_cache_key(
        academic_year=layout_result.academic_year,
        exam_type="mid_sem",
        subject=item1["subject"],
        date_str=item1["date_str"],
        room_no=item1["room_no"],
    )
    serialized_layout = {
        "academic_year": layout_result.academic_year,
        "examination": layout_result.examination,
        "exam_date_seating": layout_result.exam_date_seating,
        "subject": layout_result.subject,
        "room_no": layout_result.room_no,
        "total_seats": layout_result.total_seats,
        "grid": {f"{r},{c}": roll for (r, c), roll in layout_result.grid.items()},
    }
    store_cached_room_layout(cache_key, serialized_layout)

    # ── Step 4: Render detailed room seat card for Student A ───────────────
    view_a = _compute_student_view_from_cached_grid(serialized_layout, user_roll)
    seat_render_dict_a = {
        "success": True,
        "exam_type": "mid_sem",
        "roll_number": user_roll,
        "item": item1,
        "layout": view_a,
    }
    card_text_a, card_kb_a = render_room_seating_message(seat_render_dict_a)

    # Verify physical instructions
    assert "<b>5th bench from the FRONT</b> (Row 5 of 8)" in card_text_a
    assert "<b>2nd seat from the LEFT aisle</b> (Column 2 of 6)" in card_text_a
    assert "Facing the teacher's podium / blackboard" in card_text_a

    # Verify visual map
    assert "▶R5" in card_text_a
    assert "[🟢]" in card_text_a

    # Verify 360 neighbours
    assert "126PH0023" in card_text_a  # Left
    assert "126EC0040" in card_text_a  # Right
    assert "126EC0008" in card_text_a  # Front
    assert "126EC0007" in card_text_a  # Behind

    # ── Step 5: Multi-Tenant Cache Test: Student B in same hall ────────────
    # Roll "425CY2006" is at Row 5, Col 4 in LA2-222
    student_b_roll = "425CY2006"
    cached_from_memory = get_cached_room_layout(cache_key)
    assert cached_from_memory is not None

    view_b = _compute_student_view_from_cached_grid(cached_from_memory, student_b_roll)
    assert view_b["student_seat"]["row"] == 5
    assert view_b["student_seat"]["col"] == 4
    assert view_b["neighbours"]["left"] == "126EC0040"
    assert view_b["neighbours"]["right"] == "126EC0059"
    assert view_b["neighbours"]["front"] == "126EC0047"
    assert view_b["neighbours"]["behind"] == "126EC0046"

    seat_render_dict_b = {
        "success": True,
        "exam_type": "mid_sem",
        "roll_number": student_b_roll,
        "item": item1,
        "layout": view_b,
    }
    card_text_b, _ = render_room_seating_message(seat_render_dict_b)
    assert "4th seat from the LEFT aisle" in card_text_b
    assert "126EC0040" in card_text_b


@pytest.mark.asyncio
async def test_off_season_empty_state_e2e():
    """Verify off-season empty state returns clean message without crash."""
    empty_html = _read_fixture("mid_semester_empty.html")
    items = parse_exam_schedule_html(empty_html)
    assert items == []

    res = {
        "success": True,
        "exam_type": "end_sem",
        "roll_number": "725MN1011",
        "items": items,
    }
    text, kb = render_exam_schedule_message(res)
    assert "Examination Seating Chart (End Semester)" in text
    assert "No exam schedule is currently published for End Semester" in text
    assert kb is not None
