"""Question-paper service — global multi-tenant cache with DB-safe locking,
bounded NITRIS acquisition, bounded Telegram delivery, FloodWait retry,
crash-safe recovery, and explicit state machine.

Design:
  * Cache is global (one row per (subject_code, academic_year, exam_type)).
    All students share the same telegram_file_id. Zero NITRIS traffic on hit.
  * State machine on question_paper_caches.status:
        paper_available | paper_not_available | fetch_in_progress |
        retryable_failure | permanent_failure
  * Concurrent same-paper requests collapse into ONE acquisition job via
    atomic UPDATE...WHERE status IN ('retryable_failure', 'fetch_in_progress'+stale).
    Losers poll DB every 2s until terminal state or 60s timeout.
  * Concurrent different-paper requests are bounded by asyncio.Semaphore(8)
    for NITRIS acquisition and asyncio.Semaphore(25) for Telegram delivery
    (under Telegram's ~30/sec rate limit).
  * Slow operations (NITRIS download, Telegram upload, sleeps) NEVER hold an
    open DB session. All DB work is short transactions around atomic updates.
  * Crash recovery: stale locks (acquired_at < NOW() - 5min) are reclaimable
    by the next request. A periodic reaper converts very stale locks to
    retryable_failure.
  * Telegram failures NEVER corrupt cache — a failed send_document to the user
    leaves the cache row untouched. Only failed NITRIS acquisition or failed
    upload-to-storage-channel updates the cache row to retryable_failure.
  * ZIP and PDF both supported (signature sniffed from response bytes).
  * CREDENTIAL POLICY (H2): cold acquisitions use ONLY the requesting
    student's own NITRIS credentials. There is no cross-account fallback pool.

NEVER call this service from inside an open DB session — it manages its own
short-lived sessions internally.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Callable, Awaitable, Tuple

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from aiogram import Bot
from aiogram.types import BufferedInputFile
from aiogram.exceptions import TelegramRetryAfter, TelegramForbiddenError, TelegramAPIError

from app.config import config
from app.db.models import QuestionPaperCache, QPStatus, User
from app.nitris.client import NitrisClient
from app.nitris.exceptions import (
    AttendanceWorkflowError, SessionExpiredError, LoginError, InvalidContextError,
    PaperNotAvailableError, CredentialsQuarantinedError,
)
from app.utils import esc

logger = logging.getLogger(__name__)

# Tunables — env-driven via app.config (single source of truth), re-exported
# as module-level names so tests and call sites stay stable.
MAX_CONCURRENT_ACQUISITIONS = config.QP_MAX_CONCURRENT_ACQUISITIONS   # caps NITRIS load + memory
MAX_CONCURRENT_DELIVERIES = config.QP_MAX_CONCURRENT_DELIVERIES       # under Telegram's ~30/sec bot rate limit
ACQUIRE_STALE_SECONDS = config.QP_ACQUIRE_STALE_SECONDS               # 5 min — stale locks become reclaimable
ACQUIRE_PERMANENT_AFTER = config.QP_PERMANENT_AFTER                   # retryable_failure → permanent_failure after N attempts
WAIT_POLL_INTERVAL_SEC = config.QP_WAIT_POLL_INTERVAL_SECONDS
WAIT_TIMEOUT_SEC = config.QP_WAIT_TIMEOUT_SECONDS
FLOODWAIT_MAX_RETRIES = config.QP_FLOODWAIT_MAX_RETRIES
DELIVERY_MAX_RETRIES = config.QP_DELIVERY_MAX_RETRIES
DELIVERY_RETRY_BASE_DELAY = config.QP_DELIVERY_RETRY_BASE_DELAY


@dataclass
class QPResult:
    """Result of a deliver() call. Carries terminal state for the bot handler."""
    delivered: bool = False             # file was successfully sent to user
    not_available: bool = False         # NITRIS confirmed no paper exists
    in_progress: bool = False           # another worker is acquiring, user should retry shortly
    error: Optional[str] = None         # technical error message (only when system actually failed)
    permanent: bool = False             # permanent_failure — needs human attention
    file_kind: Optional[str] = None     # 'pdf' or 'zip' when delivered


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _is_zip(bytes_: bytes) -> bool:
    return bytes_[:4] == b"PK\x03\x04"


def _is_pdf(bytes_: bytes) -> bool:
    return bytes_[:4] == b"%PDF"


def _sniff_kind(b: bytes) -> str:
    if _is_zip(b):
        return "zip"
    if _is_pdf(b):
        return "pdf"
    # Default to pdf — NITRIS sometimes returns wrong Content-Type for PDFs
    return "pdf"


def _is_permanent_error(exc: Exception) -> bool:
    """Heuristic: only permanent failures are 'NITRIS confirmed paper doesn't exist'
    or 5xx-class portal errors that don't resolve with retries."""
    if isinstance(exc, InvalidContextError):
        # NITRIS 503 on the actual download postback = permanent for this paper
        # (the postback target itself is invalid/revoked — retrying won't help)
        return True
    msg = str(exc).lower()
    if "no paper" in msg or "paper not available" in msg or "not found on portal" in msg:
        return True
    return False


