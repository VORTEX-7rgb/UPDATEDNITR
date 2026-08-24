"""NITRIS job handlers — the actual work that runs inside the gateway.

CRITICAL: Passwords are decrypted HERE, inside the gateway's acquire() block,
used for the NITRIS login, and go out of scope when the handler returns.
They are NEVER in the job payload, NEVER serialized, NEVER logged.

Each handler:
1. Acquires a gateway slot (concurrency cap + circuit breaker)
2. Looks up user credentials from DB (INSIDE the gateway)
3. Decrypts the password (INSIDE the gateway)
4. Creates a NitrisClient, logs in through the gateway
5. Does the NITRIS work
6. Closes the client
7. Persists results to DB
8. Edits the user's Telegram message with the result (if callback provided)
"""
from __future__ import annotations

import asyncio
import logging
import html
import time
from datetime import datetime, timezone
from typing import Optional, Any

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.bot.telegram import format_attendance_message
from app.config import config, IST
from app.db.crypto import decrypt_password
from app.db.database import get_db_session, async_session_factory
from app.db.models import User, SyncState, InboxMessage, Snapshot
from app.db.repositories.snapshot_repository import SnapshotRepository
from app.db.repositories.inbox_repository import InboxRepository
from app.db.repositories.event_repository import EventRepository
from app.nitris.gateway import nitris_gateway, NitrisCircuitOpenError
from app.nitris.job_queue import nitris_job_queue, NitrisJob, Priority
from app.nitris.client import NitrisClient
from app.nitris.exceptions import (
    LoginError, LoginUnavailableError, SessionExpiredError, AttendanceParseError,
    AttendanceWorkflowError, NitrisError, CredentialsQuarantinedError,
)
from app.services.attendance_service import get_attendance_data
from app.services.snapshot_service import SnapshotService
from app.workers.sync_worker import prepare_inbox_sync, persist_inbox_sync
from app.nitris.auth_gate import on_login_failure
from app.utils import esc, spawn_tracked

from aiogram.enums import ParseMode

logger = logging.getLogger(__name__)

# Bot instance — set on startup by init_job_handlers()
_bot = None


def init_job_handlers(bot) -> None:
    """Register all NITRIS job handlers. Call once on startup."""
    global _bot
    _bot = bot

    nitris_job_queue.register_handler("attendance_refresh", handle_attendance_refresh)
    nitris_job_queue.register_handler("inbox_refresh", handle_inbox_refresh)
    nitris_job_queue.register_handler("sync_onboarding", handle_sync_onboarding)
    nitris_job_queue.register_handler("qp_metadata_fetch", handle_qp_metadata_fetch)
    # Phase 6: additional handlers for previously-bypassed paths
    nitris_job_queue.register_handler("inbox_detail_fetch", handle_inbox_detail_fetch)
    nitris_job_queue.register_handler("attachment_download", handle_attachment_download)
    nitris_job_queue.register_handler("qp_search", handle_qp_search)
    nitris_job_queue.register_handler("timetable_sync", handle_timetable_sync)
    nitris_job_queue.register_handler("qp_prewarm_subject", handle_qp_prewarm_subject)
    nitris_job_queue.register_handler("session_warm", handle_session_warm)

    logger.info(
        "Registered NITRIS job handlers: %s",
        nitris_job_queue.get_registered_handlers(),
    )


# ── Handler: attendance_refresh ─────────────────────────────────────

