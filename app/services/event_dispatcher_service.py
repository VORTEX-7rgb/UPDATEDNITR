"""Event dispatcher service — atomic claim + per-event mark-sent + retry policy.

PERMANENT FIX for the duplicate-notification bug. The old `run_dispatch_worker`
in sync_worker.py:
  1. Fetched 50 unsent events (single SELECT)
  2. Sent each via bot.send_message()
  3. After ALL 50 sent, did ONE bulk `UPDATE events SET sent=True WHERE id IN (...)`

If the bot crashed between step 2 and step 3, all those events stayed `sent=False`
in DB → re-sent on restart → DUPLICATE NOTIFICATIONS. Crash window: ~30s.

This service implements the correct pattern:
  Phase 1 (atomic claim):
    UPDATE events SET claimed_at=NOW(), claimed_by=:worker, attempt_count=attempt_count+1
    WHERE id IN (SELECT id FROM events
                 WHERE sent=False AND permanent_failure=False
                   AND (claimed_at IS NULL OR claimed_at < NOW()-INTERVAL '5 min')
                 ORDER BY id LIMIT :batch)
    RETURNING *
  Phase 2 (per-event send + immediate mark):
    For each claimed event:
      - Send to Telegram (FloodWait-aware)
      - On success: IMMEDIATELY UPDATE event SET sent=True, sent_at=NOW(), claimed_at=NULL
      - On user-blocked: UPDATE event SET sent=True, sent_at=NOW(),
                         permanent_failure=True, last_error='user blocked bot',
                         claimed_at=NULL
      - On retryable error: UPDATE event SET claimed_at=NULL, last_error=...,
                            (next cycle re-claims and retries)
      - If attempt_count >= MAX_DISPATCH_ATTEMPTS:
            UPDATE event SET permanent_failure=True, claimed_at=NULL, last_error=...

Crash window per event: ~10ms (between send_message returning and the mark_sent
UPDATE). 1 duplicate max per crashed event, vs 50 duplicates in the old code.

Stale-claim reaper (background task, runs every 60s):
  UPDATE events SET claimed_at=NULL, claimed_by=NULL
  WHERE sent=False AND claimed_at < NOW()-INTERVAL '10 min'

This recovers events claimed by a crashed dispatcher.

NEVER holds a DB session open during bot.send_message() — all DB work is in
short transactions around atomic updates. Same architectural pattern as
QPaperService.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional, Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramRetryAfter, TelegramForbiddenError, TelegramAPIError
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram import types

from app.db.models import Event, EventType
from app.utils import esc, safe_truncate

logger = logging.getLogger(__name__)

# ── Tunables ────────────────────────────────────────────────────────────────
DISPATCH_BATCH_SIZE = 25                # events claimed per cycle
CLAIM_STALE_SECONDS = 300               # 5 min — stale claims reclaimable
MAX_DISPATCH_ATTEMPTS = 5               # → permanent_failure after N retries
DISPATCH_INTERVAL_SECONDS = 60          # main loop sleep
REAPER_INTERVAL_SECONDS = 60            # stale-claim reaper sleep
PER_EVENT_SEND_TIMEOUT = 30             # bot.send_message timeout (seconds)
FLOODWAIT_MAX_RETRIES = 3               # per-event FloodWait retries


# ── Atomic claim — multi-process safe ──────────────────────────────────────

async def claim_events(
    session_factory: async_sessionmaker[AsyncSession],
    worker_id: str,
    batch_size: int = DISPATCH_BATCH_SIZE,
) -> list[dict]:
    """Atomically claim N unsent events for this worker.

    Uses UPDATE...WHERE id IN (SELECT...) RETURNING — atomic across processes.
    Only events that are:
      - sent=False
      - permanent_failure=False
      - claimed_at IS NULL OR claimed_at < NOW()-INTERVAL '5 min' (stale claim)
    are eligible.

    Returns list of dicts: {id, user_id, event_type, payload_json, attempt_count}.
    Empty list if no events to claim.
    """
    async with session_factory() as session:
        async with session.begin():
            stmt = text("""
                UPDATE events
                SET claimed_at = NOW(),
                    claimed_by = :worker_id,
                    attempt_count = attempt_count + 1
                WHERE id IN (
                    SELECT id FROM events
                    WHERE sent = FALSE
                      AND permanent_failure = FALSE
                      AND (claimed_at IS NULL
                           OR claimed_at < NOW() - make_interval(secs => :stale_secs))
                    ORDER BY id ASC
                    LIMIT :batch
                    FOR UPDATE SKIP LOCKED
                )
                RETURNING id, user_id, event_type, payload_json, attempt_count
            """)
            result = await session.execute(stmt, {
                "worker_id": worker_id,
                "stale_secs": CLAIM_STALE_SECONDS,
                "batch": batch_size,
            })
            rows = result.fetchall()
            return [
                {
                    "id": r[0],
                    "user_id": r[1],
                    "event_type": r[2],
                    "payload_json": r[3],
                    "attempt_count": r[4],
                }
                for r in rows
            ]


# ── Per-event state transitions (atomic, immediate) ────────────────────────

async def mark_event_sent(
    session_factory: async_sessionmaker[AsyncSession], event_id: int
) -> None:
    """Atomically mark a single event as sent + clear claim. Short transaction."""
    async with session_factory() as session:
        async with session.begin():
            await session.execute(text("""
                UPDATE events
                SET sent = TRUE,
                    sent_at = NOW(),
                    claimed_at = NULL,
                    claimed_by = NULL,
                    last_error = NULL
                WHERE id = :id
            """), {"id": event_id})


async def mark_event_permanent_failure(
    session_factory: async_sessionmaker[AsyncSession], event_id: int, error: str,
) -> None:
    """Mark an event as permanently failed — terminal state, no more retries.
    Also sets sent=True so it's excluded from future claim queries."""
    async with session_factory() as session:
        async with session.begin():
            await session.execute(text("""
                UPDATE events
                SET sent = TRUE,
                    sent_at = NOW(),
                    permanent_failure = TRUE,
                    claimed_at = NULL,
                    claimed_by = NULL,
                    last_error = :err
                WHERE id = :id
            """), {"id": event_id, "err": str(error)[:1000]})


