"""Exam seating service — handles schedule fetching, room layout, and visual seat map generation.

Features:
  1. Two-Tier Caching:
     - Composite Disk & Memory Cache: Room seating arrangements are keyed on
       `f"{year}:{exam_type}:{subject_code}:{date}:{room_no}"` in `data/exam_seating_cache.json`.
       Identical for all students in the same room; 0ms response on cache hits.
     - Per-User Schedule Page State: Caches the latest schedule page HTML & postback targets
       in memory (15 min TTL, max 256 entries) for instant details postback navigation.
  2. Foolproof Physical Orientation:
     - Front: Blackboard / Podium
     - Rear: Entrance / Back wall
     - Rows: 1-indexed from FRONT (Row 1) to REAR (Row N)
     - Columns: 1-indexed from LEFT aisle (Col 1) to RIGHT aisle (Col N) facing the board
     - 360° Neighbour Verification: Left, Right, Front, Behind
  3. Mobile-Responsive ASCII Seat Map:
     - Monospace mini-map strictly bounded under 28 characters wide to prevent
       wrapping on narrow smartphone viewports.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from aiogram import types
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.config import config, IST
from app.db.database import async_session_factory
from app.db.models import User
from app.nitris.client import NitrisClient
from app.nitris.exam_seating_parser import (
    ExamScheduleItem,
    SeatPosition,
    NeighbourInfo,
    RoomLayoutResult,
)
from app.nitris.exceptions import (
    LoginError,
    LoginUnavailableError,
    CredentialsQuarantinedError,
    ExamSeatingParseError,
    AttendanceWorkflowError,
)
from app.nitris.gateway import NitrisCircuitOpenError
from app.nitris.session_pool import with_pooled_session
from app.nitris.auth_gate import on_login_failure
from app.utils import esc, spawn_tracked

logger = logging.getLogger(__name__)

# ── 1. Composite Global Room Seating Cache (Disk & Memory) ───────────────────
# Key: f"{academic_year}:{exam_type}:{subject_code}:{date_str}:{room_no}"
# Value: (serialized_room_dict, expires_at_monotonic)
_global_room_cache: dict[str, tuple[dict, float]] = {}
GLOBAL_ROOM_CACHE_TTL = 86400.0 * 14  # 14 days

_CACHE_FILE = Path(__file__).resolve().parent.parent.parent / "data" / "exam_seating_cache.json"

# ── 2. Persistent Per-User Schedule Cache (Disk & Memory) ───────────────────
# Key: (user_id, exam_type)
# Value: (schedule_dict, expires_at_monotonic)
_user_schedule_cache: dict[tuple[int, str], tuple[dict, float]] = {}
USER_SCHEDULE_CACHE_TTL = 86400.0 * 7  # 7 days

# ── 3. Per-User Postback Continuity State Cache (Memory Only) ────────────────
# user_id -> (subpage_url, raw_html, items_dicts, expires_at_monotonic)
_user_schedule_pages: dict[int, tuple[str, str, list[dict], float]] = {}
_PAGE_CACHE_TTL = 900.0  # 15 minutes
_USER_PAGES_MAX_ENTRIES = 256


def _prune_user_schedule_pages() -> None:
    """Evict expired schedule state and enforce max capacity."""
    now = time.monotonic()
    expired = [uid for uid, (_, _, _, exp) in _user_schedule_pages.items() if exp <= now]
    for uid in expired:
        _user_schedule_pages.pop(uid, None)
    if len(_user_schedule_pages) > _USER_PAGES_MAX_ENTRIES:
        sorted_by_exp = sorted(_user_schedule_pages.items(), key=lambda kv: kv[1][3])
        for uid, _ in sorted_by_exp[: len(_user_schedule_pages) - _USER_PAGES_MAX_ENTRIES]:
            _user_schedule_pages.pop(uid, None)


def store_user_schedule_state(
    user_id: int, subpage_url: str, raw_html: str, items: list[ExamScheduleItem]
) -> None:
    """Safely store per-user schedule HTML for subsequent LinkButton postbacks."""
    items_dicts = [
        {
            "num": it.num,
            "day": it.day,
            "date_str": it.date_str,
            "time_str": it.time_str,
            "seating": it.seating,
            "subject": it.subject,
            "room_no": it.room_no,
            "postback_target": it.postback_target,
        }
        for it in items
    ]
    _user_schedule_pages[user_id] = (
        str(subpage_url),
        raw_html,
        items_dicts,
        time.monotonic() + _PAGE_CACHE_TTL,
    )
    _prune_user_schedule_pages()


def get_user_schedule_state(
    user_id: int,
) -> Optional[tuple[str, str, list[dict]]]:
    """Retrieve active schedule subpage URL, HTML, and items for postback."""
    entry = _user_schedule_pages.get(user_id)
    if entry and entry[3] > time.monotonic():
        return entry[0], entry[1], entry[2]
    _user_schedule_pages.pop(user_id, None)
    return None


def get_cached_user_schedule(
    user_id: int,
    exam_type: str = "mid_sem",
) -> Optional[dict]:
    """Retrieve persistently cached schedule dictionary for user and exam type."""
    entry = _user_schedule_cache.get((user_id, exam_type))
    if entry and entry[1] > time.monotonic():
        return entry[0]
    _user_schedule_cache.pop((user_id, exam_type), None)
    return None


def store_cached_user_schedule(
    user_id: int,
    exam_type: str,
    roll_number: str,
    items: list[dict],
    subpage_url: str = "",
) -> None:
    """Store user schedule in persistent cache and offload async write to disk."""
    data = {
        "user_id": user_id,
        "exam_type": exam_type,
        "roll_number": roll_number,
        "items": items,
        "subpage_url": str(subpage_url),
        "cached_at": time.time(),
    }
    _user_schedule_cache[(user_id, exam_type)] = (
        data,
        time.monotonic() + USER_SCHEDULE_CACHE_TTL,
    )
    try:
        loop = asyncio.get_running_loop()
        if loop.is_running():
            spawn_tracked(asyncio.to_thread(_save_disk_cache), name="exam-schedule-disk-write")
        else:
            _save_disk_cache()
    except RuntimeError:
        _save_disk_cache()


def make_room_cache_key(
    academic_year: str,
    exam_type: str,
    subject: str,
    date_str: str,
    room_no: str,
) -> str:
    """Construct an unambiguous composite cache key for an exam hall layout."""
    subj_code = (
        subject.split(":")[0].strip().upper() if ":" in subject else subject.strip().upper()
    )
    # Extract only the base academic year (e.g. "2026-27" from "2026-27 / Autumn")
    ay_match = re.search(r"(\d{4}-\d{2,4})", academic_year)
    ay = (
        ay_match.group(1)
        if ay_match
        else academic_year.strip().replace(" ", "").split("/")[0]
    )
    et = exam_type.strip().lower()
    dt = date_str.strip().replace(" ", "_")
    rm = room_no.strip().upper().replace(" ", "")
    return f"{ay}:{et}:{subj_code}:{dt}:{rm}"


def _load_disk_cache() -> None:
    """Load persistent global room seating cache and user schedules from disk into memory."""
    if not _CACHE_FILE.exists():
        return
    try:
        raw = _CACHE_FILE.read_text(encoding="utf-8")
        data = json.loads(raw)
        if isinstance(data, dict) and "version" in data:
            rooms = data.get("rooms", {})
            for key, val in rooms.items():
                norm_key = re.sub(r"/Autumn|/Spring", "", key)
                _global_room_cache[norm_key] = (val, time.monotonic() + GLOBAL_ROOM_CACHE_TTL)
                _global_room_cache[key] = (val, time.monotonic() + GLOBAL_ROOM_CACHE_TTL)
            schedules = data.get("schedules", {})
            for key_str, val in schedules.items():
                parts = key_str.split(":", 1)
                if len(parts) == 2:
                    try:
                        uid = int(parts[0])
                        et = parts[1]
                        _user_schedule_cache[(uid, et)] = (
                            val,
                            time.monotonic() + USER_SCHEDULE_CACHE_TTL,
                        )
                    except ValueError:
                        continue
            logger.info(
                "Loaded %d room layout(s) and %d schedule(s) from persistent cache (%s)",
                len(_global_room_cache),
                len(_user_schedule_cache),
                _CACHE_FILE,
            )
        elif isinstance(data, dict):
            # Backward compatibility with v1 flat dictionary
            for key, val in data.items():
                _global_room_cache[key] = (val, time.monotonic() + GLOBAL_ROOM_CACHE_TTL)
            logger.info(
                "Loaded %d room layout(s) from persistent cache v1 (%s)",
                len(_global_room_cache),
                _CACHE_FILE,
            )
    except Exception as e:
        logger.warning("Failed to load exam seating disk cache: %r", e)


def _save_disk_cache() -> None:
    """Persist global room seating cache and user schedules to disk."""
    try:
        _CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        dump_rooms = {key: val[0] for key, val in _global_room_cache.items()}
        dump_schedules = {
            f"{uid}:{et}": val[0] for (uid, et), val in _user_schedule_cache.items()
        }
        dump_data = {
            "version": 2,
            "rooms": dump_rooms,
            "schedules": dump_schedules,
        }
        _CACHE_FILE.write_text(json.dumps(dump_data, indent=2), encoding="utf-8")
    except Exception as e:
        logger.warning("Failed to save exam seating disk cache: %r", e)


# Populate cache at module load time
_load_disk_cache()


def get_cached_room_layout(cache_key: str) -> Optional[dict]:
    """Retrieve cached room layout data by composite key."""
    entry = _global_room_cache.get(cache_key)
    if entry and entry[1] > time.monotonic():
        return entry[0]
    return None


def store_cached_room_layout(cache_key: str, data: dict) -> None:
    """Store room layout in memory and offload async write to disk."""
    _global_room_cache[cache_key] = (data, time.monotonic() + GLOBAL_ROOM_CACHE_TTL)
    try:
        loop = asyncio.get_running_loop()
        if loop.is_running():
            spawn_tracked(asyncio.to_thread(_save_disk_cache), name="exam-seating-disk-write")
        else:
            _save_disk_cache()
    except RuntimeError:
        _save_disk_cache()


# ── 3. Core Service Methods ──────────────────────────────────────────────────

async def fetch_user_exam_schedule(
    user_id: int,
    exam_type: str = "mid_sem",
    force_refresh: bool = False,
) -> dict:
    """Fetch student's exam schedule for mid_sem or end_sem via pooled session."""
    async with async_session_factory() as session:
        user = await session.get(User, user_id)
        if not user:
            return {"success": False, "error": "User not found"}
        if not user.credentials_valid:
            return {
                "success": False,
                "error": "Credentials marked invalid. Please /forgot to update.",
            }
        roll_number = user.roll_number
        encrypted_password = user.encrypted_password

    # Check persistent cache first if not force_refresh
    if not force_refresh:
        cached_sched = get_cached_user_schedule(user_id, exam_type)
        if cached_sched:
            logger.info("Serving exam schedule for user %s (%s) from persistent cache", user_id, exam_type)
            return {
                "success": True,
                "exam_type": exam_type,
                "items": cached_sched["items"],
                "roll_number": cached_sched.get("roll_number", roll_number),
                "cached": True,
            }
        cached_state = get_user_schedule_state(user_id)
        if cached_state:
            subpage_url, raw_html, items_dicts = cached_state
            url_str = str(subpage_url)
            # Only return cached if it matches requested exam_type
            if (exam_type == "mid_sem" and "Mid_Semester.aspx" in url_str) or (
                exam_type == "end_sem" and "End_Semester.aspx" in url_str
            ):
                logger.info("Serving exam schedule for user %s from memory cache", user_id)
                return {
                    "success": True,
                    "exam_type": exam_type,
                    "items": items_dicts,
                    "roll_number": roll_number,
                    "cached": True,
                }

    try:
        async def _work(client: NitrisClient, password: str):
            return await client.fetch_exam_seating_schedule(exam_type=exam_type)

        items, raw_html, subpage_url = await with_pooled_session(
            user_id=user_id,
            roll_number=roll_number,
            encrypted_password=encrypted_password,
            work=_work,
        )

        store_user_schedule_state(user_id, subpage_url, raw_html, items)

        items_dicts = [
            {
                "num": it.num,
                "day": it.day,
                "date_str": it.date_str,
                "time_str": it.time_str,
                "seating": it.seating,
                "subject": it.subject,
                "room_no": it.room_no,
                "postback_target": it.postback_target,
            }
            for it in items
        ]

        store_cached_user_schedule(user_id, exam_type, roll_number, items_dicts, subpage_url)

        return {
            "success": True,
            "exam_type": exam_type,
            "items": items_dicts,
            "roll_number": roll_number,
            "cached": False,
        }

    except (LoginError, LoginUnavailableError) as e:
        await on_login_failure(user_id, e)
        return {
            "success": False,
            "error": "Login failed. Please check your credentials with /forgot.",
        }
    except CredentialsQuarantinedError:
        return {
            "success": False,
            "error": "Account temporarily locked due to invalid credentials. Use /forgot.",
        }
    except NitrisCircuitOpenError:
        return {
            "success": False,
            "error": "NITRIS portal is currently degraded. Please try again in a few minutes.",
        }
    except ExamSeatingParseError as e:
        logger.error("Failed to parse exam schedule for user %s: %r", user_id, e)
        return {
            "success": False,
            "error": "Could not read exam schedule from NITRIS. The portal format may have changed.",
        }
    except Exception as e:
        logger.exception("Unexpected error in fetch_user_exam_schedule: %r", e)
        return {
            "success": False,
            "error": f"Failed to fetch exam schedule: {e}",
        }