async def handle_attendance_refresh(job: NitrisJob) -> dict:
    """Fetch fresh attendance from NITRIS, save snapshot, edit user's message.

    Payload:
        - callback_chat_id: int (Telegram chat ID to edit)
        - callback_message_id: int (Telegram message ID to edit)

    Returns:
        dict with keys: success, data (AttendanceResult or None), error (str or None)
    """
    user_id = job.user_id
    callback_chat_id = job.payload.get("callback_chat_id")
    callback_message_id = job.payload.get("callback_message_id")

    # PERF instrumentation: one line showing where the time went.
    _t0 = time.monotonic()

    # ── Step 1: DB lookup (encrypted credential) — OUTSIDE gateway ────
    async with async_session_factory() as session:
        user = await session.get(User, user_id)
        if not user:
            return {"success": False, "error": "User not found", "data": None}
        if not user.credentials_valid:
            return {
                "success": False,
                "error": "Credentials marked invalid. Please /forgot to update.",
                "data": None,
            }
        roll_number = user.roll_number
        encrypted_password = user.encrypted_password
    _t_db = time.monotonic()

    # ── Step 2: NITRIS work — pooled authenticated session (PERF P1) ────
    try:
        from app.nitris.session_pool import with_pooled_session

        async def _work(client, password):
            return await get_attendance_data(roll_number, password, client=client, user_id=user_id)

        data = await with_pooled_session(
            user_id=user_id,
            roll_number=roll_number,
            encrypted_password=encrypted_password,
            work=_work,
        )
        _t_nitris = time.monotonic()
        logger.info(
            "⏱ attendance_refresh user=%s db=%dms nitris=%dms",
            roll_number,
            int((_t_db - _t0) * 1000),
            int((_t_nitris - _t_db) * 1000),
        )

    except NitrisCircuitOpenError as e:
        await _edit_callback_message(
            callback_chat_id, callback_message_id,
            "⚠️ <b>NITRIS is temporarily unavailable.</b>\n\n"
            "The system is protecting the portal from overload. "
            "Please try again in ~60 seconds.",
        )
        return {"success": False, "error": str(e), "data": None}

    except LoginUnavailableError as e:
        # Portal down/misbehaving — NOT a credential problem (H1 fix).
        # Never quarantine on this; surface the same "try later" UX.
        logger.warning("attendance_refresh: NITRIS unavailable for user_id=%s: %r", user_id, e)
        await _edit_callback_message(
            callback_chat_id, callback_message_id,
            "⚠️ <b>NITRIS is temporarily unavailable.</b>\n\n"
            "Could not reach the portal right now. "
            "Please try again in a few minutes.",
        )
        return {"success": False, "error": f"NITRIS temporarily unavailable: {e}", "data": None}

    except (LoginError, CredentialsQuarantinedError) as e:
        # Mark credentials as invalid
        await on_login_failure(user_id, str(e))
        await _edit_callback_message(
            callback_chat_id, callback_message_id,
            f"❌ <b>Login failed.</b>\n\n"
            f"Your NITRIS credentials may have changed. "
            f"Please use /forgot to update them.\n\n"
            f"<i>Error: {html.escape(str(e))}</i>",
        )
        return {"success": False, "error": f"Login failed: {e}", "data": None}

    except (AttendanceWorkflowError, AttendanceParseError) as e:
        await _update_sync_state(user_id, success=False, error_msg=str(e))
        await _edit_callback_message(
            callback_chat_id, callback_message_id,
            f"❌ <b>Could not fetch attendance.</b>\n\n"
            f"The NITRIS portal may be experiencing issues. Please try again later.\n\n"
            f"<i>Error: {html.escape(str(e)[:200])}</i>",
        )
        return {"success": False, "error": str(e), "data": None}

    # ── Step 3 (LAYER 2): render the fresh list FIRST — before any DB writes ──
    # PERF (render-first): the student's bubble updates the instant fresh data
    # is in memory. Snapshot + SyncState persistence moved to a tracked
    # background task so two sequential DB transactions no longer sit on the
    # perceived-latency path. Persistence failures remain non-fatal and are
    # logged exactly as before.
    if callback_chat_id and callback_message_id:
        try:
            from app.bot.handlers.attendance import (
                _list_text as _att_list_text,
                _kb_viewing as _att_kb_viewing,
                summarize as _att_summarize,
            )
            records = (data.to_dict() or {}).get("records") or []
            fresh_summary = _att_summarize(records)
            await _edit_callback_message(
                callback_chat_id, callback_message_id,
                _att_list_text(fresh_summary, "🟢 Updated just now."),
                reply_markup=_att_kb_viewing(fresh_summary),
            )
        except Exception as e:
            logger.warning("attendance_refresh: success self-render failed: %r", e)

    # ── Step 4: persist snapshot + sync state — OFF the perceived path ────
    def _spawn_persist() -> None:
        async def _persist() -> None:
            try:
                async with get_db_session() as session:
                    async with session.begin():
                        snapshot_service = SnapshotService(session)
                        await snapshot_service.create_snapshot_if_changed(
                            user_id=user_id,
                            module_name="attendance",
                            attendance_result=data,
                        )
                # Update sync_state to reflect this manual refresh
                await _update_sync_state(user_id, success=True)
            except Exception as e:
                logger.error("Failed to save attendance snapshot: %r", e)
        spawn_tracked(_persist(), name=f"att-persist-{user_id}")

    _spawn_persist()

    return {
        "success": True,
        "data": data,
        "error": None,
        "callback_chat_id": callback_chat_id,
        "callback_message_id": callback_message_id,
    }


# ── Handler: inbox_refresh ──────────────────────────────────────────