class QPaperService:
    """Global QP cache orchestrator. One instance per bot process (singleton)."""

    def __init__(
        self,
        bot: Bot,
        session_factory: async_sessionmaker[AsyncSession],
        creds_provider: Callable[[], Awaitable[Tuple[str, str]]],
        storage_chat_id: Optional[int] = None,
    ):
        """
        Args:
            bot: aiogram Bot instance (used for send_document + storage uploads).
            session_factory: async_sessionmaker bound to async engine.
            creds_provider: async callable returning (roll_number, plaintext_password)
                for a NITRIS account that can fetch papers. Used ONLY for acquisition
                on cache miss — never for delivery.
            storage_chat_id: Telegram chat ID for the private QP storage channel.
                Defaults to config.QP_STORAGE_CHAT_ID.
        """
        self.bot = bot
        self.session_factory = session_factory
        self.creds_provider = creds_provider
        self.storage_chat_id = storage_chat_id if storage_chat_id is not None else config.QP_STORAGE_CHAT_ID
        if not self.storage_chat_id:
            logger.info(
                "QP_STORAGE_CHAT_ID is not set in .env — paper acquisitions will use "
                "direct user chat fallback for storage and caching."
            )
        # Bounded queues — single per-process
        self._acquire_sem = asyncio.Semaphore(MAX_CONCURRENT_ACQUISITIONS)
        self._deliver_sem = asyncio.Semaphore(MAX_CONCURRENT_DELIVERIES)
        # Background reaper task handle (started lazily)
        self._reaper_task: Optional[asyncio.Task] = None

    # ── Public entry point ──────────────────────────────────────────

    async def deliver(self, cache_id: int, telegram_id: int, requester_user_id: Optional[int] = None,
                      nav_markup: Optional[Any] = None) -> QPResult:
        """Deliver a paper to a Telegram user. Called by the bot QP handler.

        Args:
            cache_id: question_paper_caches row id.
            telegram_id: chat to deliver to.
            requester_user_id: internal user id of the requesting student.
                REQUIRED for cold acquisitions: THEIR OWN NITRIS credentials
                are used exclusively (own-account attribution, no cross-tenant
                logins — H2 fix). Cache-hit deliveries never touch credentials.

        Flow:
          1. Read cache row (short DB session).
          2. Dispatch on status:
              - paper_available → cached delivery (bounded by delivery semaphore)
              - paper_not_available → PERMANENT negative: instant no-paper
                answer, zero NITRIS calls, never re-checked (professors do not
                retroactively upload papers). Manual recovery via /admin_reset_qp.
              - permanent_failure → error result
              - fetch_in_progress → wait + deliver
              - retryable_failure → try to acquire (claim)
          3. Acquisition (if claimed): NITRIS download → Telegram upload →
             atomic state update → wake any waiters via DB poll.
        """
        # Short DB read — never held during slow ops.
        snapshot = await self._read_cache(cache_id)
        if snapshot is None:
            return QPResult(error="Question paper record not found.")
        status, file_id, file_kind, sub_code, ac_year, ex_type, postback, err_msg, _not_avail_until = snapshot

        if status == QPStatus.PAPER_AVAILABLE.value and file_id:
            return await self._deliver_cached(
                cache_id, file_id, file_kind, telegram_id, sub_code, ac_year, ex_type, postback,
                requester_user_id=requester_user_id,
                nav_markup=nav_markup,
            )
        if status == QPStatus.PAPER_NOT_AVAILABLE.value:
            # PERMANENT negative cache — no TTL expiry, no re-check traffic.
            return QPResult(not_available=True)
        if status == QPStatus.PERMANENT_FAILURE.value:
            return QPResult(permanent=True, error=err_msg or "permanent_failure")
        # fetch_in_progress OR retryable_failure → try to claim
        return await self._claim_or_wait_and_deliver(
            cache_id, telegram_id, sub_code, ac_year, ex_type, postback,
            requester_user_id=requester_user_id,
            nav_markup=nav_markup,
        )

    # ── Cache read (short transaction) ──────────────────────────────

    async def _read_cache(self, cache_id: int) -> Optional[Tuple]:
        async with self.session_factory() as session:
            rec = await session.get(QuestionPaperCache, cache_id)
            if rec is None:
                return None
            return (
                rec.status, rec.telegram_file_id, rec.file_kind,
                rec.subject_code, rec.academic_year, rec.exam_type,
                rec.portal_postback_target, rec.error_message,
                getattr(rec, "not_available_until", None),
            )

    # ── Cached delivery — bounded, FloodWait-aware ───────────────────

    async def _deliver_cached(
        self, cache_id: int, file_id: str, file_kind: Optional[str],
        telegram_id: int, sub_code: str, ac_year: str, ex_type: str,
        postback: Optional[str] = None,
        requester_user_id: Optional[int] = None,
        nav_markup: Optional[Any] = None,
    ) -> QPResult:
        """Forward cached telegram_file_id to user. Bounded by delivery semaphore.
        If file_id is invalid (e.g. from an old bot token), auto-recovers by re-acquiring.
        nav_markup attaches Back-to-Papers/Dashboard buttons to the document."""
        async with self._deliver_sem:
            caption = _make_caption(sub_code, ac_year, ex_type)
            for attempt in range(DELIVERY_MAX_RETRIES):
                try:
                    await self.bot.send_document(
                        chat_id=telegram_id,
                        document=file_id,
                        caption=caption,
                        parse_mode="HTML",
                        reply_markup=nav_markup,
                    )
                    return QPResult(delivered=True, file_kind=file_kind)
                except TelegramRetryAfter as e:
                    if attempt + 1 >= DELIVERY_MAX_RETRIES:
                        return QPResult(error=f"Telegram rate-limited (retry_after={e.retry_after}s). Try again in a moment.")
                    logger.warning(
                        "FloodWait on QP delivery to %d (cache_id=%d): %ds — retrying",
                        telegram_id, cache_id, e.retry_after,
                    )
                    await asyncio.sleep(e.retry_after + 0.5)
                    continue
                except TelegramForbiddenError:
                    return QPResult(error="Bot blocked by user — cannot deliver.")
                except TelegramAPIError as e:
                    msg = str(e).lower()
                    if "wrong file identifier" in msg or "file_id" in msg or "wrong persistent file_id" in msg:
                        logger.warning(
                            "Invalid telegram_file_id for cache_id=%d: %r — auto-recovering via fresh acquisition",
                            cache_id, e,
                        )
                        async with self.session_factory() as session:
                            async with session.begin():
                                await session.execute(text("""
                                    UPDATE question_paper_caches
                                    SET status = :retryable,
                                        telegram_file_id = NULL,
                                        attempt_count = 0,
                                        error_message = :err,
                                        updated_at = NOW()
                                    WHERE id = :id
                                """), {
                                    "retryable": QPStatus.RETRYABLE_FAILURE.value,
                                    "id": cache_id,
                                    "err": f"Stale file_id: {e}",
                                    "updated_at": _now_utc(),
                                })
                        if postback is None:
                            snap = await self._read_cache(cache_id)
                            postback = snap[6] if snap else ""
                        return await self._claim_or_wait_and_deliver(
                            cache_id, telegram_id, sub_code, ac_year, ex_type, postback,
                            requester_user_id=requester_user_id,
                            nav_markup=nav_markup,
                        )
                    if "chat not found" in msg or "deactivated" in msg:
                        return QPResult(error="User account unavailable.")
                    if attempt + 1 >= DELIVERY_MAX_RETRIES:
                        return QPResult(error=f"Telegram error: {e}")
                    await asyncio.sleep(DELIVERY_RETRY_BASE_DELAY * (attempt + 1))
                    continue
            return QPResult(error="Delivery exhausted retries.")

    # ── Claim-or-wait — atomic DB CAS ────────────────────────────────

    async def _claim_or_wait_and_deliver(
        self, cache_id: int, telegram_id: int,
        sub_code: str, ac_year: str, ex_type: str, postback: str,
        requester_user_id: Optional[int] = None,
        nav_markup: Optional[Any] = None,
    ) -> QPResult:
        """Attempt to atomically claim the row for acquisition. If someone else
        has it, wait + deliver when their acquisition completes."""
        job_id = uuid.uuid4().hex
        # Atomic CAS: claim if retryable_failure OR fetch_in_progress that's stale
        claimed = await self._claim_for_acquisition(cache_id, job_id)
        if claimed:
            return await self._acquire_and_deliver(
                cache_id, telegram_id, job_id, sub_code, ac_year, ex_type, postback,
                requester_user_id=requester_user_id,
                nav_markup=nav_markup,
            )
        # Lost the race — wait for the other worker
        return await self._wait_and_deliver(
            cache_id, telegram_id, requester_user_id=requester_user_id,
            nav_markup=nav_markup,
        )

    async def _claim_for_acquisition(self, cache_id: int, job_id: str) -> bool:
        """Atomic compare-and-swap: claim the row for acquisition.

        Returns True if this caller won the claim, False otherwise.
        Pattern: UPDATE ... WHERE status='retryable_failure' OR
                                  (status='fetch_in_progress' AND acquired_at < stale threshold)
                 RETURNING id
        If 0 rows returned, someone else has a fresh claim.
        """
        async with self.session_factory() as session:
            async with session.begin():
                stmt = text("""
                    UPDATE question_paper_caches
                    SET status = :in_progress,
                        acquired_by = :job_id,
                        acquired_at = NOW(),
                        lease_expires_at = NOW() + make_interval(secs => :stale_secs),
                        last_attempt_at = NOW(),
                        attempt_count = attempt_count + 1,
                        error_message = NULL,
                        updated_at = NOW()
                    WHERE id = :cache_id
                      AND (
                        status = :retryable
                        OR (status = :in_progress
                            AND (acquired_at IS NULL
                                 OR acquired_at < NOW() - make_interval(secs => :stale_secs)))
                      )
                    RETURNING id, subject_code, academic_year, exam_type, portal_postback_target
                """)
                result = await session.execute(stmt, {
                    "in_progress": QPStatus.FETCH_IN_PROGRESS.value,
                    "retryable": QPStatus.RETRYABLE_FAILURE.value,
                    "job_id": job_id,
                    "cache_id": cache_id,
                    "stale_secs": ACQUIRE_STALE_SECONDS,
                })
                row = result.first()
                if row is None:
                    return False
                logger.info(
                    "Claimed cache_id=%d for acquisition (job=%s, attempt will be #%d)",
                    cache_id, job_id[:8], 0,  # attempt_count already incremented
                )
                return True

    async def _acquire_and_deliver(
        self, cache_id: int, telegram_id: int, job_id: str,
        sub_code: str, ac_year: str, ex_type: str, postback: str,
        requester_user_id: Optional[int] = None,
        nav_markup: Optional[Any] = None,
    ) -> QPResult:
        """Slow path — NITRIS download + Telegram upload. NO DB session held."""
        async with self._acquire_sem:
            try:
                # 1. Download from NITRIS (slow — no DB session open).
                #    requester_user_id passed positionally so test doubles
                #    accepting *args remain compatible.
                file_bytes, kind = await self._nitris_download(
                    sub_code, ac_year, ex_type, postback, requester_user_id,
                )
                # 2. Upload to storage channel OR (fallback) to the user's chat.
                #    When falling back to user's chat, the upload ALSO serves as
                #    the first delivery — no separate send_document needed.
                file_id, uploaded_to_user = await self._telegram_upload(
                    file_bytes, kind, sub_code, ac_year, ex_type,
                    fallback_chat_id=telegram_id,
                    nav_markup=nav_markup,
                )
                # 3. Atomic state update → paper_available
                await self._mark_available(cache_id, file_id, kind, len(file_bytes))
                logger.info(
                    "Acquired QP cache_id=%d (%s %s %s) — file_id=%s size=%d kind=%s uploaded_to_user=%s",
                    cache_id, sub_code, ac_year, ex_type, file_id[:24], len(file_bytes), kind, uploaded_to_user,
                )
                # 4. If the upload already went to the user's chat (fallback mode),
                #    skip the separate delivery — the file is already in their chat.
                if uploaded_to_user:
                    return QPResult(delivered=True, file_kind=kind)
                # 5. Otherwise (uploaded to storage channel), forward the cached
                #    file_id to the user.
                return await self._deliver_cached(
                    cache_id, file_id, kind, telegram_id, sub_code, ac_year, ex_type,
                    nav_markup=nav_markup,
                )
            except PaperNotAvailableError as exc:
                # NITRIS confirms no paper is uploaded -> PERMANENT negative.
                # Professors do not retroactively upload papers, so this row is
                # never re-checked. Manual recovery: /admin_reset_qp.
                await self._mark_not_available(cache_id, exc)
                logger.info(
                    "QP paper not available cache_id=%d job=%s — cached permanently",
                    cache_id, job_id[:8],
                )
                return QPResult(not_available=True)
            except CredentialsQuarantinedError as exc:
                # H2: the REQUESTER has no usable credentials — this must NOT
                # mark the SHARED cache row failed (other students may still
                # acquire with their own accounts). Leave the row untouched.
                logger.info(
                    "QP acquisition blocked: requester_user_id=%s missing/quarantined",
                    requester_user_id,
                )
                return QPResult(
                    error="Update your NITRIS credentials first — use /forgot, then try again.",
                )
            except Exception as exc:
                # Classify and update cache atomically. NEVER corrupt: the row's
                # status transitions to retryable_failure or permanent_failure —
                # the cache row itself remains valid and queryable.
                permanent = _is_permanent_error(exc)
                # Check attempt_count to escalate to permanent after threshold
                attempts = await self._get_attempt_count(cache_id)
                if attempts >= ACQUIRE_PERMANENT_AFTER:
                    permanent = True
                new_status = (
                    QPStatus.PERMANENT_FAILURE.value if permanent
                    else QPStatus.RETRYABLE_FAILURE.value
                )
                await self._mark_failure(cache_id, new_status, exc)
                logger.error(
                    "QP acquisition failed cache_id=%d job=%s: %r (status=%s attempts=%d)",
                    cache_id, job_id[:8], exc, new_status, attempts,
                )
                if permanent:
                    return QPResult(permanent=True, error=str(exc))
                return QPResult(error=f"Acquisition failed (will retry): {exc}")

    async def _wait_and_deliver(
        self, cache_id: int, telegram_id: int,
        requester_user_id: Optional[int] = None,
        nav_markup: Optional[Any] = None,
    ) -> QPResult:
        """Poll cache status until terminal or timeout. Used when another worker
        has the row in fetch_in_progress state."""
        start = time.monotonic()
        last_status = None
        while time.monotonic() - start < WAIT_TIMEOUT_SEC:
            await asyncio.sleep(WAIT_POLL_INTERVAL_SEC)
            snap = await self._read_cache(cache_id)
            if snap is None:
                return QPResult(error="Record disappeared during wait.")
            status, file_id, file_kind, sub_code, ac_year, ex_type, _, err_msg, not_avail_until = snap
            if status != last_status:
                logger.info(
                    "Wait poll cache_id=%d status=%s elapsed=%.1fs",
                    cache_id, status, time.monotonic() - start,
                )
                last_status = status
            if status == QPStatus.PAPER_AVAILABLE.value and file_id:
                return await self._deliver_cached(
                    cache_id, file_id, file_kind, telegram_id,
                    sub_code, ac_year, ex_type,
                    requester_user_id=requester_user_id,
                    nav_markup=nav_markup,
                )
            if status == QPStatus.PAPER_NOT_AVAILABLE.value:
                return QPResult(not_available=True)
            if status == QPStatus.PERMANENT_FAILURE.value:
                return QPResult(permanent=True, error=err_msg)
            if status == QPStatus.RETRYABLE_FAILURE.value:
                # The acquiring worker failed — try to re-claim
                return await self._claim_or_wait_and_deliver(
                    cache_id, telegram_id, sub_code, ac_year, ex_type, snap[6],
                    requester_user_id=requester_user_id,
                    nav_markup=nav_markup,
                )
            # fetch_in_progress — keep polling
        return QPResult(
            in_progress=True,
            error="Acquisition is taking longer than expected — please tap the button again in a moment.",
        )

    # ── NITRIS + Telegram slow operations (no DB session) ───────────

    async def _nitris_download(
        self, sub_code: str, ac_year: str, ex_type: str, postback_target: str,
        requester_user_id: Optional[int] = None,
    ) -> Tuple[bytes, str]:
        """Login to NITRIS as the REQUESTER ONLY, submit the postback target,
        fetch raw paper bytes. Returns (bytes, kind) where kind is 'pdf' or 'zip'.

        CREDENTIAL POLICY (H2 fix — own credentials exclusively): cold
        acquisitions run under the requesting student's OWN account, so
        downloads are attributed to them in the portal and a failed login
        quarantines the right person. There is NO cross-account fallback:
        logging into another student's account to serve someone else's
        request was a consent/attribution hazard and could quarantine
        innocent users.

        Raises:
            CredentialsQuarantinedError: requester missing or quarantined —
                callers must surface "/forgot" guidance and MUST NOT mark
                the shared cache row failed (other users may still acquire).
            LoginError: portal rejected the requester's credentials.
            LoginUnavailableError: portal down/misbehaving (never a
                credential problem — propagates without quarantine).
        """
        if requester_user_id is None:
            raise RuntimeError(
                "QP acquisition requires requester_user_id — no anonymous "
                "cross-account downloads."
            )

        own = await self._load_own_credentials(requester_user_id)
        if own is None:
            raise CredentialsQuarantinedError(
                f"Requester user_id={requester_user_id} is missing or has "
                "invalid credentials — use /forgot to update them."
            )

        from app.nitris.session_pool import with_pooled_session

        roll, user_id, encrypted_password = own

        async def _work(client: NitrisClient, password: str):
            file_bytes = await client.download_question_paper_bytes(
                academic_year=ac_year,
                subject_query=sub_code,
                event_target=postback_target,
            )
            return file_bytes, _sniff_kind(file_bytes)

        # PERF P1: pooled authenticated session — warm acquisitions skip login.
        try:
            return await with_pooled_session(
                user_id=user_id,
                roll_number=roll,
                encrypted_password=encrypted_password,
                work=_work,
            )
        except LoginError as e:
            # Confirmed rejection of the REQUESTER'S OWN credentials —
            # quarantine the right person via the standard gate.
            logger.warning("QP download login failed for user_id=%d: %r", user_id, e)
            from app.nitris.auth_gate import on_login_failure
            await on_login_failure(user_id, str(e))
            raise

    async def _load_own_credentials(self, user_id: int) -> Optional[Tuple[str, int, str]]:
        """Short-session read of a user's OWN credentials.

        Returns (roll_number, user_id, encrypted_password) or None if the user
        is missing or quarantined (H2: there is no pool fallback anymore —
        None means acquisition cannot proceed for this requester). Never
        raises — a lookup failure is treated like missing credentials.
        """
        try:
            from sqlalchemy import select as sa_select
            async with self.session_factory() as session:
                row = (
                    await session.execute(
                        sa_select(
                            User.roll_number, User.id, User.encrypted_password,
                        ).where(
                            User.id == user_id,
                            User.credentials_valid == True,  # noqa: E712
                        )
                    )
                ).first()
            if row is None:
                return None
            return (row[0], row[1], row[2])
        except Exception as e:
            logger.warning(
                "Could not load own credentials for user_id=%s — using pool fallback: %r",
                user_id, e,
            )
            return None

    async def _telegram_upload(
        self, file_bytes: bytes, kind: str,
        sub_code: str, ac_year: str, ex_type: str,
        fallback_chat_id: Optional[int] = None,
        nav_markup: Optional[Any] = None,
    ) -> Tuple[str, bool]:
        """Upload the paper ONCE to the bot's private QP storage channel.

        If QP_STORAGE_CHAT_ID is 0/unset or if the channel upload fails (e.g.
        chat not found / bot not admin), gracefully falls back to uploading
        directly to fallback_chat_id (the requesting user's chat).
        Telegram file_ids are reusable across all chats, so the cached file_id
        is forwardable to any other student instantly.
        """
        ext = "pdf" if kind == "pdf" else "zip"
        safe_year = ac_year.replace("/", "_").replace("\\", "_")
        filename = f"{sub_code}_{safe_year}_{ex_type}.{ext}"

        # 1. Try storage channel first if configured
        if self.storage_chat_id:
            try:
                document = BufferedInputFile(file_bytes, filename=filename)
                msg = await self.bot.send_document(
                    chat_id=self.storage_chat_id,
                    document=document,
                    caption=f"📚 {sub_code} | {ac_year} | {ex_type}",
                )
                if msg.document and msg.document.file_id:
                    return msg.document.file_id, False
            except Exception as e:
                logger.warning(
                    "Upload to QP_STORAGE_CHAT_ID (%s) failed (%r) — falling back to direct user chat",
                    self.storage_chat_id, e,
                )

        # 2. Fallback to direct user chat
        if not fallback_chat_id:
            raise RuntimeError(
                "Cannot upload paper — neither valid QP_STORAGE_CHAT_ID nor fallback user chat provided."
            )

        logger.info(
            "Uploading paper %s (%s %s) directly to user chat %d (file_id will be cached for all students)",
            sub_code, ac_year, ex_type, fallback_chat_id,
        )
        document = BufferedInputFile(file_bytes, filename=filename)
        msg = await self.bot.send_document(
            chat_id=fallback_chat_id,
            document=document,
            caption=_make_caption(sub_code, ac_year, ex_type),
            parse_mode="HTML",
            reply_markup=nav_markup,
        )
        if not msg.document or not msg.document.file_id:
            raise RuntimeError("Telegram upload returned no document file_id.")
        return msg.document.file_id, True

    # ── Atomic state transitions ─────────────────────────────────────

    async def _mark_available(
        self, cache_id: int, file_id: str, kind: str, size: int,
    ) -> None:
        async with self.session_factory() as session:
            async with session.begin():
                await session.execute(text("""
                    UPDATE question_paper_caches
                    SET status = :status,
                        telegram_file_id = :file_id,
                        file_kind = :kind,
                        file_size_bytes = :size,
                        acquired_by = NULL,
                        acquired_at = NULL,
                        error_message = NULL,
                        updated_at = NOW()
                    WHERE id = :cache_id AND status = :in_progress
                """), {
                    "status": QPStatus.PAPER_AVAILABLE.value,
                    "file_id": file_id,
                    "kind": kind,
                    "size": size,
                    "cache_id": cache_id,
                    "in_progress": QPStatus.FETCH_IN_PROGRESS.value,
                })

    async def _mark_failure(
        self, cache_id: int, new_status: str, exc: Exception,
    ) -> None:
        async with self.session_factory() as session:
            async with session.begin():
                await session.execute(text("""
                    UPDATE question_paper_caches
                    SET status = :status,
                        acquired_by = NULL,
                        acquired_at = NULL,
                        error_message = :err,
                        updated_at = NOW()
                    WHERE id = :cache_id AND status = :in_progress
                """), {
                    "status": new_status,
                    "err": str(exc)[:1000],
                    "cache_id": cache_id,
                    "in_progress": QPStatus.FETCH_IN_PROGRESS.value,
                })

    async def _mark_not_available(self, cache_id: int, exc: Exception) -> None:
        """Mark a paper as permanently not available.

        ``not_available_until`` is set to NULL on purpose: NULL means the
        negative is permanent (no TTL expiry re-check). This also heals any
        legacy row that still carried an old TTL stamp.
        """
        async with self.session_factory() as session:
            async with session.begin():
                await session.execute(text("""
                    UPDATE question_paper_caches
                    SET status = :status,
                        not_available_until = NULL,
                        acquired_by = NULL,
                        acquired_at = NULL,
                        error_message = :err,
                        updated_at = NOW()
                    WHERE id = :cache_id AND status = :in_progress
                """), {
                    "status": QPStatus.PAPER_NOT_AVAILABLE.value,
                    "err": str(exc)[:1000],
                    "cache_id": cache_id,
                    "in_progress": QPStatus.FETCH_IN_PROGRESS.value,
                })

    async def _get_attempt_count(self, cache_id: int) -> int:
        async with self.session_factory() as session:
            row = (await session.execute(
                text("SELECT attempt_count FROM question_paper_caches WHERE id = :id"),
                {"id": cache_id},
            )).first()
            return int(row[0]) if row else 0

    # ── Stale-lock reaper (background safety net) ────────────────────

    async def _reap_stale_locks(self) -> int:
        """Convert locks held > ACQUIRE_STALE_SECONDS*2 to retryable_failure.
        Returns number of rows reclaimed."""
        async with self.session_factory() as session:
            async with session.begin():
                result = await session.execute(text("""
                    UPDATE question_paper_caches
                    SET status = :retryable,
                        acquired_by = NULL,
                        error_message = COALESCE(error_message, '') || ' [stale-lock-reaped]',
                        updated_at = NOW()
                    WHERE status = :in_progress
                      AND acquired_at IS NOT NULL
                      AND acquired_at < NOW() - make_interval(secs => :stale_secs * 2)
                """), {
                    "retryable": QPStatus.RETRYABLE_FAILURE.value,
                    "in_progress": QPStatus.FETCH_IN_PROGRESS.value,
                    "stale_secs": ACQUIRE_STALE_SECONDS,
                })
                return result.rowcount or 0

    async def _reaper_loop(self) -> None:
        """Background task — runs every 60s, reclaims crashed acquisitions."""
        while True:
            try:
                reclaimed = await self._reap_stale_locks()
                if reclaimed:
                    logger.info("Stale-lock reaper reclaimed %d QP acquisition(s)", reclaimed)
            except Exception as e:
                logger.error("Stale-lock reaper failed: %r", e)
            await asyncio.sleep(60.0)

    def start_reaper(self) -> None:
        """Idempotent — starts the background reaper if not already running."""
        if self._reaper_task is None or self._reaper_task.done():
            self._reaper_task = asyncio.create_task(self._reaper_loop())

    async def stop_reaper(self) -> None:
        if self._reaper_task and not self._reaper_task.done():
            self._reaper_task.cancel()
            try:
                await self._reaper_task
            except asyncio.CancelledError:
                pass


# ── Helpers ────────────────────────────────────────────────────────


def _make_caption(sub_code: str, ac_year: str, ex_type: str) -> str:
    return (
        f"📚 <b>NITRIS Question Paper</b>\n\n"
        f"📖 Subject: <b>{esc(sub_code)}</b>\n"
        f"📅 Session: <b>{esc(ac_year)}</b>\n"
        f"📝 Exam: <b>{ex_type.upper().replace('_', ' ')}</b>\n"
    )
