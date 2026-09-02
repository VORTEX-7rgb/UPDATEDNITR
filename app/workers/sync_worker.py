"""Periodic background sync worker and event notification dispatcher."""

import asyncio
import logging
import html
from datetime import datetime, timezone, timedelta
from typing import Optional, Any

def normalize_to_utc(dt: datetime) -> datetime:
    """Normalize any datetime (aware or naive IST) to UTC timezone-aware datetime."""
    if dt.tzinfo is None:
        from app.config import config
        dt = dt.replace(tzinfo=config.IST)
    return dt.astimezone(timezone.utc)

from app.utils import esc, safe_truncate


from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramForbiddenError, TelegramAPIError
from sqlalchemy import select, or_
from sqlalchemy.orm import selectinload

from app.db.database import get_db_session, is_db_connection_error
from app.db.models import User, SyncState, Event, EventType
from app.db.crypto import decrypt_password
from app.db.repositories.event_repository import EventRepository
from app.services.attendance_service import get_attendance_data
from app.services.snapshot_service import SnapshotService
from app.services.lock_service import user_lock

logger = logging.getLogger(__name__)

# NOTE: The legacy SYNC_SEMAPHORE_LIMIT / SYNC_INTERVAL_SECONDS /
# DISPATCH_INTERVAL_SECONDS constants were removed — they were superseded by
# the Phase-5 durable scheduler (MODULE_TTL_SECONDS + SCHEDULER_* in
# app/config.py) and nothing imported them.


async def wait_for_db_recovery(worker_name: str) -> None:
    """Blocks until the database connection is healthy, using exponential backoff."""
    backoff = 5
    attempt = 1
    has_failed = False
    
    while True:
        try:
            async with get_db_session() as session:
                # Issue a simple lightweight ping query
                await session.execute(select(1))
            if has_failed:
                logger.info("Database recovered. %s worker resumed.", worker_name)
            return
        except Exception as e:
            if is_db_connection_error(e):
                if not has_failed:
                    logger.error("Database connection lost. %s worker waiting for recovery...", worker_name)
                    has_failed = True
                
                logger.warning(
                    "Attempting reconnect for %s worker. Retry %d (waiting %ds)...",
                    worker_name, attempt, backoff
                )
                await asyncio.sleep(backoff)
                attempt += 1
                backoff = min(backoff * 2, 60)
            else:
                # If it's a non-connection error, propagate it immediately
                raise