async def handle_inbox_refresh(job: NitrisJob) -> dict:
    """Sync inbox messages from NITRIS for a user.

    Payload:
        - callback_chat_id: int (optional, for editing message)
        - callback_message_id: int (optional)
    """
    user_id = job.user_id
    callback_chat_id = job.payload.get("callback_chat_id")
    callback_message_id = job.payload.get("callback_message_id")

    # ── Step 1: DB lookup — OUTSIDE gateway ──
    try:
        async with async_session_factory() as session:
            user = await session.get(User, user_id)
            if not user:
                return {"success": False, "error": "User not found"}
            if not user.credentials_valid:
                return {"success": False, "error": "Credentials invalid"}
            roll_number = user.roll_number
            encrypted_password = user.encrypted_password
    except Exception as e:
        logger.error("inbox_refresh DB lookup failed: %r", e)
        return {"success": False, "error": f"DB lookup failed: {e}"}

    # ── Step 2: NITRIS work — pooled authenticated session (PERF P1) ──
    try:
        from app.nitris.session_pool import with_pooled_session
        from app.workers.sync_worker import wait_for_db_recovery

        # PERF (lease hygiene): if the DB is down, wait HERE — before taking a
        # scarce pooled portal session. Waiting inside the lease used to pin
        # an authenticated NITRIS session for minutes while looping on SELECT 1.
        await wait_for_db_recovery(f"Inbox-Refresh-{user_id}")

        async def _inbox_work(client, password):
            scraped, detail_cache, existing_by_id = await prepare_inbox_sync(client, user_id)
            return scraped, detail_cache, existing_by_id

        scraped, detail_cache, existing_by_id = await with_pooled_session(
            user_id=user_id,
            roll_number=roll_number,
            encrypted_password=encrypted_password,
            work=_inbox_work,
        )

        # DB write -- OUTSIDE the gateway lock (lease boundary fix)
        await persist_inbox_sync(user_id, scraped, detail_cache, existing_by_id)

        return {"success": True, "error": None}

    except NitrisCircuitOpenError as e:
        return {"success": False, "error": str(e)}
    except LoginUnavailableError as e:
        # Portal down/misbehaving — NOT a credential problem (H1 fix).
        logger.warning("inbox_refresh: NITRIS unavailable for user_id=%s: %r", user_id, e)
        return {"success": False, "error": f"NITRIS temporarily unavailable: {e}"}
    except (LoginError, CredentialsQuarantinedError) as e:
        await on_login_failure(user_id, str(e))
        return {"success": False, "error": f"Login failed: {e}"}
    except Exception as e:
        logger.error("inbox_refresh job failed: %r", e)
        return {"success": False, "error": str(e)}


# ── Handler: sync_onboarding (post-registration baseline prefetch) ──

async def handle_sync_onboarding(job: NitrisJob) -> dict:
    """Silent post-registration baseline sync: inbox + timetable on ONE login.

    Runs in the background right after a user registers so that:
      - the inbox cache is warm (first tap is instant), and
      - historical messages are inserted WITHOUT "new message" notifications
        (baseline=True), so a fresh user isn't spammed with their whole
        NITRIS backlog.

    Payload: none (user_id comes from the job). Inbox and timetable are
    scraped/persisted independently — a failure in one can't break the other.
    """
    user_id = job.user_id

    # ── Phase 1: DB lookup — OUTSIDE gateway ──
    try:
        async with async_session_factory() as session:
            user = await session.get(User, user_id)
            if not user or not user.credentials_valid:
                return {"success": False, "error": "user_invalid_or_missing"}
            roll_number = user.roll_number
            encrypted_password = user.encrypted_password
    except Exception as e:
        logger.error("sync_onboarding DB lookup failed for user_id=%d: %r", user_id, e)
        return {"success": False, "error": str(e)}

    # ── Phase 2: pooled session — inbox scrape, then timetable (PERF P1) ──
    scraped = detail_cache = existing_by_id = None
    slots = None
    inbox_scrape_error: Optional[str] = None
    timetable_scrape_error: Optional[str] = None
    try:
        from app.nitris.session_pool import with_pooled_session
        from app.workers.sync_worker import wait_for_db_recovery

        # PERF (lease hygiene): DB-recovery wait happens BEFORE the portal
        # session lease — never hold a pooled NITRIS session during a DB outage.
        await wait_for_db_recovery(f"Onboarding-{user_id}")

        async def _inbox_work(client, password):
            return await prepare_inbox_sync(client, user_id)

        try:
            scraped, detail_cache, existing_by_id = await with_pooled_session(
                user_id=user_id,
                roll_number=roll_number,
                encrypted_password=encrypted_password,
                work=_inbox_work,
            )
        except Exception as e:
            inbox_scrape_error = str(e)
            logger.warning("sync_onboarding inbox scrape failed for user_id=%d: %r", user_id, e)

        async def _tt_work(client, password):
            from app.nitris.parser import parse_home_page
            home_html = await client.fetch_home_html()
            parsed = await asyncio.to_thread(parse_home_page, home_html)
            return parsed.timetable

        try:
            slots = await with_pooled_session(
                user_id=user_id,
                roll_number=roll_number,
                encrypted_password=encrypted_password,
                work=_tt_work,
            )
        except Exception as e:
            timetable_scrape_error = str(e)
            logger.warning("sync_onboarding timetable scrape failed for user_id=%d: %r", user_id, e)
    except NitrisCircuitOpenError as e:
        return {"success": False, "error": str(e)}
    except (LoginError, CredentialsQuarantinedError) as e:
        await on_login_failure(user_id, str(e))
        return {"success": False, "error": f"Login failed: {e}"}
    except Exception as e:
        logger.error("sync_onboarding NITRIS work failed for user_id=%d: %r", user_id, e)
        return {"success": False, "error": str(e)}

    # ── Phase 3: persist each module independently — OUTSIDE gateway ──
    inbox_persist_error: Optional[str] = None
    if scraped is not None:
        try:
            await persist_inbox_sync(
                user_id, scraped, detail_cache or {}, existing_by_id or {}, baseline=True,
            )
        except Exception as e:
            inbox_persist_error = str(e)
            logger.error("sync_onboarding inbox persist failed for user_id=%d: %r", user_id, e)

    timetable_persist_error: Optional[str] = None
    if slots is not None:
        try:
            from app.db.repositories.timetable_repository import TimetableRepository
            from app.config import IST
            synced_at = datetime.now(IST)
            async with get_db_session() as session:
                async with session.begin():
                    repo = TimetableRepository(session)
                    await repo.replace_user_timetable(user_id=user_id, slots=slots, synced_at=synced_at)
        except Exception as e:
            timetable_persist_error = str(e)
            logger.error("sync_onboarding timetable persist failed for user_id=%d: %r", user_id, e)

    # ── Phase 4: enqueue retry jobs for failed modules (best-effort) ──
    if inbox_scrape_error or inbox_persist_error:
        try:
            await nitris_job_queue.enqueue(
                job_type="inbox_refresh",
                user_id=user_id,
                priority=Priority.LOW,
                dedup_key=f"onboarding_retry_inbox:user:{user_id}",
                payload={},
            )
            logger.info("sync_onboarding enqueued inbox retry for user_id=%d", user_id)
        except Exception as e:
            logger.warning("Failed to enqueue inbox retry: %r", e)

    if timetable_scrape_error or timetable_persist_error:
        try:
            await nitris_job_queue.enqueue(
                job_type="timetable_sync",
                user_id=user_id,
                priority=Priority.LOW,
                dedup_key=f"onboarding_retry_timetable:user:{user_id}",
                payload={},
            )
            logger.info("sync_onboarding enqueued timetable retry for user_id=%d", user_id)
        except Exception as e:
            logger.warning("Failed to enqueue timetable retry: %r", e)

    return {
        "success": True,
        "modules": {
            "inbox": {
                "scrape_error": inbox_scrape_error,
                "persist_error": inbox_persist_error,
            },
            "timetable": {
                "scrape_error": timetable_scrape_error,
                "persist_error": timetable_persist_error,
            },
        },
    }