async def release_event_claim(
    session_factory: async_sessionmaker[AsyncSession], event_id: int, error: Optional[str] = None,
) -> None:
    """Release the claim on an event so it can be re-claimed next cycle.
    Used after a retryable failure (e.g. FloodWait exhausted, network error)."""
    async with session_factory() as session:
        async with session.begin():
            await session.execute(text("""
                UPDATE events
                SET claimed_at = NULL,
                    claimed_by = NULL,
                    last_error = :err
                WHERE id = :id
            """), {"id": event_id, "err": str(error)[:1000] if error else None})


# ── Stale-claim reaper ──────────────────────────────────────────────────────

async def reap_stale_claims(
    session_factory: async_sessionmaker[AsyncSession]
) -> int:
    """Reclaim events claimed > CLAIM_STALE_SECONDS*2 ago by a crashed worker.
    Returns number of events reclaimed."""
    async with session_factory() as session:
        async with session.begin():
            result = await session.execute(text("""
                UPDATE events
                SET claimed_at = NULL,
                    claimed_by = NULL,
                    last_error = COALESCE(last_error, '') || ' [stale-claim-reaped]'
                WHERE sent = FALSE
                  AND permanent_failure = FALSE
                  AND claimed_at IS NOT NULL
                  AND claimed_at < NOW() - make_interval(secs => :stale_secs * 2)
            """), {"stale_secs": CLAIM_STALE_SECONDS})
            return result.rowcount or 0


# ── Bot lookup helper ──────────────────────────────────────────────────────