async def fetch_user_room_layout(
    user_id: int,
    exam_type: str,
    item_num: int,
    force_refresh: bool = False,
) -> dict:
    """Fetch and parse room sitting chart for a specific schedule item."""
    async with async_session_factory() as session:
        user = await session.get(User, user_id)
        if not user:
            return {"success": False, "error": "User not found"}
        if not user.credentials_valid:
            return {"success": False, "error": "Credentials marked invalid."}
        roll_number = user.roll_number
        encrypted_password = user.encrypted_password

    # Check if target item and room layout are already in persistent cache (0ms path)
    target_item = None
    state = get_user_schedule_state(user_id)
    if state and not force_refresh:
        _, _, items_dicts = state
        target_item = next((it for it in items_dicts if it["num"] == item_num), None)

    if not target_item and not force_refresh:
        cached_sched = get_cached_user_schedule(user_id, exam_type)
        if cached_sched:
            target_item = next(
                (it for it in cached_sched.get("items", []) if it["num"] == item_num),
                None,
            )

    if target_item and not force_refresh:
        cache_key = make_room_cache_key(
            academic_year="2026-27",
            exam_type=exam_type,
            subject=target_item["subject"],
            date_str=target_item["date_str"],
            room_no=target_item["room_no"],
        )
        cached_layout = get_cached_room_layout(cache_key)
        if cached_layout:
            logger.info("Serving room layout (%s) from composite cache (0ms)", cache_key)
            computed = _compute_student_view_from_cached_grid(cached_layout, roll_number)
            return {
                "success": True,
                "cached": True,
                "item": target_item,
                "layout": computed,
                "roll_number": roll_number,
                "exam_type": exam_type,
            }

    # Live fetch path: ensure active schedule state with viewstate is available
    if not state or force_refresh:
        sched_res = await fetch_user_exam_schedule(
            user_id, exam_type=exam_type, force_refresh=True
        )
        if not sched_res.get("success"):
            return sched_res
        state = get_user_schedule_state(user_id)
        if not state:
            return {"success": False, "error": "Could not retrieve schedule state."}

    subpage_url, raw_html, items_dicts = state

    target_item = next((it for it in items_dicts if it["num"] == item_num), None)
    if not target_item:
        return {"success": False, "error": f"Exam item #{item_num} not found in schedule."}

    postback_target = target_item.get("postback_target")
    if not postback_target:
        return {"success": False, "error": "No sitting details link available for this exam."}

    # Attempt composite cache lookup
    cache_key = make_room_cache_key(
        academic_year="2026-27",  # default prefix for normalization
        exam_type=exam_type,
        subject=target_item["subject"],
        date_str=target_item["date_str"],
        room_no=target_item["room_no"],
    )

    if not force_refresh:
        cached_layout = get_cached_room_layout(cache_key)
        if cached_layout:
            logger.info("Serving room layout (%s) from composite cache (0ms)", cache_key)
            # Reconstruct student-specific position & neighbours from grid
            computed = _compute_student_view_from_cached_grid(cached_layout, roll_number)
            return {
                "success": True,
                "cached": True,
                "item": target_item,
                "layout": computed,
                "roll_number": roll_number,
                "exam_type": exam_type,
            }

    try:
        async def _work(client: NitrisClient, password: str):
            return await client.fetch_room_sitting_details(
                subpage_url=subpage_url,
                schedule_html=raw_html,
                postback_target=postback_target,
                target_roll=roll_number,
            )

        layout_res: RoomLayoutResult = await with_pooled_session(
            user_id=user_id,
            roll_number=roll_number,
            encrypted_password=encrypted_password,
            work=_work,
        )

        # Store in composite cache
        serialized_layout = {
            "academic_year": layout_res.academic_year,
            "examination": layout_res.examination,
            "exam_date_seating": layout_res.exam_date_seating,
            "subject": layout_res.subject,
            "room_no": layout_res.room_no,
            "total_seats": layout_res.total_seats,
            "grid": {f"{r},{c}": roll for (r, c), roll in layout_res.grid.items()},
        }
        # Update cache key with parsed academic year if available
        if layout_res.academic_year:
            cache_key = make_room_cache_key(
                academic_year=layout_res.academic_year,
                exam_type=exam_type,
                subject=target_item["subject"],
                date_str=target_item["date_str"],
                room_no=target_item["room_no"],
            )
        store_cached_room_layout(cache_key, serialized_layout)

        computed = _compute_student_view_from_cached_grid(serialized_layout, roll_number)
        return {
            "success": True,
            "cached": False,
            "item": target_item,
            "layout": computed,
            "roll_number": roll_number,
            "exam_type": exam_type,
        }

    except (LoginError, LoginUnavailableError) as e:
        await on_login_failure(user_id, e)
        return {"success": False, "error": "Login failed. Use /forgot."}
    except CredentialsQuarantinedError:
        return {"success": False, "error": "Account locked. Use /forgot."}
    except NitrisCircuitOpenError:
        return {"success": False, "error": "NITRIS is currently degraded. Please try later."}
    except ExamSeatingParseError as e:
        logger.error("Room sitting parse error for user %s: %r", user_id, e)
        return {"success": False, "error": "Could not read room layout from NITRIS."}
    except Exception as e:
        logger.exception("Unexpected error in fetch_user_room_layout: %r", e)
        return {"success": False, "error": f"Failed to fetch room layout: {e}"}


