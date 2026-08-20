"""Global multi-tenant attachment caching and delivery engine for NITRIS notices.

ARCHITECTURE
============
Mirrors QPaperService (`qpaper_service.py`) exactly:
  1. Global Content Store (`attachment_caches` table):
     - Keyed on `attachment_path` (the normalized URL path; query tokens stripped).
     - Single row shared across all students.
     - Telegram `file_id` cached here once, reused for all students with ZERO NITRIS traffic.

  2. Atomic Compare-And-Swap (CAS) state machine:
     - AVAILABLE | NOT_AVAILABLE | FETCH_IN_PROGRESS | RETRYABLE_FAILURE | PERMANENT_FAILURE
     - UPDATE attachment_caches SET status='fetch_in_progress', acquired_by=...
       WHERE id=... AND (status='retryable_failure' OR (status='fetch_in_progress' AND stale))
     - Exactly ONE worker acquires from NITRIS; all other concurrent requests poll DB.

  3. Strict Lease Boundaries:
     - Password decryption JIT inside `nitris_gateway.acquire()`.
     - `NitrisClient` closed inside `finally:` block of `acquire()`.
     - ZERO DB transactions open during NITRIS download or Telegram upload.

  4. Storage Channel & Fallback:
     - Uploads to `ATTACHMENT_STORAGE_CHAT_ID`.
     - If storage channel is unconfigured or fails, uploads directly to the user and caches file_id.

  5. Stale-Lock Reaper:
     - Periodic task (every 60s) resets stuck `fetch_in_progress` rows to `retryable_failure`.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Callable, Any, Awaitable

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import BufferedInputFile
from sqlalchemy import select, update, func, text as sql_text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import config
from app.db.models import AttachmentCache, AttachmentStatus
from app.utils import normalize_attachment_path, attachment_basename

logger = logging.getLogger(__name__)


@dataclass
class AttachmentResult:
    """Result of an attachment delivery attempt."""
    delivered: bool = False
    in_progress: bool = False
    not_available: bool = False
    permanent: bool = False
    error: Optional[str] = None
    file_kind: Optional[str] = None
    cache_id: Optional[int] = None


class AttachmentService:
    """Global multi-tenant caching and delivery service for NITRIS notice attachments."""

    def __init__(
        self,
        bot: Bot,
        session_factory: async_sessionmaker[AsyncSession],
        storage_chat_id: int = 0,
        stale_seconds: int = 300,
        permanent_after: int = 5,
        max_concurrent_acquisitions: int = 8,
        max_concurrent_deliveries: int = 25,
        wait_poll_interval: float = 2.0,
        wait_timeout: float = 60.0,
        floodwait_max_retries: int = 3,
        delivery_max_retries: int = 3,
        delivery_retry_base_delay: float = 1.0,
    ) -> None:
        self.bot = bot
        self.session_factory = session_factory
        self.storage_chat_id = storage_chat_id or config.ATTACHMENT_STORAGE_CHAT_ID
        self.stale_seconds = stale_seconds
        self.permanent_after = permanent_after
        self.max_concurrent_acquisitions = max_concurrent_acquisitions
        self.max_concurrent_deliveries = max_concurrent_deliveries
        self.wait_poll_interval = wait_poll_interval
        self.wait_timeout = wait_timeout
        self.floodwait_max_retries = floodwait_max_retries
        self.delivery_max_retries = delivery_max_retries
        self.delivery_retry_base_delay = delivery_retry_base_delay

        self._acquire_sem: Optional[asyncio.Semaphore] = None
        self._deliver_sem: Optional[asyncio.Semaphore] = None
        self._reaper_task: Optional[asyncio.Task] = None
        self._closing = False

    def _get_acquire_sem(self) -> asyncio.Semaphore:
        if self._acquire_sem is None:
            self._acquire_sem = asyncio.Semaphore(self.max_concurrent_acquisitions)
        return self._acquire_sem

    def _get_deliver_sem(self) -> asyncio.Semaphore:
        if self._deliver_sem is None:
            self._deliver_sem = asyncio.Semaphore(self.max_concurrent_deliveries)
        return self._deliver_sem

    def start_reaper(self) -> None:
        """Start the background stale-lock reaper."""
        if self._reaper_task is None or self._reaper_task.done():
            self._closing = False
            self._reaper_task = asyncio.create_task(
                self._reap_stale_locks_loop(),
                name="attachment-cache-stale-lock-reaper",
            )
            logger.info("AttachmentService: started stale-lock reaper loop")

    async def stop_reaper(self) -> None:
        """Cleanly cancel the background reaper."""
        self._closing = True
        if self._reaper_task and not self._reaper_task.done():
            self._reaper_task.cancel()
            try:
                await self._reaper_task
            except asyncio.CancelledError:
                pass
            logger.info("AttachmentService: stopped stale-lock reaper loop")

    async def deliver(
        self,
        attachment_url: str,
        telegram_id: int,
        source_user_id: int,
        source_roll_number: str,
        encrypted_password: str,
        subject: str = "",
    ) -> AttachmentResult:
        """Deliver an attachment to a student using global cache and atomic acquisition.

        Flow:
          1. Normalize URL to canonical attachment_path.
          2. Find or create the AttachmentCache row.
          3. If AVAILABLE and has telegram_file_id → forward cached file (0 NITRIS traffic).
          4. If NOT_AVAILABLE → return not_available.
          5. If PERMANENT_FAILURE → return permanent error.
          6. Else → Try to atomically claim acquisition lease.
             - If won CAS: Acquire via NITRIS inside gateway, upload, cache, deliver.
             - If lost CAS: Wait/poll until acquiring worker finishes, then deliver.
        """
        canonical_path = normalize_attachment_path(attachment_url)
        if not canonical_path:
            return AttachmentResult(permanent=True, error="Invalid attachment URL")

        cache_id = await self._find_or_create_cache_row(canonical_path)
        status, file_id, file_kind = await self._read_cache(cache_id)

        if status == AttachmentStatus.AVAILABLE.value and file_id:
            return await self._deliver_cached(
                cache_id=cache_id,
                telegram_id=telegram_id,
                telegram_file_id=file_id,
                canonical_path=canonical_path,
                subject=subject,
                file_kind=file_kind,
                recover=lambda: self._claim_or_wait_and_deliver(
                    cache_id=cache_id,
                    attachment_url=attachment_url,
                    canonical_path=canonical_path,
                    telegram_id=telegram_id,
                    source_user_id=source_user_id,
                    source_roll_number=source_roll_number,
                    encrypted_password=encrypted_password,
                    subject=subject,
                ),
            )

        if status == AttachmentStatus.NOT_AVAILABLE.value:
            return AttachmentResult(not_available=True, cache_id=cache_id)

        if status == AttachmentStatus.PERMANENT_FAILURE.value:
            return AttachmentResult(
                permanent=True,
                error="Attachment is permanently unavailable on portal",
                cache_id=cache_id,
            )

        return await self._claim_or_wait_and_deliver(
            cache_id=cache_id,
            attachment_url=attachment_url,
            canonical_path=canonical_path,
            telegram_id=telegram_id,
            source_user_id=source_user_id,
            source_roll_number=source_roll_number,
            encrypted_password=encrypted_password,
            subject=subject,
        )

    async def _find_or_create_cache_row(self, canonical_path: str) -> int:
        """Find existing cache ID or insert a new one atomically (race-safe).

        Concurrent cold-start requests for the same attachment path collapse onto
        ONE row via ``INSERT ... ON CONFLICT (attachment_path) DO NOTHING
        RETURNING id``. This avoids the IntegrityError a naive read-then-insert
        would raise on the unique index ``idx_attachment_caches_path`` when many
        students tap the same notice attachment simultaneously.
        """
        async with self.session_factory() as session:
            stmt = select(AttachmentCache.id).where(
                AttachmentCache.attachment_path == canonical_path
            )
            row = (await session.execute(stmt)).scalar_one_or_none()
            if row:
                return row

        # Fast path missed — atomically insert, tolerating a concurrent insert.
        async with self.session_factory() as session:
            async with session.begin():
                insert_stmt = sql_text("""
                    INSERT INTO attachment_caches (attachment_path, status, attempt_count)
                    VALUES (:path, :status, 0)
                    ON CONFLICT (attachment_path) DO NOTHING
                    RETURNING id
                """)
                row = (await session.execute(
                    insert_stmt,
                    {
                        "path": canonical_path,
                        "status": AttachmentStatus.RETRYABLE_FAILURE.value,
                    },
                )).scalar_one_or_none()
                if row is not None:
                    return row

        # Lost the insert race — another worker already created the row. Read it.
        async with self.session_factory() as session:
            stmt = select(AttachmentCache.id).where(
                AttachmentCache.attachment_path == canonical_path
            )
            row = (await session.execute(stmt)).scalar_one_or_none()
            if row:
                return row
            raise RuntimeError(
                f"Failed to find or create attachment cache row for {canonical_path!r}"
            )

    async def _read_cache(self, cache_id: int) -> tuple[str, Optional[str], Optional[str]]:
        """Read (status, telegram_file_id, file_kind) for a cache_id."""
        async with self.session_factory() as session:
            stmt = select(
                AttachmentCache.status,
                AttachmentCache.telegram_file_id,
                AttachmentCache.file_kind,
            ).where(AttachmentCache.id == cache_id)
            row = (await session.execute(stmt)).first()
            if not row:
                return (AttachmentStatus.RETRYABLE_FAILURE.value, None, None)
            return (row[0], row[1], row[2])

    async def _claim_or_wait_and_deliver(
        self,
        cache_id: int,
        attachment_url: str,
        canonical_path: str,
        telegram_id: int,
        source_user_id: int,
        source_roll_number: str,
        encrypted_password: str,
        subject: str,
    ) -> AttachmentResult:
        """Attempt to win CAS lease. If won, acquire from NITRIS. If lost, wait for winner."""
        job_id = f"job-{uuid.uuid4().hex[:8]}"
        claimed = await self._claim_for_acquisition(cache_id, job_id)

        if claimed:
            logger.info("AttachmentService: won CAS claim on cache_id=%d job_id=%s", cache_id, job_id)
            return await self._acquire_and_deliver(
                cache_id=cache_id,
                attachment_url=attachment_url,
                canonical_path=canonical_path,
                telegram_id=telegram_id,
                source_user_id=source_user_id,
                source_roll_number=source_roll_number,
                encrypted_password=encrypted_password,
                subject=subject,
                job_id=job_id,
            )
        else:
            logger.info("AttachmentService: lost CAS claim on cache_id=%d — entering waiter loop", cache_id)
            return await self._wait_and_deliver(cache_id, telegram_id, canonical_path, subject)

    async def _claim_for_acquisition(self, cache_id: int, job_id: str) -> bool:
        """Atomic Compare-And-Swap claim."""
        async with self.session_factory() as session:
            async with session.begin():
                stmt = sql_text("""
                    UPDATE attachment_caches
                    SET status = 'fetch_in_progress',
                        acquired_by = :job_id,
                        acquired_at = NOW(),
                        lease_expires_at = NOW() + make_interval(secs => :stale_secs),
                        last_attempt_at = NOW(),
                        attempt_count = attempt_count + 1,
                        error_message = NULL,
                        updated_at = NOW()
                    WHERE id = :cache_id
                      AND (
                        status = 'retryable_failure'
                        OR (status = 'fetch_in_progress'
                            AND (acquired_at IS NULL
                                 OR acquired_at < NOW() - make_interval(secs => :stale_secs)))
                      )
                    RETURNING id
                """)
                res = await session.execute(
                    stmt, {"cache_id": cache_id, "job_id": job_id, "stale_secs": self.stale_seconds}
                )
                return res.scalar_one_or_none() is not None

    async def _acquire_and_deliver(
        self,
        cache_id: int,
        attachment_url: str,
        canonical_path: str,
        telegram_id: int,
        source_user_id: int,
        source_roll_number: str,
        encrypted_password: str,
        subject: str,
        job_id: str,
    ) -> AttachmentResult:
        """Download from NITRIS inside gateway, upload to Telegram, update cache, and deliver."""
        async with self._get_acquire_sem():
            try:
                file_bytes, filename, kind = await self._nitris_download(
                    attachment_url=attachment_url,
                    canonical_path=canonical_path,
                    source_user_id=source_user_id,
                    source_roll_number=source_roll_number,
                    encrypted_password=encrypted_password,
                )
            except Exception as e:
                logger.error("AttachmentService: download failed for cache_id=%d: %r", cache_id, e)
                await self._mark_failure(cache_id, str(e))
                return AttachmentResult(permanent=False, error=str(e), cache_id=cache_id)

            if not file_bytes:
                await self._mark_not_available(cache_id)
                return AttachmentResult(not_available=True, cache_id=cache_id)

            try:
                telegram_file_id, uploaded_to_user = await self._telegram_upload(
                    file_bytes=file_bytes,
                    filename=filename,
                    subject=subject,
                    fallback_chat_id=telegram_id,
                )
            except Exception as e:
                logger.error("AttachmentService: telegram upload failed for cache_id=%d: %r", cache_id, e)
                await self._mark_failure(cache_id, f"Telegram upload error: {e}")
                return AttachmentResult(permanent=False, error=str(e), cache_id=cache_id)

            content_hash = hashlib.sha256(file_bytes).hexdigest()
            await self._mark_available(
                cache_id=cache_id,
                telegram_file_id=telegram_file_id,
                content_hash=content_hash,
                portal_filename=filename,
                file_kind=kind,
                file_size_bytes=len(file_bytes),
            )

            if uploaded_to_user:
                return AttachmentResult(delivered=True, file_kind=kind, cache_id=cache_id)

            return await self._deliver_cached(
                cache_id=cache_id,
                telegram_id=telegram_id,
                telegram_file_id=telegram_file_id,
                canonical_path=canonical_path,
                subject=subject,
                file_kind=kind,
            )

    async def _nitris_download(
        self,
        attachment_url: str,
        canonical_path: str,
        source_user_id: int,
        source_roll_number: str,
        encrypted_password: str,
    ) -> tuple[bytes, str, str]:
        """Download attachment from NITRIS strictly within gateway.acquire().

        Enforces the credential-quarantine gate: refuses to attempt a login for
        a quarantined user by loading credentials through auth_gate first.
        """
        from app.db.crypto import decrypt_password
        from app.nitris.gateway import nitris_gateway
        from app.nitris.client import NitrisClient
        from app.nitris.exceptions import LoginError
        from app.nitris.auth_gate import load_user_credentials, on_login_failure

        # Pre-check: raises CredentialsQuarantinedError for a quarantined user,
        # and fetches the freshest credentials (ignoring any stale caller-passed
        # password so a concurrent mark-invalid cannot be bypassed).
        creds = await load_user_credentials(self.session_factory, source_user_id)

        filename = attachment_basename(canonical_path)

        async with nitris_gateway.acquire():
            password = decrypt_password(creds.encrypted_password)
            client = NitrisClient()
            try:
                await nitris_gateway.login_through_gateway(
                    client, creds.roll_number, password, user_id=source_user_id
                )
                file_bytes = await client.download_attachment(attachment_url)
            except LoginError:
                await on_login_failure(source_user_id, "attachment_download_login_failed")
                raise
            finally:
                await client.close()

        if not file_bytes:
            return b"", filename, "pdf"

        # Sniff kind
        kind = "zip" if file_bytes.startswith(b"PK\x03\x04") else "pdf"
        return file_bytes, filename, kind

    async def _telegram_upload(
        self,
        file_bytes: bytes,
        filename: str,
        subject: str,
        fallback_chat_id: int,
    ) -> tuple[str, bool]:
        """Upload file to Telegram storage channel (or fallback directly to user)."""
        if len(file_bytes) > 50 * 1024 * 1024:
            raise RuntimeError("Attachment too large for Telegram upload (>50MB)")

        input_file = BufferedInputFile(file_bytes, filename=filename)
        caption = f"📎 <b>Attachment:</b> {subject[:60]}" if subject else "📎 <b>Notice Attachment</b>"

        # Try storage channel first if configured
        if self.storage_chat_id:
            try:
                msg = await self.bot.send_document(
                    chat_id=self.storage_chat_id,
                    document=input_file,
                    caption=caption,
                )
                if msg.document:
                    return msg.document.file_id, False
            except Exception as e:
                logger.warning(
                    "AttachmentService: upload to storage channel %d failed: %r — falling back to user direct upload",
                    self.storage_chat_id, e
                )

        # Fallback: upload directly to requesting user's chat
        input_file = BufferedInputFile(file_bytes, filename=filename)
        msg = await self.bot.send_document(
            chat_id=fallback_chat_id,
            document=input_file,
            caption=caption,
        )
        if not msg.document:
            raise RuntimeError("Telegram did not return document file_id")
        return msg.document.file_id, True

    async def _mark_available(
        self,
        cache_id: int,
        telegram_file_id: str,
        content_hash: str,
        portal_filename: str,
        file_kind: str,
        file_size_bytes: int,
    ) -> None:
        async with self.session_factory() as session:
            async with session.begin():
                stmt = sql_text("""
                    UPDATE attachment_caches
                    SET status = 'available',
                        telegram_file_id = :file_id,
                        content_hash = :hash,
                        portal_filename = :filename,
                        file_kind = :kind,
                        file_size_bytes = :size,
                        error_message = NULL,
                        updated_at = NOW()
                    WHERE id = :cache_id
                      AND status = 'fetch_in_progress'
                """)
                await session.execute(
                    stmt,
                    {
                        "cache_id": cache_id,
                        "file_id": telegram_file_id,
                        "hash": content_hash,
                        "filename": portal_filename,
                        "kind": file_kind,
                        "size": file_size_bytes,
                    },
                )

    async def _mark_not_available(self, cache_id: int) -> None:
        async with self.session_factory() as session:
            async with session.begin():
                stmt = sql_text("""
                    UPDATE attachment_caches
                    SET status = 'not_available',
                        updated_at = NOW()
                    WHERE id = :cache_id
                      AND status = 'fetch_in_progress'
                """)
                await session.execute(stmt, {"cache_id": cache_id})

    async def _mark_failure(self, cache_id: int, error: str) -> None:
        async with self.session_factory() as session:
            async with session.begin():
                # Read attempt count
                stmt = select(AttachmentCache.attempt_count).where(AttachmentCache.id == cache_id)
                attempts = (await session.execute(stmt)).scalar_one_or_none() or 0
                new_status = (
                    AttachmentStatus.PERMANENT_FAILURE.value
                    if attempts >= self.permanent_after
                    else AttachmentStatus.RETRYABLE_FAILURE.value
                )
                update_stmt = sql_text("""
                    UPDATE attachment_caches
                    SET status = :status,
                        error_message = :err,
                        updated_at = NOW()
                    WHERE id = :cache_id
                      AND status = 'fetch_in_progress'
                """)
                await session.execute(update_stmt, {"cache_id": cache_id, "status": new_status, "err": error[:500]})

    async def _wait_and_deliver(
        self,
        cache_id: int,
        telegram_id: int,
        canonical_path: str,
        subject: str,
    ) -> AttachmentResult:
        """Poll DB while acquiring worker downloads and uploads."""
        deadline = asyncio.get_event_loop().time() + self.wait_timeout

        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(self.wait_poll_interval)
            status, file_id, file_kind = await self._read_cache(cache_id)

            if status == AttachmentStatus.AVAILABLE.value and file_id:
                return await self._deliver_cached(
                    cache_id=cache_id,
                    telegram_id=telegram_id,
                    telegram_file_id=file_id,
                    canonical_path=canonical_path,
                    subject=subject,
                    file_kind=file_kind,
                )

            if status == AttachmentStatus.NOT_AVAILABLE.value:
                return AttachmentResult(not_available=True, cache_id=cache_id)

            if status in (
                AttachmentStatus.RETRYABLE_FAILURE.value,
                AttachmentStatus.PERMANENT_FAILURE.value,
            ):
                return AttachmentResult(
                    permanent=(status == AttachmentStatus.PERMANENT_FAILURE.value),
                    error="Acquisition failed by previous attempt",
                    cache_id=cache_id,
                )

        return AttachmentResult(in_progress=True, cache_id=cache_id)

    async def _deliver_cached(
        self,
        cache_id: int,
        telegram_id: int,
        telegram_file_id: str,
        canonical_path: str,
        subject: str,
        file_kind: Optional[str] = None,
        recover: Optional[Callable[[], Awaitable[AttachmentResult]]] = None,
    ) -> AttachmentResult:
        """Forward a cached Telegram file_id to a user.

        FloodWait retries are bounded by ``floodwait_max_retries`` and generic
        retries by ``delivery_max_retries`` (independent budgets). On a rejected
        file_id the cache row is invalidated and — when a ``recover`` callback is
        supplied — the attachment is re-acquired and re-delivered in the same
        request (mirrors QPaperService self-recovery).
        """
        caption = f"📎 <b>Attachment:</b> {subject[:60]}" if subject else "📎 <b>Notice Attachment</b>"

        async with self._get_deliver_sem():
            generic_attempt = 0
            flood_retries = 0
            while True:
                try:
                    await self.bot.send_document(
                        chat_id=telegram_id,
                        document=telegram_file_id,
                        caption=caption,
                    )
                    return AttachmentResult(delivered=True, file_kind=file_kind, cache_id=cache_id)
                except TelegramRetryAfter as e:
                    if flood_retries >= self.floodwait_max_retries:
                        return AttachmentResult(
                            delivered=False,
                            file_kind=file_kind,
                            cache_id=cache_id,
                            error=f"Telegram rate-limited (retry_after={e.retry_after}s). Try again shortly.",
                        )
                    flood_retries += 1
                    logger.warning(
                        "AttachmentService: FloodWait %ds for chat %d (retry %d/%d)",
                        e.retry_after, telegram_id, flood_retries, self.floodwait_max_retries,
                    )
                    await asyncio.sleep(e.retry_after + 0.5)
                except TelegramForbiddenError:
                    logger.warning("AttachmentService: bot blocked by user %d", telegram_id)
                    return AttachmentResult(
                        delivered=False, file_kind=file_kind, cache_id=cache_id,
                        error="Bot blocked by user — cannot deliver.",
                    )
                except TelegramBadRequest as e:
                    err_msg = str(e).lower()
                    if "wrong file identifier" in err_msg or "file_id" in err_msg:
                        logger.warning(
                            "AttachmentService: invalid file_id for cache_id=%d — invalidating and recovering",
                            cache_id,
                        )
                        await self._invalidate_file_id(cache_id)
                        if recover is not None:
                            return await recover()
                        return AttachmentResult(
                            delivered=False, file_kind=file_kind, cache_id=cache_id,
                            error="Stale cached file — please try again.",
                        )
                    return AttachmentResult(
                        delivered=False, file_kind=file_kind, cache_id=cache_id, error=str(e),
                    )
                except Exception as e:
                    generic_attempt += 1
                    if generic_attempt >= self.delivery_max_retries:
                        return AttachmentResult(
                            delivered=False, file_kind=file_kind, cache_id=cache_id, error=str(e),
                        )
                    logger.error(
                        "AttachmentService: send attempt %d/%d failed: %r",
                        generic_attempt, self.delivery_max_retries, e,
                    )
                    await asyncio.sleep(self.delivery_retry_base_delay * (2 ** (generic_attempt - 1)))

    async def _invalidate_file_id(self, cache_id: int) -> None:
        """Reset cache when Telegram rejects file_id."""
        async with self.session_factory() as session:
            async with session.begin():
                stmt = sql_text("""
                    UPDATE attachment_caches
                    SET status = 'retryable_failure',
                        telegram_file_id = NULL,
                        error_message = 'Invalidated cached file_id',
                        updated_at = NOW()
                    WHERE id = :cache_id
                """)
                await session.execute(stmt, {"cache_id": cache_id})

    async def _reap_stale_locks_loop(self) -> None:
        """Loop to clean up stale fetch_in_progress locks."""
        while not self._closing:
            try:
                await asyncio.sleep(60.0)
                await self._reap_stale_locks()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("AttachmentService: error in reaper loop: %r", e)

    async def _reap_stale_locks(self) -> None:
        """Reap stuck locks back to retryable_failure."""
        async with self.session_factory() as session:
            async with session.begin():
                stmt = sql_text("""
                    UPDATE attachment_caches
                    SET status = 'retryable_failure',
                        acquired_by = NULL,
                        error_message = COALESCE(error_message, '') || ' [stale-lock-reaped]',
                        updated_at = NOW()
                    WHERE status = 'fetch_in_progress'
                      AND acquired_at IS NOT NULL
                      AND acquired_at < NOW() - make_interval(secs => :stale_secs * 2)
                """)
                res = await session.execute(stmt, {"stale_secs": self.stale_seconds})
                if res.rowcount and res.rowcount > 0:
                    logger.warning("AttachmentService: reaped %d stale attachment lock(s)", res.rowcount)


_attachment_service: Optional[AttachmentService] = None


def get_attachment_service() -> AttachmentService:
    """Retrieve the singleton AttachmentService instance."""
    global _attachment_service
    if _attachment_service is None:
        raise RuntimeError("AttachmentService not initialized. Call init_attachment_service() first.")
    return _attachment_service


def init_attachment_service(
    bot: Bot,
    session_factory: async_sessionmaker[AsyncSession],
    storage_chat_id: int = 0,
) -> AttachmentService:
    """Initialize singleton AttachmentService and start reaper."""
    global _attachment_service
    _attachment_service = AttachmentService(
        bot=bot,
        session_factory=session_factory,
        storage_chat_id=storage_chat_id,
        stale_seconds=config.ATTACHMENT_CACHE_STALE_SECONDS,
        permanent_after=config.ATTACHMENT_CACHE_PERMANENT_AFTER,
        max_concurrent_acquisitions=config.ATTACHMENT_MAX_CONCURRENT_ACQUISITIONS,
        max_concurrent_deliveries=config.ATTACHMENT_MAX_CONCURRENT_DELIVERIES,
        wait_poll_interval=config.ATTACHMENT_WAIT_POLL_INTERVAL,
        wait_timeout=config.ATTACHMENT_WAIT_TIMEOUT,
        floodwait_max_retries=config.ATTACHMENT_FLOODWAIT_MAX_RETRIES,
        delivery_max_retries=config.ATTACHMENT_DELIVERY_MAX_RETRIES,
        delivery_retry_base_delay=config.ATTACHMENT_DELIVERY_RETRY_BASE_DELAY,
    )
    _attachment_service.start_reaper()
    return _attachment_service


async def shutdown_attachment_service() -> None:
    """Cleanly stop AttachmentService singleton."""
    global _attachment_service
    if _attachment_service is not None:
        await _attachment_service.stop_reaper()