# ── Handler: qp_metadata_fetch ──────────────────────────────────────

async def handle_qp_metadata_fetch(job: NitrisJob) -> dict:
    """Fetch QP metadata from NITRIS for a (subject, year) tuple.

    This handler supports SINGLE-FLIGHT DEDUP: if 100 students request the
    same metadata simultaneously, only ONE NITRIS call happens.

    Payload:
        - roll_number: str (for the NITRIS login)
        - academic_year: str (e.g. "2024-25/Autumn")
        - subject_code: str (e.g. "CS2001")

    ARCHITECTURE (lease boundary fix):
        DB lookup is OUTSIDE acquire(). Only decrypt → login → NITRIS HTTP
        → client.close is inside the gateway.
    """
    user_id = job.user_id
    academic_year = job.payload.get("academic_year", "")
    subject_code = job.payload.get("subject_code", "")

    # ── Step 1: DB lookup — OUTSIDE gateway ──
    try:
        async with async_session_factory() as session:
            user = await session.get(User, user_id)
            if not user:
                return {"success": False, "error": "User not found"}
            if not user.credentials_valid:
                return {"success": False, "error": "Credentials invalid"}
            roll_number = user.roll_number
            encrypted_password = user.encrypted_password
    except Exception as e:
        logger.error("qp_metadata_fetch DB lookup failed: %r", e)
        return {"success": False, "error": f"DB lookup failed: {e}"}

    # ── Step 2: NITRIS work — pooled authenticated session (PERF P1) ────
    # NOTE: a dead `from app.nitris.parser import parse_question_papers_html`
    # used to live here and raised ImportError on every batch metadata fetch
    # (the name lives in examination_parser; this function never used it).
    try:
        from app.nitris.session_pool import with_pooled_session

        async def _work(client, password):
            from app.services.examination_service import ExaminationService

            parsed_records = await ExaminationService.fetch_subject_metadata_from_portal(
                username=roll_number,
                password=password,
                academic_year=academic_year,
                subject_code=subject_code,
                client=client,
            )
            return parsed_records

        parsed_records = await with_pooled_session(
            user_id=user_id,
            roll_number=roll_number,
            encrypted_password=encrypted_password,
            work=_work,
        )

        return {
            "success": True,
            "parsed_records": parsed_records,
            "academic_year": academic_year,
            "subject_code": subject_code,
        }

    except NitrisCircuitOpenError as e:
        return {"success": False, "error": str(e)}
    except LoginUnavailableError as e:
        # Portal down/misbehaving — NOT a credential problem (H1 fix).
        logger.warning("qp_metadata_fetch: NITRIS unavailable for user_id=%s: %r", user_id, e)
        return {"success": False, "error": f"NITRIS temporarily unavailable: {e}"}
    except (LoginError, CredentialsQuarantinedError) as e:
        await on_login_failure(user_id, str(e))
        return {"success": False, "error": f"Login failed: {e}"}
    except Exception as e:
        logger.error("qp_metadata_fetch job failed: %r", e)
        return {"success": False, "error": str(e)}