async def get_telegram_id_for_user(
    session_factory: async_sessionmaker[AsyncSession], user_id: int
) -> Optional[int]:
    """Look up the telegram_id for a user_id. Short DB session."""
    async with session_factory() as session:
        result = await session.execute(text(
            "SELECT telegram_id FROM users WHERE id = :id"
        ), {"id": user_id})
        row = result.first()
        return int(row[0]) if row else None


# ── Main dispatcher ────────────────────────────────────────────────────────

class EventDispatcherService:
    """Singleton event dispatcher. Same architectural pattern as QPaperService:
    atomic DB-CAS claim + per-event state transition + stale-claim reaper.

    Crash safety:
      - Per-event mark_sent happens IMMEDIATELY after Telegram send succeeds
        (crash window ~10ms, not ~30s)
      - Stale-claim reaper reclaims events claimed >10min ago
      - Atomic claim prevents duplicate sends across processes (future-proof)
    """

    def __init__(
        self,
        bot: Bot,
        session_factory: async_sessionmaker[AsyncSession],
        worker_id: Optional[str] = None,
    ):
        self.bot = bot
        self.session_factory = session_factory
        self.worker_id = worker_id or f"dispatcher-{uuid.uuid4().hex[:8]}"
        self._reaper_task: Optional[asyncio.Task] = None
        self._stop = False

    def start_reaper(self) -> None:
        """Idempotent — starts the background stale-claim reaper."""
        if self._reaper_task is None or self._reaper_task.done():
            self._reaper_task = asyncio.create_task(self._reaper_loop())

    async def stop_reaper(self) -> None:
        self._stop = True
        if self._reaper_task and not self._reaper_task.done():
            self._reaper_task.cancel()
            try:
                await self._reaper_task
            except asyncio.CancelledError:
                pass

    async def _reaper_loop(self) -> None:
        """Background task — reclaims crashed dispatcher's event claims."""
        while not self._stop:
            try:
                reclaimed = await reap_stale_claims(self.session_factory)
                if reclaimed:
                    logger.info("Stale-claim reaper reclaimed %d event(s)", reclaimed)
            except Exception as e:
                logger.error("Stale-claim reaper failed: %r", e)
            await asyncio.sleep(REAPER_INTERVAL_SECONDS)

    async def run_forever(self) -> None:
        """Main dispatcher loop. Call via asyncio.create_task from main.py."""
        logger.info("EventDispatcherService started (worker_id=%s)", self.worker_id)
        self.start_reaper()
        while not self._stop:
            try:
                await self._dispatch_once()
            except Exception as e:
                logger.error("Dispatcher cycle failed: %r", e)
            await asyncio.sleep(DISPATCH_INTERVAL_SECONDS)

    async def stop(self) -> None:
        """Graceful shutdown."""
        self._stop = True
        await self.stop_reaper()

    async def _dispatch_once(self) -> int:
        """Run one dispatch cycle. Returns number of events successfully sent."""
        # Phase 1: atomic claim
        claimed = await claim_events(self.session_factory, self.worker_id)
        if not claimed:
            return 0

        logger.info("Dispatcher claimed %d event(s)", len(claimed))

        sent_count = 0
        for ev in claimed:
            try:
                # Look up telegram_id (short DB session, not held during send)
                telegram_id = await get_telegram_id_for_user(
                    self.session_factory, ev["user_id"]
                )
                if telegram_id is None:
                    # User was deleted (orphaned event) — mark permanent
                    await mark_event_permanent_failure(
                        self.session_factory, ev["id"],
                        error=f"orphaned event — user_id={ev['user_id']} not found",
                    )
                    logger.warning(
                        "Event %d marked permanent_failure (orphaned user_id=%d)",
                        ev["id"], ev["user_id"],
                    )
                    continue

                # Format + send
                msg_text, reply_markup = _format_notification(
                    ev["event_type"], ev["payload_json"]
                )

                # Send to Telegram with FloodWait retry
                success, error = await self._send_with_retry(
                    telegram_id, msg_text, reply_markup
                )

                if success:
                    # IMMEDIATELY mark this single event as sent (short transaction).
                    # Crash window between send_message returning and this UPDATE
                    # is ~10ms — orders of magnitude smaller than the old bulk
                    # update's ~30s window.
                    await mark_event_sent(self.session_factory, ev["id"])
                    sent_count += 1
                    logger.info(
                        "Dispatched event %d to telegram_id=%d (attempt %d)",
                        ev["id"], telegram_id, ev["attempt_count"],
                    )
                elif error == "USER_BLOCKED":
                    # User blocked the bot — terminal state, no retry
                    await mark_event_permanent_failure(
                        self.session_factory, ev["id"],
                        error="user blocked the bot",
                    )
                    logger.warning(
                        "Event %d marked permanent_failure (user %d blocked bot)",
                        ev["id"], telegram_id,
                    )
                elif error == "USER_DEACTIVATED":
                    await mark_event_permanent_failure(
                        self.session_factory, ev["id"],
                        error="user account deactivated",
                    )
                else:
                    # Retryable failure (network, FloodWait exhausted, etc.)
                    if ev["attempt_count"] >= MAX_DISPATCH_ATTEMPTS:
                        await mark_event_permanent_failure(
                            self.session_factory, ev["id"],
                            error=f"exhausted {MAX_DISPATCH_ATTEMPTS} attempts: {error}",
                        )
                        logger.warning(
                            "Event %d marked permanent_failure (exhausted retries)",
                            ev["id"],
                        )
                    else:
                        # Release claim — will be re-claimed next cycle
                        await release_event_claim(
                            self.session_factory, ev["id"],
                            error=f"retryable: {error}",
                        )
                        logger.info(
                            "Event %d released (attempt %d, error: %s)",
                            ev["id"], ev["attempt_count"], error[:100] if error else "",
                        )
            except Exception as e:
                logger.error(
                    "Unexpected error dispatching event %d: %r", ev["id"], e
                )
                # Release the claim so it can be retried — don't leave it stuck
                try:
                    await release_event_claim(
                        self.session_factory, ev["id"], error=str(e),
                    )
                except Exception:
                    pass

        return sent_count

    async def _send_with_retry(
        self, telegram_id: int, text: str, reply_markup=None,
    ) -> tuple[bool, Optional[str]]:
        """Send a Telegram message with FloodWait retry. Returns (success, error_or_status)."""
        for attempt in range(FLOODWAIT_MAX_RETRIES):
            try:
                await self.bot.send_message(
                    chat_id=telegram_id,
                    text=text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=reply_markup,
                )
                return True, None
            except TelegramRetryAfter as e:
                if attempt + 1 >= FLOODWAIT_MAX_RETRIES:
                    return False, f"floodwait_exhausted (retry_after={e.retry_after}s)"
                logger.warning(
                    "FloodWait sending to %d: %ds — retrying",
                    telegram_id, e.retry_after,
                )
                await asyncio.sleep(e.retry_after + 0.5)
                continue
            except TelegramForbiddenError:
                return False, "USER_BLOCKED"
            except TelegramAPIError as e:
                msg = str(e).lower()
                if "chat not found" in msg or "deactivated" in msg:
                    return False, "USER_DEACTIVATED"
                if attempt + 1 >= FLOODWAIT_MAX_RETRIES:
                    return False, f"telegram_api_error: {e}"
                # Brief backoff for transient errors
                await asyncio.sleep(1.0 * (attempt + 1))
                continue
        return False, "send_exhausted_retries"