async def prepare_inbox_sync(client, user_id):
    """Network phase for inbox sync. No open DB transaction, no DB writes.

    Phase 4 perf redesign:
      - Detail fetches are now PARALLEL with bounded concurrency (default 5).
        15 messages × 4s sequential = 60s → 15/5 × 4s = 12s. ~5x speedup.
      - Only the NEWEST INBOX_SYNC_DETAIL_LIMIT (default 15) missing messages
        are detail-fetched per sync; older ones persist header-only and
        lazy-fetch on first open (cache-first-forever contract).
      - SessionExpiredError PROPAGATES (caller must re-login, not swallow).
      - Other transient errors fall back to body=None but mark the row for
        re-fetch on next user open (handled in persist_inbox_sync).

    Returns (scraped_messages, detail_cache, existing_by_portal_id).
    """
    from app.nitris.parser import parse_messages_list_html, parse_message_detail_html
    from app.db.repositories.inbox_repository import InboxRepository
    from app.config import config
    from app.nitris.exceptions import SessionExpiredError

    # 1. Fetch + parse the raw messages list (network, no DB session held).
    #    Parsing runs in a worker thread — the AllMessages page is ~700KB+ of
    #    ASP.NET HTML and a full BS4 tree build would stall the event loop.
    list_html = await client.fetch_messages_list()
    scraped_messages = await asyncio.to_thread(parse_messages_list_html, list_html)
    if not scraped_messages:
        logger.info("No messages found on portal for user_id=%s", user_id)
        return [], {}, {}

    # NOTE: wait_for_db_recovery is deliberately NOT called here anymore.
    # Job handlers pre-wait BEFORE acquiring the pooled portal session
    # (lease hygiene) — waiting inside this callback used to pin an
    # authenticated NITRIS session for minutes during a DB outage.

    # 2. Short DB read of already-known portal messages. This connection is
    #    released before any slow network I/O below.
    portal_ids = [m["portal_message_id"] for m in scraped_messages]
    async with get_db_session() as session:
        inbox_repo = InboxRepository(session)
        existing = await inbox_repo.get_by_portal_message_ids(user_id, portal_ids)
    existing_by_id = {m.portal_message_id: m for m in existing}

    # 3. Pre-fetch detail pages for NEW non-historical messages IN PARALLEL.
    #    Only the NEWEST INBOX_SYNC_DETAIL_LIMIT missing messages are fetched
    #    during the sync; older ones are persisted header-only by
    #    persist_inbox_sync (body=None + stale body_fetched_at) and their
    #    bodies are lazily fetched on first open via the cache-first inbox
    #    path. Also bounded by INBOX_DETAIL_FETCH_CONCURRENCY (default 5) to
    #    avoid tripping the NITRIS circuit breaker with too many requests.
    new_messages_to_fetch = sorted(
        (
            m for m in scraped_messages
            if m["portal_message_id"] not in existing_by_id
            and not m["token"].startswith("postback:")
        ),
        key=lambda m: m["sent_on"],
        reverse=True,
    )[: config.INBOX_SYNC_DETAIL_LIMIT]

    if not new_messages_to_fetch:
        return scraped_messages, {}, existing_by_id

    detail_sem = asyncio.Semaphore(config.INBOX_DETAIL_FETCH_CONCURRENCY)
    fetch_errors: dict[int, str] = {}

    async def _fetch_one_detail(msg: dict) -> tuple[int, dict]:
        """Fetch a single message detail. Returns (portal_id, detail_dict).
        SessionExpiredError propagates so caller can re-login. Other errors
        return body=None so the message can still be persisted with header only."""
        async with detail_sem:
            try:
                detail_html = await client.fetch_message_detail(msg["token"])
                parsed = await asyncio.to_thread(parse_message_detail_html, detail_html)
                return (msg["portal_message_id"], parsed)
            except SessionExpiredError:
                # Propagate — caller must re-login, not silently swallow
                raise
            except Exception as e:
                logger.warning(
                    "Failed to fetch message detail for portal_id=%s (user_id=%s): %r",
                    msg["portal_message_id"], user_id, e,
                )
                fetch_errors[msg["portal_message_id"]] = str(e)
                return (msg["portal_message_id"], {"body": None, "attachment_url": None})

    # gather() with return_exceptions=False so SessionExpiredError propagates
    # immediately and aborts the whole sync (the caller will re-login).
    results = await asyncio.gather(*[_fetch_one_detail(m) for m in new_messages_to_fetch])
    detail_cache = dict(results)

    prepare_inbox_sync.last_fetch_errors = fetch_errors  # type: ignore[attr-defined]

    return scraped_messages, detail_cache, existing_by_id