# ── Handler: inbox_detail_fetch (Phase 6 — lazy load message body) ──

async def handle_inbox_detail_fetch(job: NitrisJob) -> dict:
    """Fetch a single inbox message body from NITRIS (lazy load).

    Payload:
        - message_id: int (InboxMessage.id)

    Returns:
        dict with success, body, attachment_url
    """
    user_id = job.user_id
    message_id = job.payload.get("message_id")

    if not message_id:
        return {"success": False, "error": "message_id required"}

    # ── Step 1: Look up user & token — OUTSIDE gateway ─────────────────
    async with async_session_factory() as session:
        user = await session.get(User, user_id)
        if not user:
            return {"success": False, "error": "User not found"}
        if not user.credentials_valid:
            return {"success": False, "error": "Credentials invalid"}
        roll_number = user.roll_number
        encrypted_password = user.encrypted_password

        # Load the message to get its token
        from sqlalchemy import select as sa_select
        stmt = sa_select(InboxMessage).where(
            InboxMessage.id == message_id,
            InboxMessage.user_id == user_id,
        )
        msg = (await session.execute(stmt)).scalar_one_or_none()
        if not msg:
            return {"success": False, "error": "Message not found"}
        token = msg.token

    # ── Step 2: NITRIS work — pooled authenticated session (PERF P1) ─────
    real_token = None
    detail_data = None
    try:
        from app.nitris.session_pool import with_pooled_session
        from app.nitris.parser import parse_message_detail_html

        async def _work(client, password):
            if token.startswith("postback:"):
                event_target = token.split("postback:")[1]
                rt, detail_html = await client.submit_message_postback(event_target)
                return rt, parse_message_detail_html(detail_html)
            detail_html = await client.fetch_message_detail(token)
            return None, parse_message_detail_html(detail_html)

        real_token, detail_data = await with_pooled_session(
            user_id=user_id,
            roll_number=roll_number,
            encrypted_password=encrypted_password,
            work=_work,
        )
    except NitrisCircuitOpenError as e:
        return {"success": False, "error": str(e)}
    except LoginUnavailableError as e:
        # Portal down/misbehaving — NOT a credential problem (H1 fix).
        logger.warning("inbox_detail_fetch: NITRIS unavailable for user_id=%s: %r", user_id, e)
        return {"success": False, "error": f"NITRIS temporarily unavailable: {e}"}
    except (LoginError, CredentialsQuarantinedError) as e:
        await on_login_failure(user_id, str(e))
        return {"success": False, "error": f"Login failed: {e}"}
    except Exception as e:
        logger.error("inbox_detail_fetch job failed: %r", e)
        return {"success": False, "error": str(e)}

    # ── Step 3: DB update — OUTSIDE gateway slot ────────────────────────
    try:
        async with get_db_session() as update_session:
            async with update_session.begin():
                if token.startswith("postback:") and real_token:
                    from app.nitris.parser import extract_message_id
                    from sqlalchemy import func as sa_func
                    portal_id = extract_message_id(real_token)
                    from sqlalchemy import update as sqlalchemy_update
                    update_values = {
                        "token": real_token,
                        "body": detail_data["body"],
                        "body_fetched_at": sa_func.now(),
                        "attachment_url": detail_data["attachment_url"],
                    }
                    if portal_id:
                        update_values["portal_message_id"] = portal_id
                    stmt = (
                        sqlalchemy_update(InboxMessage)
                        .where(InboxMessage.id == message_id)
                        .values(**update_values)
                    )
                    await update_session.execute(stmt)
                else:
                    from app.db.repositories.inbox_repository import InboxRepository
                    up_inbox_repo = InboxRepository(update_session)
                    await up_inbox_repo.update_message_body(
                        message_id=message_id,
                        body=detail_data["body"],
                        attachment_url=detail_data["attachment_url"],
                    )
    except Exception as e:
        logger.error("Failed updating inbox message in DB: %r", e)

    return {
        "success": True,
        "body": detail_data["body"] if detail_data else "",
        "attachment_url": detail_data["attachment_url"] if detail_data else None,
    }


# ── Handler: attachment_download (Phase 6) ──────────────────────────