# ── Message formatting ─────────────────────────────────────────────────────

def _format_notification(event_type: str | EventType, payload: dict) -> tuple[str, Any]:
    """Build the Telegram message + optional inline keyboard.
    Returns (text, reply_markup_or_None)."""
    sub_name = esc(payload.get("subject_name") or payload.get("subject_code", "Unknown"))
    sub_code = esc(payload.get("subject_code", ""))

    ev_type_val = event_type.value if isinstance(event_type, EventType) else str(event_type)

    if ev_type_val == EventType.NEW_SUBJECT_ADDED.value:
        text = (
            f"📚 <b>New Subject Registered</b>\n\n"
            f"🎓 Course: <b>{sub_name}</b> ({sub_code})\n"
            f"👨‍🏫 Faculty: <b>{esc(payload.get('faculty', 'N/A'))}</b>\n"
            f"📊 Initial Stats: TC: {esc(payload.get('tc', '0'))} | UA: {esc(payload.get('ua', '0'))} | OA: 0\n"
        )
        return text, None

    if ev_type_val == EventType.ATTENDANCE_UPDATED.value:
        text = f"📊 <b>Attendance Update Detected</b>\n\n🔸 Subject: <b>{sub_name}</b> ({sub_code})\n📈 Class Stats changed:\n"
        changes = payload.get("changes", {})
        for field, delta in changes.items():
            name = field.upper()
            text += f"  • {name}: <b>{esc(delta.get('old'))} ➡️ {esc(delta.get('new'))}</b>\n"
        return text, None

    if ev_type_val == EventType.NEW_ABSENCE_DETECTED.value:
        text = (
            f"🚨 <b>New Absence Logged!</b>\n\n"
            f"🔸 Subject: <b>{sub_name}</b> ({sub_code})\n"
            f"⚠️ You were marked <b>ABSENT</b>!\n"
            f"📉 Unauthorized Absences: <b>{esc(payload.get('old_ua', '0'))} ➡️ {esc(payload.get('new_ua', '0'))}</b>\n"
            f"📊 Current Stats: TC: {esc(payload.get('total_classes', '0'))} | UA: {esc(payload.get('new_ua', '0'))}\n\n"
            f"<i>Keep an eye on your attendance to avoid debarment!</i>"
        )
        return text, None

    if ev_type_val == EventType.NEW_MESSAGE_RECEIVED.value:
        attach_str = "📎 Attachment included" if payload.get("has_attachment") else "No attachments"
        body_snippet = safe_truncate(esc(payload.get('body_snippet')), 150)
        text = (
            f"📩 <b>New Message Received!</b>\n\n"
            f"👤 From: <b>{esc(payload.get('sender'))}</b>\n"
            f"📌 Subject: <b>{esc(payload.get('subject'))}</b>\n\n"
            f"<i>\"{body_snippet}\"</i>\n\n"
            f"💡 {attach_str}\n"
            f"👉 Use /latest or open your Inbox to read the full notice!"
        )
        # Add "Read Full Notice" inline button
        builder = InlineKeyboardBuilder()
        msg_id = payload.get("message_id")
        if msg_id:
            builder.row(types.InlineKeyboardButton(
                text="📖 Read Full Notice", callback_data=f"msg_{msg_id}",
            ))
        return text, builder.as_markup() if msg_id else None

    if ev_type_val == EventType.MESSAGE_UPDATED.value:
        text = (
            f"🔄 <b>Notice Updated!</b>\n\n"
            f"👤 From: <b>{esc(payload.get('sender'))}</b>\n"
            f"📌 Subject: <b>{esc(payload.get('subject'))}</b>\n\n"
            f"⚠️ The university has updated this notice. Open your Inbox to read the revised version."
        )
        builder = InlineKeyboardBuilder()
        msg_id = payload.get("message_id")
        if msg_id:
            builder.row(types.InlineKeyboardButton(
                text="📖 Read Full Notice", callback_data=f"msg_{msg_id}",
            ))
        return text, builder.as_markup() if msg_id else None

    return f"ℹ️ <b>System Alert</b>\n\nSubject <b>{sub_name}</b> ({sub_code}) changed: {esc(payload)}", None
