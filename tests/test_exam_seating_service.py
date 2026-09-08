"""Unit tests for the exam seating service and visual layout generators."""
import pytest

from app.services.exam_seating_service import (
    make_room_cache_key,
    _ordinal,
    format_seat_direction,
    generate_visual_seat_map,
    format_neighbours_block,
    _compute_student_view_from_cached_grid,
    render_exam_schedule_message,
    render_room_seating_message,
)


def test_make_room_cache_key():
    key = make_room_cache_key(
        academic_year=" 2026-27 ",
        exam_type="MID_SEM",
        subject="ER2251 : Mining Geology",
        date_str="21 Sep 2026",
        room_no="LA2-222",
    )
    assert key == "2026-27:mid_sem:ER2251:21_Sep_2026:LA2-222"


def test_ordinal():
    assert _ordinal(1) == "1st"
    assert _ordinal(2) == "2nd"
    assert _ordinal(3) == "3rd"
    assert _ordinal(4) == "4th"
    assert _ordinal(11) == "11th"
    assert _ordinal(12) == "12th"
    assert _ordinal(13) == "13th"
    assert _ordinal(21) == "21st"
    assert _ordinal(22) == "22nd"
    assert _ordinal(23) == "23rd"


def test_format_seat_direction():
    seat = {"row": 5, "col": 2, "total_rows": 8, "total_cols": 6}
    text = format_seat_direction(seat)
    assert "5th bench from the FRONT" in text
    assert "Row 5 of 8" in text
    assert "2nd seat from the LEFT aisle" in text
    assert "Column 2 of 6" in text
    assert "Facing the teacher's podium / blackboard" in text


def test_generate_visual_seat_map_width_boundary():
    # 6 columns x 8 rows
    seat = {"row": 5, "col": 2, "total_rows": 8, "total_cols": 6}
    map_str = generate_visual_seat_map(seat)
    lines = map_str.split("\n")

    # Verify no line exceeds 28 characters
    for idx, line in enumerate(lines):
        assert len(line) <= 28, f"Line {idx} exceeds 28 characters: {line!r} (len={len(line)})"

    assert "[🟢]" in map_str
    assert "▶R5" in map_str


def test_generate_visual_seat_map_large_hall_windowing():
    # 15 columns x 20 rows (large auditorium)
    seat = {"row": 12, "col": 9, "total_rows": 20, "total_cols": 15}
    map_str = generate_visual_seat_map(seat)
    lines = map_str.split("\n")

    # Strict mobile width guarantee: must not exceed 28 characters
    for idx, line in enumerate(lines):
        assert len(line) <= 28, f"Large hall line {idx} exceeds 28 characters: {line!r} (len={len(line)})"

    assert "[🟢]" in map_str
    assert "▶R12" in map_str


def test_format_neighbours_block():
    neighbours = {
        "left": "126PH0023",
        "right": "126EC0040",
        "front": "126EC0008",
        "behind": "126EC0007",
    }
    block = format_neighbours_block(neighbours)
    assert "126PH0023" in block
    assert "126EC0040" in block
    assert "126EC0008" in block
    assert "126EC0007" in block

    # Boundary cases (wall / aisle / blackboard)
    boundary_neighbours = {
        "left": None,
        "right": "126EC0040",
        "front": None,
        "behind": "126EC0007",
    }
    block2 = format_neighbours_block(boundary_neighbours)
    assert "[Left Aisle / Wall]" in block2
    assert "[Blackboard / Podium]" in block2