async def handle_attachment_download(job: NitrisJob) -> dict:
    """Download an inbox attachment from NITRIS and upload to Telegram.

    Payload:
        - message_id: int (InboxMessage.id)
        - callback_chat_id: int (Telegram chat to send the file to)

    Returns:
        dict with success, file_id (Telegram file_id if uploaded), error

    ARCHITECTURE (lease boundary fix):
        DB lookups, Telegram upload, and DB file_id caching are all OUTSIDE
        acquire(). Only decrypt → login → NITRIS download → client.close
        is inside the gateway.
    """
    user_id = job.user_id
    message_id = job.payload.get("message_id")
    callback_chat_id = job.payload.get("callback_chat_id")

    if not message_id or not callback_chat_id:
        return {"success": False, "error": "message_id and callback_chat_id required"}

    # ── Step 1: DB lookups — OUTSIDE gateway ──
    try:
        async with async_session_factory() as session:
            user = await session.get(User, user_id)
            if not user:
                return {"success": False, "error": "User not found"}
            if not user.credentials_valid:
                return {"success": False, "error": "Credentials invalid"}
            roll_number = user.roll_number
            encrypted_password = user.encrypted_password

            from sqlalchemy import select as sa_select
            stmt = sa_select(InboxMessage).where(
                InboxMessage.id == message_id,
                InboxMessage.user_id == user_id,
            )
            msg = (await session.execute(stmt)).scalar_one_or_none()
            if not msg or not msg.attachment_url:
                return {"success": False, "error": "Message or attachment not found"}
            attachment_url = msg.attachment_url
            subject = msg.subject
    except Exception as e:
        logger.error("attachment_download DB lookup failed: %r", e)
        return {"success": False, "error": f"DB lookup failed: {e}"}

    # ── Step 2: NITRIS download — pooled authenticated session (PERF P1) ──
    file_bytes = None
    try:
        from app.nitris.session_pool import with_pooled_session

        async def _work(client, password):
            return await client.download_attachment(attachment_url)

        file_bytes = await with_pooled_session(
            user_id=user_id,
            roll_number=roll_number,
            encrypted_password=encrypted_password,
            work=_work,
        )

    except NitrisCircuitOpenError as e:
        return {"success": False, "error": str(e)}
    except LoginUnavailableError as e:
        # Portal down/misbehaving — NOT a credential problem (H1 fix).
        logger.warning("attachment_download: NITRIS unavailable for user_id=%s: %r", user_id, e)
        return {"success": False, "error": f"NITRIS temporarily unavailable: {e}"}
    except (LoginError, CredentialsQuarantinedError) as e:
        await on_login_failure(user_id, str(e))
        return {"success": False, "error": f"Login failed: {e}"}
    except Exception as e:
        logger.error("attachment_download NITRIS work failed: %r", e)
        return {"success": False, "error": str(e)}

    # ── Step 3: Telegram upload + DB caching — OUTSIDE gateway ──
    try:
        # Check 50MB Telegram limit
        MAX_FILE_SIZE = 50 * 1024 * 1024
        if len(file_bytes) > MAX_FILE_SIZE:
            return {
                "success": False,
                "error": "Attachment too large (>50MB) for Telegram upload.",
            }

        # Sanitize filename
        import re
        sanitized_subject = re.sub(r'[^a-zA-Z0-9_\- ]', '', subject)
        sanitized_subject = re.sub(r'\s+', ' ', sanitized_subject).strip()
        if not sanitized_subject:
            sanitized_subject = f"notice_attachment_{message_id}"
        filename = f"{sanitized_subject[:50]}.pdf"

        from aiogram.types import BufferedInputFile
        input_file = BufferedInputFile(file_bytes, filename=filename)

        sent_message = await _bot.send_document(
            chat_id=callback_chat_id,
            document=input_file,
        )

        if sent_message.document:
            file_id = sent_message.document.file_id
            # Cache the file_id
            async with get_db_session() as update_session:
                async with update_session.begin():
                    from app.db.repositories.inbox_repository import InboxRepository
                    up_inbox_repo = InboxRepository(update_session)
                    await up_inbox_repo.update_telegram_file_id(message_id, file_id)

            return {"success": True, "file_id": file_id}

        return {"success": False, "error": "Telegram upload returned no document file_id"}

    except Exception as e:
        logger.error("attachment_download Telegram/DB phase failed: %r", e)
        return {"success": False, "error": str(e)}


# ── Handler: qp_search (Phase 6 — live QP subject search) ──────────