def _compute_student_view_from_cached_grid(cached_layout: dict, roll_number: str) -> dict:
    """Extract student coordinates and 4-way neighbours from cached room grid."""
    grid_raw: dict[str, str] = cached_layout.get("grid", {})
    grid: dict[tuple[int, int], str] = {}
    for key_str, roll in grid_raw.items():
        try:
            r_s, c_s = key_str.split(",")
            grid[(int(r_s), int(c_s))] = roll
        except ValueError:
            continue

    total_seats = len(grid)
    max_row = max((r for r, _ in grid.keys()), default=1)
    max_col = max((c for _, c in grid.keys()), default=1)

    norm_target = roll_number.strip().upper()
    student_seat = None
    neighbours = None

    for (r, c), roll in grid.items():
        if roll.strip().upper() == norm_target:
            student_seat = {
                "row": r,
                "col": c,
                "total_rows": max_row,
                "total_cols": max_col,
            }
            neighbours = {
                "left": grid.get((r, c - 1)),
                "right": grid.get((r, c + 1)),
                "front": grid.get((r - 1, c)),
                "behind": grid.get((r + 1, c)),
            }
            break

    return {
        "academic_year": cached_layout.get("academic_year", ""),
        "examination": cached_layout.get("examination", ""),
        "exam_date_seating": cached_layout.get("exam_date_seating", ""),
        "subject": cached_layout.get("subject", ""),
        "room_no": cached_layout.get("room_no", ""),
        "total_seats": total_seats,
        "student_seat": student_seat,
        "neighbours": neighbours,
    }


