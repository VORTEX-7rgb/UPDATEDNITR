"""Timetable sync — pulls Home.aspx from NITRIS, parses, transactionally
replaces the stored weekly timetable.

Follows the same lease-boundary discipline as qpaper_service:

    Step 1: DB lookup (encrypted credential)       — OUTSIDE gateway.acquire()
    Step 2: gateway.acquire()                        — INSIDE
              decrypt_password()
              login_through_gateway()
              client.fetch_home_html()
              parse_home_page(html)
    Step 3: transactional DB replace                  — OUTSIDE
    Step 4: Telegram edit / send result               — OUTSIDE

Passwords are decrypted JUST-IN-TIME inside acquire() and go out of scope
when the handler returns. They are NEVER in the job payload, NEVER
serialized, NEVER logged.

Sync is MANUAL ONLY — triggered by /timetablesync command or the 📅 Sync
inline button. Per tier_1 in NITRIS_PORTAL_RECON.json, class_timetable
TTL = 7 days (only changes at semester boundary), but we override to
manual because the user wants explicit control.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from sqlalchemy import select

from app.config import config, IST
from app.db.database import async_session_factory, get_db_session
from app.db.models import User
from app.db.repositories.timetable_repository import TimetableRepository
from app.nitris.exceptions import (
    LoginError, SessionExpiredError, HomeParseError, NitrisError,
)
from app.nitris.parser import parse_home_page, TimetableSlot

logger = logging.getLogger(__name__)


class TimetableSyncError(Exception):
    """Raised when the timetable sync fails for any reason (login, parse, DB)."""

    def __init__(self, message: str, kind: str = "internal") -> None:
        super().__init__(message)
        self.kind = kind  # "login" | "parse" | "network" | "internal" | "circuit"


async def fetch_timetable_html_via_gateway(
    user_id: int,
    roll_number: str,
    encrypted_password: str,
) -> tuple[str, list[TimetableSlot]]:
    """Acquire a gateway slot, log in, fetch Home.aspx, parse timetable.

    Returns (raw_html, parsed_slots). The raw HTML is returned for snapshot
    purposes on downstream failure.

    All NITRIS-bound work happens INSIDE the gateway's acquire() block. The
    caller is responsible for DB writes (which happen OUTSIDE acquire() to
    respect the lease boundary — see qpaper_service pattern).
    """
    # Local imports to avoid circular import (gateway imports from app.nitris)
    from app.nitris.gateway import nitris_gateway, NitrisCircuitOpenError
    from app.nitris.client import NitrisClient
    from app.db.crypto import decrypt_password

    async with nitris_gateway.acquire():
        # JIT password decryption INSIDE acquire() — password never leaves
        # this scope, never serialized into job payload or logs.
        password = decrypt_password(encrypted_password)

        client = NitrisClient()
        try:
            await nitris_gateway.login_through_gateway(client, roll_number, password)
            html = await client.fetch_home_html()
            slots = parse_home_page(html).timetable
            return html, slots
        finally:
            await client.close()
            # password drops out of scope here


async def sync_user_timetable(
    user_id: int,
    callback_chat_id: Optional[int] = None,
    callback_message_id: Optional[int] = None,
) -> dict:
    """Top-level sync entry point — used by the timetable_sync job handler.

    Returns a dict: {success: bool, error: str | None, entry_count: int,
    synced_at_ist: str | None, fresh_html_bytes: int}.

    The job handler (app.nitris.job_handlers.handle_timetable_sync) calls
    this and then formats the Telegram reply. Bot concerns stay out of
    this service.
    """
    # ── Step 1: DB lookup (encrypted credentials) — OUTSIDE gateway ────
    try:
        async with async_session_factory() as session:
            user = await session.get(User, user_id)
            if not user:
                return {
                    "success": False,
                    "error": "User not found",
                    "kind": "internal",
                    "entry_count": 0,
                }
            if not user.credentials_valid:
                return {
                    "success": False,
                    "error": "Credentials marked invalid. Use /forgot to update.",
                    "kind": "login",
                    "entry_count": 0,
                }
            roll_number = user.roll_number
            encrypted_password = user.encrypted_password
    except Exception as e:
        logger.error("timetable_sync: DB lookup failed for user_id=%d: %r", user_id, e)
        return {
            "success": False,
            "error": f"DB lookup failed: {e}",
            "kind": "internal",
            "entry_count": 0,
        }

    # ── Step 2: NITRIS work (decrypt + login + fetch + parse) — INSIDE gateway ──
    try:
        _html, slots = await fetch_timetable_html_via_gateway(
            user_id, roll_number, encrypted_password,
        )
    except NitrisCircuitOpenError as e:
        return {
            "success": False,
            "error": "NITRIS temporarily unavailable (circuit open). Retry in ~60s.",
            "kind": "circuit",
            "entry_count": 0,
        }
    except LoginError as e:
        # Mark credentials invalid so the user gets the /forgot prompt
        from app.nitris.job_handlers import _mark_credentials_invalid
        await _mark_credentials_invalid(user_id, str(e))
        return {
            "success": False,
            "error": f"Login failed — credentials may have changed. {e}",
            "kind": "login",
            "entry_count": 0,
        }
    except HomeParseError as e:
        # Per-user parse failure (NITRIS changed markup, empty dashboard).
        # Does NOT trip the circuit (HomeParseError is a NitrisError but the
        # gateway still records the error; per-user faults like stale cookies
        # already skip the circuit via the LoginError branch).
        logger.error("timetable_sync: parse failed for user_id=%d: %r", user_id, e)
        return {
            "success": False,
            "error": f"Could not parse the timetable from NITRIS. {e}",
            "kind": "parse",
            "entry_count": 0,
        }
    except (SessionExpiredError, NitrisError) as e:
        logger.error("timetable_sync: NITRIS workflow failed for user_id=%d: %r", user_id, e)
        return {
            "success": False,
            "error": f"NITRIS workflow error: {e}",
            "kind": "network",
            "entry_count": 0,
        }
    except Exception as e:
        logger.exception("timetable_sync: unexpected error for user_id=%d", user_id)
        return {
            "success": False,
            "error": f"Unexpected error: {e}",
            "kind": "internal",
            "entry_count": 0,
        }

    # ── Step 3: transactional DB replace — OUTSIDE gateway ─────────────
    synced_at = datetime.now(IST)  # tz-aware IST — never datetime.now() bare
    try:
        async with get_db_session() as session:
            async with session.begin():
                repo = TimetableRepository(session)
                count = await repo.replace_user_timetable(
                    user_id=user_id, slots=slots, synced_at=synced_at,
                )
    except Exception as e:
        logger.error("timetable_sync: DB replace failed for user_id=%d: %r", user_id, e)
        return {
            "success": False,
            "error": f"Failed to save timetable: {e}",
            "kind": "internal",
            "entry_count": 0,
        }

    # ── Step 4: optional module_sync_schedule update — OUTSIDE gateway ─
    # Update the schedule row's last_synced_at so the admin /status dashboard
    # reflects the manual sync. Does NOT enqueue the next auto-sync (manual
    # only — we set last_status="manual" to distinguish from auto-synced
    # modules like attendance/inbox).
    try:
        from datetime import timedelta
        async with get_db_session() as session:
            async with session.begin():
                from app.db.models import ModuleSyncSchedule
                stmt = select(ModuleSyncSchedule).where(
                    ModuleSyncSchedule.user_id == user_id,
                    ModuleSyncSchedule.module_name == "timetable",
                )
                result = await session.execute(stmt)
                schedule = result.scalar_one_or_none()
                far_future = synced_at + timedelta(days=3650)
                if schedule is None:
                    # First sync ever for this user — create tracking row (far future next_sync_at)
                    schedule = ModuleSyncSchedule(
                        user_id=user_id,
                        module_name="timetable",
                        last_synced_at=synced_at,
                        next_sync_at=far_future,
                        last_status="manual",
                        consecutive_failures=0,
                    )
                    session.add(schedule)
                else:
                    schedule.last_synced_at = synced_at
                    schedule.next_sync_at = far_future
                    schedule.last_status = "manual"
                    schedule.consecutive_failures = 0
                    schedule.last_error = None
    except Exception as e:
        # Non-fatal: the timetable itself was saved. Just log.
        logger.warning("timetable_sync: could not update schedule row: %r", e)

    return {
        "success": True,
        "error": None,
        "kind": None,
        "entry_count": count,
        "synced_at_ist": synced_at.strftime("%d %b %Y, %H:%M IST"),
        "fresh_html_bytes": len(_html) if _html else 0,
        "callback_chat_id": callback_chat_id,
        "callback_message_id": callback_message_id,
    }