async def persist_inbox_sync(user_id, scraped_messages, detail_cache, existing_by_id, baseline: bool = False):
    """DB write phase for inbox sync. One short transaction, no network I/O.

    When ``baseline=True`` (first sync right after registration), new messages
    are inserted SILENTLY — no NEW_MESSAGE_RECEIVED events — so a user's
    historical inbox backlog doesn't flood them with "new message" alerts.

    IMPLICIT BASELINE: even with baseline=False, event creation is suppressed
    whenever the user's inbox is still EMPTY (first-ever population). This
    makes "the first population of an inbox never notifies" a structural
    invariant instead of a caller convention — a racing scheduler tick or
    refresh job can no longer burst the backlog while onboarding is in flight.
    """
    if not scraped_messages:
        return

    from app.db.repositories.inbox_repository import InboxRepository

    await wait_for_db_recovery(f"Sync-Inbox-Persist-{user_id}")

    async with get_db_session() as session:
        async with session.begin():
            # Serialize concurrent per-user inbox persistence at the DB level.
            # pg_advisory_xact_lock blocks until any competing transaction for
            # this user commits, then holds until OUR transaction commits - it is
            # auto-released on commit/rollback, so no lock can leak on exceptions.
            from sqlalchemy import text
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
                {"key": f"inbox:user:{user_id}"},
            )

            inbox_repo = InboxRepository(session)
            event_repo = EventRepository(session)

            # Re-read current DB state INSIDE the lock. The passed-in
            # existing_by_id was computed during prepare_inbox_sync (before this
            # lock); a concurrent sync may have committed the same messages since
            # then, so re-derive the authoritative map here to avoid a spurious
            # unique-constraint violation.
            portal_ids = [m["portal_message_id"] for m in scraped_messages]
            current = await inbox_repo.get_by_portal_message_ids(user_id, portal_ids)
            existing_by_id = {m.portal_message_id: m for m in current}

            # IMPLICIT BASELINE (spam-race fix, incident 2026-08-25 VM logs
            # 16:51 UTC): the scheduler's next_sync_at spread is uniform over
            # the whole TTL window, so a brand-new user's inbox schedule can
            # come due SECONDS after registration — before the silent
            # onboarding prefetch commits. That racing sync ran with
            # baseline=False, populated the still-empty inbox, and burst a
            # NEW_MESSAGE_RECEIVED notification per backlog message.
            #
            # Invariant: if this user has NO inbox rows yet, ANY sync that
            # populates them now is delivering the student's historical
            # backlog — regardless of which caller lost the race (scheduler,
            # onboarding retry, user-tapped refresh). First population is
            # therefore ALWAYS silent; only genuinely-new arrivals on top of
            # an existing inbox may notify.
            effective_baseline = baseline
            if not effective_baseline:
                effective_baseline = not await inbox_repo.has_any_messages(user_id)
                if effective_baseline:
                    logger.info(
                        "Implicit baseline for user_id=%d — first-ever inbox "
                        "population arrived via a non-baseline sync path; "
                        "suppressing new-message events.",
                        user_id,
                    )

            for msg in scraped_messages:
                normalized_scraped_sent_on = normalize_to_utc(msg["sent_on"])
                existing = existing_by_id.get(msg["portal_message_id"])

                if existing is None:
                    if msg["token"].startswith("postback:"):
                        new_msg = await inbox_repo.create_message(
                            user_id=user_id,
                            portal_message_id=msg["portal_message_id"],
                            token=msg["token"],
                            sender=msg["sender"],
                            subject=msg["subject"],
                            sent_on=normalized_scraped_sent_on,
                            body=None,
                            attachment_url=None,
                        )
                        logger.info(
                            "Sync registered historical message ID %s for user %s (header only)",
                            new_msg.portal_message_id, user_id,
                        )
                    else:
                        detail_data = detail_cache.get(
                            msg["portal_message_id"], {"body": None, "attachment_url": None}
                        )
                        new_msg = await inbox_repo.create_message(
                            user_id=user_id,
                            portal_message_id=msg["portal_message_id"],
                            token=msg["token"],
                            sender=msg["sender"],
                            subject=msg["subject"],
                            sent_on=normalized_scraped_sent_on,
                            body=detail_data.get("body"),
                            attachment_url=detail_data.get("attachment_url"),
                        )
                        if detail_data.get("body") is not None:
                            new_msg.body_fetched_at = datetime.now(timezone.utc)
                        else:
                            # Phase 4: detail fetch failed for this message.
                            # Set body_fetched_at to a STALE timestamp so the
                            # lazy-fetch TTL in render_single_message triggers
                            # a re-fetch on next user open. Without this, the
                            # message permanently shows "(No content)" until
                            # the next full sync (which may be hours away).
                            from app.config import config
                            from datetime import timedelta
                            stale_ts = datetime.now(timezone.utc) - timedelta(
                                seconds=config.INBOX_BODY_TTL_SECONDS * 2
                            )
                            new_msg.body_fetched_at = stale_ts
                            logger.info(
                                "Marked portal_id=%s for lazy body fetch on next open "
                                "(no body fetched during sync — capped or fetch failed)",
                                msg["portal_message_id"],
                            )

                        if not effective_baseline:
                            if not await event_repo.has_message_event(
                                user_id, EventType.NEW_MESSAGE_RECEIVED, new_msg.id
                            ):
                                await event_repo.create_event(
                                    user_id=user_id,
                                    event_type=EventType.NEW_MESSAGE_RECEIVED,
                                    payload_json={
                                        "message_id": new_msg.id,
                                        "sender": new_msg.sender,
                                        "subject": new_msg.subject,
                                        "body_snippet": (new_msg.body[:150] + "..." if new_msg.body else ""),
                                        "has_attachment": bool(new_msg.attachment_url),
                                    },
                                )
                        logger.info(
                            "Sync inserted message ID %s for user %s (baseline=%s)",
                            new_msg.portal_message_id, user_id, effective_baseline,
                        )
                else:
                    if existing.token != msg["token"]:
                        logger.info(
                            "Message token shifted from %s to %s for user %s. Updating token dynamically.",
                            existing.token, msg["token"], user_id,
                        )
                        existing.token = msg["token"]

                    existing_sent_on_utc = normalize_to_utc(existing.sent_on)
                    if existing.subject != msg["subject"]:
                        logger.info(
                            "Sync detected edited notice. Invalidating cache for token: %s",
                            msg["token"],
                        )
                        existing.subject = msg["subject"]
                        existing.sent_on = normalized_scraped_sent_on
                        existing.body = None
                        existing.body_fetched_at = None
                        existing.attachment_url = None
                        existing.attachment_cache_id = None
                        existing.is_read = False
                        if not await event_repo.has_message_event(
                            user_id, EventType.MESSAGE_UPDATED, existing.id
                         ):
                            await event_repo.create_event(
                                user_id=user_id,
                                event_type=EventType.MESSAGE_UPDATED,
                                payload_json={
                                    "message_id": existing.id,
                                    "sender": existing.sender,
                                    "subject": existing.subject,
                                },
                            )
                    elif existing_sent_on_utc != normalized_scraped_sent_on:
                        # Silent timestamp migration: update existing.sent_on to corrected UTC
                        # without wiping body cache, resetting read status, or spamming events.
                        existing.sent_on = normalized_scraped_sent_on