# ── 4. Presentation & Visual Map Helpers ─────────────────────────────────────

def _ordinal(n: int) -> str:
    """Return English ordinal string (e.g. 1st, 2nd, 3rd, 4th)."""
    if 11 <= (n % 100) <= 13:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def format_seat_direction(seat_dict: dict) -> str:
    """Generate unambiguous physical orientation instructions facing the Blackboard."""
    row = seat_dict["row"]
    col = seat_dict["col"]
    tot_rows = seat_dict["total_rows"]
    tot_cols = seat_dict["total_cols"]

    row_text = f"<b>{_ordinal(row)} bench from the FRONT</b> (Row {row} of {tot_rows})"
    col_text = f"<b>{_ordinal(col)} seat from the LEFT aisle</b> (Column {col} of {tot_cols})"

    return (
        f"📍 <b>Physical Room Orientation:</b>\n"
        f"• <b>Bench:</b> {row_text}\n"
        f"• <b>Position:</b> {col_text}\n"
        f"<i>(Facing the teacher's podium / blackboard)</i>"
    )


def generate_visual_seat_map(seat_dict: dict) -> str:
    """Construct an ASCII visual seat map strictly <= 28 characters wide."""
    cur_row = seat_dict["row"]
    cur_col = seat_dict["col"]
    tot_rows = seat_dict["total_rows"]
    tot_cols = seat_dict["total_cols"]

    lines = []

    # Column header line
    # If total_cols <= 7: render all columns
    # If total_cols > 7: render a 5-column focus window around student
    if tot_cols <= 7:
        cols_to_render = list(range(1, tot_cols + 1))
        header_str = "     " + " ".join(f"{c:2d}" for c in cols_to_render)
        lines.append(header_str)

        for r in range(1, tot_rows + 1):
            marker = "▶" if r == cur_row else " "
            row_lbl = f"{marker}R{r:<2d}"
            seats = []
            for c in cols_to_render:
                if r == cur_row and c == cur_col:
                    seats.append("[🟢]")
                else:
                    seats.append(" · ")
            lines.append(row_lbl + "".join(seats))
    else:
        # Windowed around current col
        start_c = max(1, min(cur_col - 2, tot_cols - 4))
        end_c = min(tot_cols, start_c + 4)
        cols_to_render = list(range(start_c, end_c + 1))

        col_hdr = "     " + " ".join(f"{c:2d}" for c in cols_to_render)
        lines.append(col_hdr)

        for r in range(1, tot_rows + 1):
            marker = "▶" if r == cur_row else " "
            row_lbl = f"{marker}R{r:<2d}"
            seats = []
            for c in cols_to_render:
                if r == cur_row and c == cur_col:
                    seats.append("[🟢]")
                else:
                    seats.append(" · ")
            lines.append(row_lbl + "".join(seats))

    return "\n".join(lines)