async def handle_qp_search(job: NitrisJob) -> dict:
    """Search NITRIS for question paper subjects matching a query.

    Payload:
        - query: str (search term)
        - academic_year: str (optional, defaults to "2024-25")

    Returns:
        dict with success, records (list of QuestionPaperRecord)

    ARCHITECTURE (lease boundary fix):
        DB lookup is OUTSIDE acquire(). Only decrypt → login → NITRIS HTTP
        → client.close is inside the gateway.
    """
    user_id = job.user_id
    query = job.payload.get("query", "")

    if len(query) < 2:
        return {"success": False, "error": "Query too short"}

    # ── Step 1: DB lookup — OUTSIDE gateway ──
    try:
        async with async_session_factory() as session:
            user = await session.get(User, user_id)
            if not user:
                return {"success": False, "error": "User not found"}
            if not user.credentials_valid:
                return {"success": False, "error": "Credentials invalid"}
            roll_number = user.roll_number
            encrypted_password = user.encrypted_password
    except Exception as e:
        logger.error("qp_search DB lookup failed: %r", e)
        return {"success": False, "error": f"DB lookup failed: {e}"}

    # ── Step 2: NITRIS work — pooled authenticated session (PERF P1) ──
    search_records = None
    try:
        from app.nitris.session_pool import with_pooled_session
        from app.nitris.examination_parser import parse_question_papers_html
        from app.utils import current_academic_year

        ay = current_academic_year()

        async def _work(client, password):
            records = []
            try:
                html_autumn = await client.fetch_question_papers(
                    academic_year=f"{ay}/Autumn", subject_query=query
                )
                records.extend(await asyncio.to_thread(parse_question_papers_html, html_autumn))
            except Exception as e_autumn:
                logger.warning("Autumn search failed: %r", e_autumn)

            try:
                html_spring = await client.fetch_question_papers(
                    academic_year=f"{ay}/Spring", subject_query=query
                )
                records.extend(await asyncio.to_thread(parse_question_papers_html, html_spring))
            except Exception as e_spring:
                logger.warning("Spring search failed: %r", e_spring)
            return records

        search_records = await with_pooled_session(
            user_id=user_id,
            roll_number=roll_number,
            encrypted_password=encrypted_password,
            work=_work,
        )

        return {"success": True, "records": search_records}

    except NitrisCircuitOpenError as e:
        return {"success": False, "error": str(e)}
    except LoginUnavailableError as e:
        # Portal down/misbehaving — NOT a credential problem (H1 fix).
        logger.warning("qp_search: NITRIS unavailable for user_id=%s: %r", user_id, e)
        return {"success": False, "error": f"NITRIS temporarily unavailable: {e}"}
    except (LoginError, CredentialsQuarantinedError) as e:
        await on_login_failure(user_id, str(e))
        return {"success": False, "error": f"Login failed: {e}"}
    except Exception as e:
        logger.error("qp_search NITRIS work failed: %r", e)
        return {"success": False, "error": str(e)}


# ── Helpers ─────────────────────────────────────────────────────────

async def _edit_callback_message(
    chat_id: Optional[int],
    message_id: Optional[int],
    text: str,
    reply_markup=None,
) -> None:
    """Edit a Telegram message if chat_id and message_id are provided."""
    if not _bot or not chat_id or not message_id:
        return
    try:
        await _bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=reply_markup,
        )
    except Exception as e:
        logger.warning("Failed to edit callback message: %r", e)


async def _update_sync_state(
    user_id: int, success: bool, error_msg: Optional[str] = None,
) -> None:
    """Update the SyncState tracker after a manual refresh."""
    try:
        async with get_db_session() as session:
            async with session.begin():
                stmt = select(SyncState).where(SyncState.user_id == user_id)
                res = await session.execute(stmt)
                state = res.scalar_one_or_none()

                if not state:
                    state = SyncState(user_id=user_id, failure_count=0)
                    session.add(state)

                state.last_sync = datetime.now(IST)
                if success:
                    state.last_success = datetime.now(IST)
                    state.last_error = None
                    state.failure_count = 0
                else:
                    state.last_error = (error_msg or "Unknown error")[:1000]
                    state.failure_count = (state.failure_count or 0) + 1
    except Exception as e:
        logger.error("Failed to update SyncState for user_id=%d: %r", user_id, e)


async def handle_timetable_sync(job: NitrisJob) -> dict:
    """Manual timetable sync from Home.aspx dashboard.

    Payload:
        - callback_chat_id: int (optional Telegram chat ID to edit/reply)
        - callback_message_id: int (optional Telegram message ID to edit)

    Returns:
        dict with success, error, entry_count, synced_at_ist.
    """
    from app.services.timetable_service import sync_user_timetable
    from app.bot.handlers.timetable import get_day_selector_keyboard, get_not_synced_keyboard
    from app.config import IST
    from datetime import datetime

    user_id = job.user_id
    callback_chat_id = job.payload.get("callback_chat_id")
    callback_message_id = job.payload.get("callback_message_id")

    result = await sync_user_timetable(
        user_id=user_id,
        callback_chat_id=callback_chat_id,
        callback_message_id=callback_message_id,
    )

    # Edit the loading message with result if callback provided
    if _bot and callback_chat_id and callback_message_id:
        try:
            if result.get("success"):
                entry_count = result.get("entry_count", 0)
                synced_at_ist = result.get("synced_at_ist", "")
                today_weekday = min(datetime.now(IST).weekday(), 5)
                await _bot.edit_message_text(
                    chat_id=callback_chat_id,
                    message_id=callback_message_id,
                    text=(
                        f"✅ <b>Timetable synced successfully!</b>\n\n"
                        f"📊 Saved <b>{entry_count}</b> class slots.\n"
                        f"🕒 <i>{synced_at_ist}</i>\n\n"
                        f"Choose an option below to view your schedule:"
                    ),
                    reply_markup=get_day_selector_keyboard(today_weekday),
                    parse_mode=ParseMode.HTML,
                )
            else:
                err = result.get("error", "Unknown error")
                await _bot.edit_message_text(
                    chat_id=callback_chat_id,
                    message_id=callback_message_id,
                    text=f"❌ <b>Timetable sync failed:</b>\n\n{esc(str(err))}",
                    reply_markup=get_not_synced_keyboard(),
                    parse_mode=ParseMode.HTML,
                )
        except Exception as e:
            logger.warning("timetable_sync: could not edit callback message: %r", e)

    return result