_event_dispatcher = None


async def init_event_dispatcher(bot: Bot) -> None:
    """Initialize the EventDispatcherService singleton.
    Call from main.py AFTER bot is created, BEFORE polling starts.
    Also starts the stale-claim reaper background task."""
    global _event_dispatcher
    from app.db.database import async_session_factory
    from app.services.event_dispatcher_service import EventDispatcherService
    _event_dispatcher = EventDispatcherService(
        bot=bot,
        session_factory=async_session_factory,
    )
    _event_dispatcher.start_reaper()
    logger.info("EventDispatcherService initialized (worker_id=%s)", _event_dispatcher.worker_id)


async def shutdown_event_dispatcher() -> None:
    """Graceful shutdown — call from main.py finally block."""
    global _event_dispatcher
    if _event_dispatcher is not None:
        await _event_dispatcher.stop()


def format_notification_message(event_type: str | EventType, payload: dict) -> str:
    """Backward-compatible helper delegating to event_dispatcher_service._format_notification."""
    from app.services.event_dispatcher_service import _format_notification
    text_content, _ = _format_notification(event_type, payload)
    return text_content


async def run_dispatch_worker(bot: Bot) -> None:
    """REWRITTEN — delegates to EventDispatcherService.

    The old implementation had the duplicate-notification bug:
      1. Fetch 50 unsent events
      2. Send each via bot.send_message()
      3. After ALL 50 sent, ONE bulk mark_sent
    Crash between 2 and 3 → all 50 stay sent=False → re-sent on restart →
    DUPLICATE NOTIFICATIONS.

    The new implementation (EventDispatcherService) uses:
      - Atomic claim via UPDATE...WHERE id IN (SELECT...) RETURNING
      - Per-event immediate mark_sent (crash window ~10ms, not ~30s)
      - Stale-claim reaper for crashed dispatcher recovery
      - FloodWait retry with retry_after sleep
      - Retry exhaustion → permanent_failure (terminal state)
      - Orphaned events (user deleted) → permanent_failure (not silent drop)
    """
    global _event_dispatcher
    while _event_dispatcher is None:
        await asyncio.sleep(0.5)
    await _event_dispatcher.run_forever()