def format_neighbours_block(neighbours: Optional[dict]) -> str:
    """Format 360-degree adjacent neighbour verification roll numbers."""
    if not neighbours:
        return "<i>Surrounding seat details not available.</i>"

    left = neighbours.get("left") or "[Left Aisle / Wall]"
    right = neighbours.get("right") or "[Right Aisle / Wall]"
    front = neighbours.get("front") or "[Blackboard / Podium]"
    behind = neighbours.get("behind") or "[Rear Wall / Back]"

    return (
        f"👥 <b>360° Neighbour Verification:</b>\n"
        f"• <b>Left:</b> <code>{esc(left)}</code>\n"
        f"• <b>Right:</b> <code>{esc(right)}</code>\n"
        f"• <b>Front (closer to board):</b> <code>{esc(front)}</code>\n"
        f"• <b>Behind (closer to door):</b> <code>{esc(behind)}</code>"
    )


# ── 5. Message Renderers ─────────────────────────────────────────────────────

def render_exam_schedule_message(result: dict) -> tuple[str, types.InlineKeyboardMarkup]:
    """Render the student's exam schedule overview with seating buttons."""
    exam_type = result.get("exam_type", "mid_sem")
    exam_title = "Mid Semester" if exam_type == "mid_sem" else "End Semester"
    other_type = "end_sem" if exam_type == "mid_sem" else "mid_sem"
    other_title = "End Sem" if exam_type == "mid_sem" else "Mid Sem"

    items: list[dict] = result.get("items", [])
    roll_number = result.get("roll_number", "")

    builder = InlineKeyboardBuilder()

    if not items:
        text = (
            f"🗓️ <b>Examination Seating Chart ({exam_title})</b>\n\n"
            f"👤 <b>Roll Number:</b> <code>{esc(roll_number)}</code>\n\n"
            f"ℹ️ <i>No exam schedule is currently published for {exam_title}.</i>\n"
            f"NITRIS usually releases the seating schedule 3–5 days before exams begin."
        )
        builder.row(
            types.InlineKeyboardButton(
                text=f"🔄 Switch to {other_title}", callback_data=f"exams_type:{other_type}"
            )
        )
        builder.row(
            types.InlineKeyboardButton(
                text="🔄 Refresh", callback_data=f"exams_refresh:{exam_type}"
            ),
            types.InlineKeyboardButton(
                text="🏠 Dashboard", callback_data="inbox_back_dashboard"
            ),
        )
        return text, builder.as_markup()

    lines = [
        f"🗓️ <b>Examination Seating Chart ({exam_title})</b>",
        f"👤 <b>Roll Number:</b> <code>{esc(roll_number)}</code>",
        f"📚 <b>Scheduled Exams:</b> {len(items)} subject(s)\n",
    ]

    for it in items:
        lines.append(
            f"<b>{it['num']}. {esc(it['subject'])}</b>\n"
            f"   📅 {esc(it['day'])} • {esc(it['date_str'])}\n"
            f"   ⏰ {esc(it['time_str'])} ({esc(it['seating'])})\n"
            f"   🏛️ Room: <b>{esc(it['room_no'])}</b>\n"
        )
        # Create seat details button for each exam
        subj_code = it["subject"].split(":")[0].strip() if ":" in it["subject"] else it["subject"][:8]
        builder.row(
            types.InlineKeyboardButton(
                text=f"📍 Seat: {subj_code} ({it['room_no']})",
                callback_data=f"exams_seat:{it['num']}:{exam_type}",
            )
        )

    builder.row(
        types.InlineKeyboardButton(
            text=f"🔄 Switch to {other_title}", callback_data=f"exams_type:{other_type}"
        )
    )
    builder.row(
        types.InlineKeyboardButton(
            text="🔄 Refresh", callback_data=f"exams_refresh:{exam_type}"
        ),
        types.InlineKeyboardButton(
            text="🏠 Dashboard", callback_data="inbox_back_dashboard"
        ),
    )

    return "\n".join(lines), builder.as_markup()