def test_compute_student_view_from_cached_grid():
    cached = {
        "academic_year": "2026-27",
        "examination": "Mid Semester",
        "exam_date_seating": "21 Sep 2026 / 1st Seating",
        "subject": "ER2251 : Mining Geology",
        "room_no": "LA2-222",
        "grid": {
            "1,1": "126PH0011",
            "1,2": "422CY5054",
            "2,1": "725MN1006",
            "2,2": "126EC0020",
        },
    }

    # Student at (2, 2)
    view = _compute_student_view_from_cached_grid(cached, "126EC0020")
    assert view["student_seat"] is not None
    assert view["student_seat"]["row"] == 2
    assert view["student_seat"]["col"] == 2
    assert view["student_seat"]["total_rows"] == 2
    assert view["student_seat"]["total_cols"] == 2
    assert view["neighbours"]["left"] == "725MN1006"
    assert view["neighbours"]["front"] == "422CY5054"
    assert view["neighbours"]["right"] is None
    assert view["neighbours"]["behind"] is None


def test_render_messages():
    # Schedule with 1 item
    res = {
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
            }
        ],
    }
    text, kb = render_exam_schedule_message(res)
    assert "ER2251 : Mining Geology" in text
    assert "LA2-222" in text
    assert len(kb.inline_keyboard) >= 2

    # Room seating message
    seat_res = {
        "exam_type": "mid_sem",
        "roll_number": "725MN1011",
        "item": res["items"][0],
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
    s_text, s_kb = render_room_seating_message(seat_res)
    assert "LA2-222" in s_text
    assert "5th bench from the FRONT" in s_text
    assert "126PH0023" in s_text
    assert len(s_kb.inline_keyboard) >= 2


def test_persistent_user_schedule_caching(tmp_path, monkeypatch):
    import app.services.exam_seating_service as ess

    # Point _CACHE_FILE to temporary file
    temp_cache_file = tmp_path / "exam_seating_cache.json"
    monkeypatch.setattr(ess, "_CACHE_FILE", temp_cache_file)

    # Store a user schedule
    sample_items = [
        {
            "num": 1,
            "day": "Day 1",
            "date_str": "21 Sep 2026",
            "time_str": "08.00 am to 10.00 am",
            "seating": "1st Seating",
            "subject": "ER2251 : Mining Geology",
            "room_no": "LA2-222",
            "postback_target": "ctl02$btnDetails",
        }
    ]
    ess.store_cached_user_schedule(
        user_id=42,
        exam_type="mid_sem",
        roll_number="725MN1011",
        items=sample_items,
        subpage_url="http://example.com/Mid_Semester.aspx",
    )

    # In-memory retrieval
    cached = ess.get_cached_user_schedule(42, "mid_sem")
    assert cached is not None
    assert cached["roll_number"] == "725MN1011"
    assert len(cached["items"]) == 1

    # Verify disk persistence
    ess._save_disk_cache()
    assert temp_cache_file.exists()

    # Clear in-memory caches to simulate bot restart
    ess._user_schedule_cache.clear()
    assert ess.get_cached_user_schedule(42, "mid_sem") is None

    # Load from disk
    ess._load_disk_cache()
    reloaded = ess.get_cached_user_schedule(42, "mid_sem")
    assert reloaded is not None
    assert reloaded["roll_number"] == "725MN1011"
    assert reloaded["items"][0]["room_no"] == "LA2-222"


def test_persistent_room_cache_v1_backward_compatibility(tmp_path, monkeypatch):
    import json
    import app.services.exam_seating_service as ess

    temp_cache_file = tmp_path / "exam_seating_cache_v1.json"
    monkeypatch.setattr(ess, "_CACHE_FILE", temp_cache_file)

    # Write old v1 format (flat dict without "version" wrapper)
    v1_data = {
        "2026-27:mid_sem:ER2251:21_Sep_2026:LA2-222": {
            "room_no": "LA2-222",
            "grid": {"1,1": "725MN1011"},
        }
    }
    temp_cache_file.write_text(json.dumps(v1_data), encoding="utf-8")

    ess._global_room_cache.clear()
    ess._load_disk_cache()

    room = ess.get_cached_room_layout("2026-27:mid_sem:ER2251:21_Sep_2026:LA2-222")
    assert room is not None
    assert room["room_no"] == "LA2-222"

