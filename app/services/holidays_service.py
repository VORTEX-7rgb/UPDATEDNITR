"""Holiday calendar service — orchestrates Home.aspx calendar fetch and navigation.

Features a multi-tiered caching architecture:
  1. Persistent Global Cache (Disk & Memory):
     Academic holidays are identical for all 6,000+ NITR students. The calendar
     is persisted in `data/holidays_cache.json` and loaded into memory on startup.
     Requests for any cached month are served in 0ms without hitting NITRIS or
     taking gateway slots — even after full bot restarts!
  2. Per-User Navigation State:
     Retains the user's latest rendered HolidaysPage in memory for postback
     token continuity without bloating Telegram callback_data.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from app.config import IST
from app.db.database import async_session_factory
from app.db.models import User
from app.nitris.client import NitrisClient
from app.nitris.constants import HOLIDAYS_CALENDAR_EVENT_TARGET
from app.nitris.exceptions import (
    LoginError,
    LoginUnavailableError,
    CredentialsQuarantinedError,
    HolidaysParseError,
    AttendanceWorkflowError,
)
from app.nitris.gateway import NitrisCircuitOpenError
from app.nitris.holidays_parser import (
    HolidayEntry,
    HolidaysPage,
    parse_holidays_html,
)
from app.nitris.session_pool import with_pooled_session
from app.nitris.auth_gate import on_login_failure

logger = logging.getLogger(__name__)

# ── 1. Institute-Wide Global Month Cache (Persistent) ────────────────────────
# (year, month) -> (res_dict, HolidaysPage, expires_at_monotonic)
_global_month_cache: dict[tuple[int, int], tuple[dict, HolidaysPage, float]] = {}
GLOBAL_HOLIDAYS_CACHE_TTL = 86400.0 * 30  # 30 days

_CACHE_FILE = Path(__file__).resolve().parent.parent.parent / "data" / "holidays_cache.json"

# ── 2. Per-User Postback Continuity Cache ────────────────────────────────────
# user_id -> (HolidaysPage, expires_at_monotonic)
# NOTE: HolidaysPage carries raw_html. To strictly bound memory, we cap this
# dict at _USER_PAGES_MAX_ENTRIES and prune expired entries on every store.
_user_calendar_pages: dict[int, tuple[HolidaysPage, float]] = {}
_PAGE_CACHE_TTL = 900.0  # 15 minutes
_USER_PAGES_MAX_ENTRIES = 256  # Hard capacity cap


def _prune_user_pages() -> None:
    """Evict expired entries from _user_calendar_pages and enforce hard cap."""
    now = time.monotonic()
    expired = [uid for uid, (_, exp) in _user_calendar_pages.items() if exp <= now]
    for uid in expired:
        _user_calendar_pages.pop(uid, None)
    if len(_user_calendar_pages) > _USER_PAGES_MAX_ENTRIES:
        sorted_by_exp = sorted(_user_calendar_pages.items(), key=lambda kv: kv[1][1])
        for uid, _ in sorted_by_exp[: len(_user_calendar_pages) - _USER_PAGES_MAX_ENTRIES]:
            _user_calendar_pages.pop(uid, None)


def _store_user_page(user_id: int, page: HolidaysPage) -> None:
    """Safely store user page and immediately prune to prevent memory leaks."""
    _user_calendar_pages[user_id] = (page, time.monotonic() + _PAGE_CACHE_TTL)
    _prune_user_pages()


def serialize_holidays_page(page: HolidaysPage) -> dict:
    """Serialize HolidaysPage to a JSON-compatible dict for job payload transport."""
    return {
        "month": page.month,
        "year": page.year,
        "month_label": page.month_label,
        "holidays": [
            {
                "day": h.day,
                "name": h.name,
                "month": h.month,
                "year": h.year,
                "is_trailing": h.is_trailing,
            }
            for h in page.holidays
        ],
        "prev_event_argument": page.prev_event_argument,
        "next_event_argument": page.next_event_argument,
        "event_target": page.event_target,
        "raw_html": page.raw_html,
    }


def deserialize_holidays_page(data: dict) -> HolidaysPage:
    """Deserialize dict back to a HolidaysPage instance."""
    holidays = [
        HolidayEntry(
            day=h["day"],
            name=h["name"],
            month=h["month"],
            year=h["year"],
            is_trailing=h.get("is_trailing", False),
        )
        for h in data.get("holidays", [])
    ]
    return HolidaysPage(
        month=data["month"],
        year=data["year"],
        month_label=data["month_label"],
        holidays=holidays,
        prev_event_argument=data.get("prev_event_argument"),
        next_event_argument=data.get("next_event_argument"),
        event_target=data.get("event_target", HOLIDAYS_CALENDAR_EVENT_TARGET),
        raw_html=data.get("raw_html", ""),
    )


def _load_disk_cache() -> None:
    """Load persistent global holidays cache from disk into memory on startup."""
    if not _CACHE_FILE.exists():
        return
    try:
        raw = _CACHE_FILE.read_text(encoding="utf-8")
        data = json.loads(raw)
        for key, val in data.items():
            year, month = val["year"], val["month"]
            page = deserialize_holidays_page(val)
            res_dict = {
                "success": True,
                "error": None,
                "kind": "holidays",
                "month": val["month"],
                "year": val["year"],
                "month_label": val["month_label"],
                "holidays": val.get("holidays", []),
                "prev_available": val.get("prev_available", False),
                "next_available": val.get("next_available", False),
                "page": val,
            }
            _global_month_cache[(year, month)] = (
                res_dict,
                page,
                time.monotonic() + GLOBAL_HOLIDAYS_CACHE_TTL,
            )
        logger.info("Loaded %d holiday month(s) from persistent cache (%s)", len(_global_month_cache), _CACHE_FILE)
    except Exception as e:
        logger.warning("Failed to load holiday disk cache: %r", e)


def _save_disk_cache() -> None:
    """Persist the global month cache to disk."""
    try:
        _CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        dump_data = {}
        for (y, m), (res_dict, page, _) in _global_month_cache.items():
            dump_data[f"{y}-{m}"] = {
                "month": page.month,
                "year": page.year,
                "month_label": page.month_label,
                "holidays": [
                    {
                        "day": h.day,
                        "name": h.name,
                        "month": h.month,
                        "year": h.year,
                        "is_trailing": h.is_trailing,
                    }
                    for h in page.holidays
                ],
                "prev_available": bool(page.prev_event_argument),
                "next_available": bool(page.next_event_argument),
                "prev_event_argument": page.prev_event_argument,
                "next_event_argument": page.next_event_argument,
                "event_target": page.event_target,
                "raw_html": page.raw_html,
            }
        _CACHE_FILE.write_text(json.dumps(dump_data, indent=2), encoding="utf-8")
    except Exception as e:
        logger.warning("Failed to save holiday disk cache: %r", e)


# Populate in-memory cache on module boot
_load_disk_cache()


def get_cached_holidays(year: Optional[int] = None, month: Optional[int] = None) -> Optional[dict]:
    """Retrieve institute-wide cached holiday calendar data for a (year, month).

    If year and month are omitted, returns data for current IST date if cached.
    """
    if year is None or month is None:
        today = datetime.now(IST)
        year, month = today.year, today.month

    entry = _global_month_cache.get((year, month))
    if entry and entry[2] > time.monotonic():
        return entry[0]
    return None


def store_cached_holidays(page: HolidaysPage, res_dict: dict) -> None:
    """Store parsed HolidaysPage and render dict in the institute-wide cache and persist.

    The in-memory dict update is sync and atomic. The disk write is offloaded
    to a background thread via spawn_tracked so write_text never stalls the event loop.
    """
    _global_month_cache[(page.year, page.month)] = (
        res_dict,
        page,
        time.monotonic() + GLOBAL_HOLIDAYS_CACHE_TTL,
    )
    # Fire-and-forget the disk write on a background thread if event loop is running.
    try:
        import asyncio
        from app.utils import spawn_tracked
        loop = asyncio.get_running_loop()
        if loop.is_running():
            spawn_tracked(asyncio.to_thread(_save_disk_cache), name="holidays-disk-write")
        else:
            _save_disk_cache()
    except RuntimeError:
        # No running event loop (e.g. during import-time seed or unit tests) — write sync.
        _save_disk_cache()


def get_cached_user_page(user_id: int) -> Optional[HolidaysPage]:
    """Retrieve the cached HolidaysPage for a user if still valid."""
    entry = _user_calendar_pages.get(user_id)
    if entry and entry[1] > time.monotonic():
        return entry[0]
    _user_calendar_pages.pop(user_id, None)
    return None


async def fetch_user_holidays(
    user_id: int,
    direction: Optional[str] = None,
    current_page: Optional[HolidaysPage] = None,
    force_refresh: bool = False,
) -> dict:
    """Fetch current month holidays or navigate previous/next month.

    Cache-first:
      1. If force_refresh is False and this month is already in the global
         cache, returns immediately (0ms, zero portal traffic).
      2. If navigating (prev/next) and the target month is already cached,
         returns immediately (0ms).
      3. On cache miss or force_refresh: calls NITRIS, parses, and populates
         the global cache for all users.

    Args:
        user_id: Database user ID.
        direction: "prev", "next", or None (for current month).
        current_page: Pre-existing HolidaysPage for postback navigation. If None,
                      falls back to in-memory cached page for this user.
        force_refresh: If True, bypasses cache and forces fresh scrape from NITRIS.

    Returns:
        dict with success, error, month, year, month_label, holidays, prev_available,
        next_available, and page.
    """
    # Step 1: Validate registered user and credentials
    async with async_session_factory() as session:
        user = await session.get(User, user_id)
        if not user:
            return {"success": False, "error": "User not found"}
        if not user.credentials_valid:
            return {"success": False, "error": "Credentials marked invalid. Please /forgot to update."}
        roll_number = user.roll_number
        encrypted_password = user.encrypted_password

    # ── Fast-path 1: Current month cache hit ─────────────────────────────────
    if not force_refresh and direction is None:
        cached = get_cached_holidays()
        if cached:
            logger.info("Serving current month holidays from global cache (0ms)")
            # Maintain user's navigation page state
            entry = _global_month_cache.get((cached["year"], cached["month"]))
            if entry:
                _store_user_page(user_id, entry[1])
            return cached

    # If navigation was requested without an explicit current_page, check user's cache
    if direction and current_page is None:
        current_page = get_cached_user_page(user_id)

    # ── Fast-path 2: Navigated target month cache hit ─────────────────────────
    if not force_refresh and direction in ("prev", "next") and current_page:
        if direction == "next":
            t_month = current_page.month + 1 if current_page.month < 12 else 1
            t_year = current_page.year if current_page.month < 12 else current_page.year + 1
        else:
            t_month = current_page.month - 1 if current_page.month > 1 else 12
            t_year = current_page.year if current_page.month > 1 else current_page.year - 1

        cached_target = get_cached_holidays(t_year, t_month)
        if cached_target:
            logger.info("Serving navigated %s/%s holidays from global cache (0ms)", t_year, t_month)
            entry = _global_month_cache.get((t_year, t_month))
            if entry:
                _store_user_page(user_id, entry[1])
            return cached_target

    try:
        async def _work(client: NitrisClient, password: str) -> HolidaysPage:
            if direction and current_page:
                # CRITICAL: Re-GET Home.aspx on THIS pooled client to obtain a
                # session-matched __VIEWSTATE / __EVENTVALIDATION before posting
                # the calendar navigation postback. The pooled client may have a
                # DIFFERENT ASP.NET_SessionId than the session that originally
                # rendered current_page.raw_html — ASP.NET rejects postbacks
                # whose __EVENTVALIDATION doesn't match the active session.
                # We then walk to the target month from the fresh page.
                import asyncio as _asyncio
                fresh_html = await client.fetch_home_html()
                fresh_page = await _asyncio.to_thread(parse_holidays_html, fresh_html)
                target_label = _target_month_label(current_page, direction)
                if fresh_page.month_label.lower() == target_label.lower():
                    return fresh_page

                walker = fresh_page
                for _ in range(24):
                    step = _step_direction(walker, target_label)
                    if step is None:
                        return walker
                    try:
                        walker = await client.navigate_holidays_month(walker, step)
                        _cache_intermediate_page(walker)
                    except ValueError:
                        # Calendar boundary hit — return what we reached
                        return walker
                return walker
            return await client.fetch_holidays()

        page: HolidaysPage = await with_pooled_session(
            user_id=user_id,
            roll_number=roll_number,
            encrypted_password=encrypted_password,
            work=_work,
        )

        # Cache per-user navigation continuity (bounded + pruned)
        _store_user_page(user_id, page)

        res = {
            "success": True,
            "error": None,
            "kind": "holidays",
            "month": page.month,
            "year": page.year,
            "month_label": page.month_label,
            "holidays": [
                {
                    "day": h.day,
                    "name": h.name,
                    "month": h.month,
                    "year": h.year,
                    "is_trailing": h.is_trailing,
                }
                for h in page.holidays
            ],
            "prev_available": bool(page.prev_event_argument),
            "next_available": bool(page.next_event_argument),
            "page": serialize_holidays_page(page),
        }

        # Cache globally for all students and persist
        store_cached_holidays(page, res)
        return res

    except NitrisCircuitOpenError as e:
        return {
            "success": False,
            "error": "NITRIS is temporarily unavailable due to high portal load. Please try again in ~60 seconds.",
        }
    except LoginUnavailableError as e:
        logger.warning("fetch_user_holidays: NITRIS unavailable for user_id=%s: %r", user_id, e)
        return {
            "success": False,
            "error": "NITRIS is temporarily unreachable. Please try again in a few minutes.",
        }
    except (LoginError, CredentialsQuarantinedError) as e:
        await on_login_failure(user_id, str(e))
        return {
            "success": False,
            "error": "Login failed. Your NITRIS password may have changed. Please use /forgot to update it.",
        }
    except (HolidaysParseError, AttendanceWorkflowError, ValueError) as e:
        logger.warning("fetch_user_holidays error for user_id=%s: %r", user_id, e)
        return {"success": False, "error": str(e)}
    except Exception as e:
        logger.error("fetch_user_holidays unexpected error for user_id=%s: %r", user_id, e)
        return {"success": False, "error": f"Failed to load holidays: {e}"}


# ── Navigation & Walking Helpers ─────────────────────────────────────────────

def _cache_intermediate_page(page: HolidaysPage) -> None:
    """Store intermediate step in global cache so future queries hit 0ms."""
    res = {
        "success": True,
        "error": None,
        "kind": "holidays",
        "month": page.month,
        "year": page.year,
        "month_label": page.month_label,
        "holidays": [
            {
                "day": h.day,
                "name": h.name,
                "month": h.month,
                "year": h.year,
                "is_trailing": h.is_trailing,
            }
            for h in page.holidays
        ],
        "prev_available": bool(page.prev_event_argument),
        "next_available": bool(page.next_event_argument),
        "page": serialize_holidays_page(page),
    }
    store_cached_holidays(page, res)


def _target_month_label(current_page: HolidaysPage, direction: str) -> str:
    """Compute the target month label after a prev/next step from current_page."""
    import calendar as _cal
    m, y = current_page.month, current_page.year
    if direction.strip().lower() == "prev":
        m -= 1
        if m < 1:
            m = 12
            y -= 1
    else:
        m += 1
        if m > 12:
            m = 1
            y += 1
    return f"{_cal.month_name[m]} {y}"


def _step_direction(walker: HolidaysPage, target_label: str) -> Optional[str]:
    """Return 'prev'/'next' to step from walker toward target_label, or None
    if walker IS the target."""
    if walker.month_label.lower() == target_label.lower():
        return None
    if (walker.year, walker.month) < _label_to_ym(target_label):
        return "next"
    return "prev"


def _label_to_ym(label: str) -> tuple[int, int]:
    """Parse 'September 2026' -> (2026, 9). Returns (0, 0) on failure."""
    import re
    import calendar as _cal
    m = re.match(r"^\s*([A-Za-z]+)\s+(\d{4})\s*$", label)
    if not m:
        return (0, 0)
    month_name, year_str = m.group(1), m.group(2)
    for i in range(1, 13):
        if _cal.month_name[i].lower() == month_name.lower():
            return (int(year_str), i)
    return (0, 0)