def render_room_seating_message(result: dict) -> tuple[str, types.InlineKeyboardMarkup]:
    """Render the detailed seat position, ASCII map, and neighbour verification."""
    item = result.get("item", {})
    layout = result.get("layout", {})
    roll_number = result.get("roll_number", "")
    exam_type = result.get("exam_type", "mid_sem")
    item_num = item.get("num", 1)

    builder = InlineKeyboardBuilder()

    subject = layout.get("subject") or item.get("subject", "N/A")
    date_session = layout.get("exam_date_seating") or f"{item.get('date_str', '')} ({item.get('seating', '')})"
    room_no = layout.get("room_no") or item.get("room_no", "N/A")
    student_seat = layout.get("student_seat")
    neighbours = layout.get("neighbours")
    total_seats = layout.get("total_seats", 0)

    lines = [
        "💺 <b>Exam Seating Details</b>\n",
        f"📚 <b>Subject:</b> {esc(subject)}",
        f"📅 <b>Date & Session:</b> {esc(date_session)}",
        f"🏛️ <b>Exam Hall / Room:</b> <b>{esc(room_no)}</b>\n",
    ]

    if student_seat:
        direction_text = format_seat_direction(student_seat)
        visual_map = generate_visual_seat_map(student_seat)
        neighbours_text = format_neighbours_block(neighbours)

        lines.append(direction_text)
        lines.append(f"\n🗺️ <b>Visual Seat Map:</b>\n<pre>{visual_map}</pre>\n")
        lines.append(neighbours_text)
    else:
        lines.append(
            f"⚠️ <b>Seat Not Found in Room Grid</b>\n"
            f"Roll number <code>{esc(roll_number)}</code> was not matched in the published "
            f"chart for room <b>{esc(room_no)}</b> (total {total_seats} seats listed).\n\n"
            f"<i>Please verify your exam schedule or contact the academic office/invigilator.</i>"
        )

    builder.row(
        types.InlineKeyboardButton(
            text="🔙 Back to Schedule", callback_data=f"exams_type:{exam_type}"
        ),
        types.InlineKeyboardButton(
            text="🔄 Refresh Seat", callback_data=f"exams_seat_refresh:{item_num}:{exam_type}"
        ),
    )
    builder.row(
        types.InlineKeyboardButton(
            text="🏠 Dashboard", callback_data="inbox_back_dashboard"
        )
    )

    return "\n".join(lines), builder.as_markup()