async def handle_qp_prewarm_subject(payload: dict, bot) -> dict:
    """Pre-warm BOTH exam types for one subject (admin-driven cache filling).

    Payload: {subject_code, academic_year, donor_user_id}
    - Ensures catalog rows exist (metadata fetch under the DONOR's pooled
      session, only when a row is missing)
    - For each exam type: claim → download (donor) → CHANNEL-only upload →
      mark available. Never touches any student's chat.
    - Honors prewarm_state.stop_event between every item.
    """
    from app.services.prewarm_state import prewarm_state

    subject_code = payload.get("subject_code") or ""
    ac_year = payload.get("academic_year") or ""
    donor_user_id = payload.get("donor_user_id")

    if prewarm_state.stopped:
        return {"success": False, "stopped": True}

    from app.bot import qpaper_registry
    svc = qpaper_registry.qpaper_service
    if svc is None:
        return {"success": False, "error": "QPaperService not initialized"}

    from app.db.database import get_db_session
    from app.services.examination_service import ExaminationService

    # ── Phase 1: ensure cache rows exist for both exam types ──────────
    row_ids: dict[str, Optional[int]] = {"mid_sem": None, "end_sem": None}
    async with get_db_session() as session:
        exam_service = ExaminationService(session)
        for et in ("mid_sem", "end_sem"):
            row = await exam_service.get_cached_paper(subject_code, ac_year, et)
            row_ids[et] = row.id if row else None

    need_meta = any(v is None for v in row_ids.values())
    if need_meta and not prewarm_state.stopped:
        try:
            records = await svc._fetch_metadata_via_pool(donor_user_id, ac_year, subject_code)
            async with get_db_session() as session:
                es = ExaminationService(session)
                persisted = await es.persist_subject_metadata(
                    parsed_records=records,
                    academic_year=ac_year,
                    subject_code=subject_code,
                )
                await session.commit()
                for r in persisted:
                    if getattr(r, "exam_type", None) in row_ids:
                        row_ids[r.exam_type] = r.id
        except Exception as e:
            logger.warning(
                "prewarm: metadata sync failed for %s %s: %r",
                subject_code, ac_year, e,
            )
            # Continue — whatever rows already exist still get pre-warmed.

    # ── Phase 2: acquire each exam type (stop-aware, concurrency-capped) ──
    results: list[str] = []
    for et in ("mid_sem", "end_sem"):
        if prewarm_state.stopped:
            break
        cache_id = row_ids.get(et)
        if cache_id is None:
            prewarm_state.counters["skipped"] += 1
            results.append(f"{subject_code}:{et}:no-row")
            continue

        async with prewarm_state.semaphore:
            if prewarm_state.stopped:
                prewarm_state.counters["skipped"] += 1
                continue
            res = await svc.prewarm_one(cache_id, donor_user_id)

        results.append(f"{subject_code}:{et}:{res}")
        if res == "available":
            prewarm_state.counters["available"] += 1
        elif res == "not_available":
            prewarm_state.counters["not_available"] += 1
        elif res in ("failed", "permanent-failed", "donor-creds", "channel-down"):
            prewarm_state.counters["failed"] += 1
        else:  # already / negative / busy / missing / no-target
            prewarm_state.counters["skipped"] += 1

    prewarm_state.record_subject_done()
    return {"success": True, "results": results}


async def handle_session_warm(payload: dict, bot) -> dict:
    """LAYER 1: silently warm a user's pooled portal session (login now so the
    next tap skips paced-login entirely). Fired from dashboard/inbox/timetable
    touchpoints; dedup + throttle live in session_warmer.request_session_warm.
    """
    user_id = payload.get("user_id")
    if not user_id:
        return {"success": False, "error": "missing user_id"}

    from app.services.session_warmer import warm_now
    ok = await warm_now(int(user_id))
    return {"success": bool(ok)}


