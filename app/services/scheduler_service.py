"""Durable per-module TTL scheduler (Phase 5).

Replaces the old "SELECT all users → sync all every 2h" architecture.

ARCHITECTURE:
    Every 30 seconds:
        1. Query module_sync_schedule for DUE rows (next_sync_at <= NOW())
           that are NOT claimed and NOT disabled.
        2. Atomically claim a bounded batch (UPDATE ... WHERE id IN (SELECT ...)
           RETURNING — same CAS pattern as event dispatcher).
        3. Enqueue each claimed row as a LOW-priority job through the job queue.
        4. The job updates next_sync_at = NOW() + module_ttl (with jitter)
           and clears scheduler_claimed_at when done.

RESTART SAFETY:
    If the scheduler crashes after claiming but before enqueuing, the
    claimed rows become reclaimable after SCHEDULER_CLAIM_STALE_SECONDS
    (default 5 min). The next cycle will re-claim and re-enqueue them.

NO MASS SYNC:
    The scheduler does NOT create tasks for all 5000 users. It only picks
    rows where next_sync_at <= NOW(). With per-module TTLs:
    - attendance (6h): ~5000/6 = ~833 users per hour = ~14 due per 30s cycle
    - inbox (15min): ~5000/15 = ~333 users per minute = ~166 due per 30s cycle
    Total: ~180 due jobs per 30s cycle, bounded by SCHEDULER_BATCH_SIZE (25).
    The rest wait for the next cycle.

NO "SLEEP 2H":
    The scheduler polls every 30 seconds. This is cheap (one indexed query)
    and ensures work is picked up promptly when due.

PER-USER BACKOFF:
    On consecutive failures, next_sync_at is pushed further into the future
    (exponential backoff up to 6x the base TTL). This prevents hammering
    NITRIS for users with broken credentials.
"""
from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timezone, timedelta
from typing import Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import config
from app.db.database import get_db_session, async_session_factory, is_db_connection_error
from app.nitris.gateway import nitris_gateway, NitrisCircuitOpenError
from app.nitris.job_queue import nitris_job_queue, Priority

logger = logging.getLogger(__name__)


async def wait_for_db_recovery(worker_name: str) -> None:
    """Blocks until the database connection is healthy."""
    backoff = 5
    attempt = 1
    has_failed = False

    while True:
        try:
            async with get_db_session() as session:
                await session.execute(text("SELECT 1"))
            if has_failed:
                logger.info("Database recovered. %s resumed.", worker_name)
            return
        except Exception as e:
            if is_db_connection_error(e):
                if not has_failed:
                    logger.error("Database connection lost. %s waiting for recovery...", worker_name)
                    has_failed = True
                logger.warning(
                    "Reconnect attempt %d for %s (waiting %ds)...",
                    attempt, worker_name, backoff,
                )
                await asyncio.sleep(backoff)
                attempt += 1
                backoff = min(backoff * 2, 60)
            else:
                raise


async def claim_due_schedule_rows(
    session_factory: async_sessionmaker[AsyncSession],
    batch_size: Optional[int] = None,
    claim_stale_seconds: Optional[int] = None,
) -> list[dict]:
    """Atomically claim a batch of due schedule rows.

    Uses UPDATE...WHERE id IN (SELECT...) RETURNING — same CAS pattern
    as the event dispatcher. This prevents duplicate scheduling across
    multiple scheduler processes (future-proof).

    Returns list of dicts: {id, user_id, module_name, consecutive_failures}.
    """
    batch_size = batch_size or config.SCHEDULER_BATCH_SIZE
    claim_stale = claim_stale_seconds or config.SCHEDULER_CLAIM_STALE_SECONDS

    async with session_factory() as session:
        async with session.begin():
            stmt = text("""
                UPDATE module_sync_schedule
                SET scheduler_claimed_at = NOW(),
                    updated_at = NOW()
                WHERE id IN (
                    SELECT id FROM module_sync_schedule
                    WHERE next_sync_at <= NOW()
                      AND last_status != 'disabled'
                      AND (
                        scheduler_claimed_at IS NULL
                        OR scheduler_claimed_at < NOW() - make_interval(secs => :stale_secs)
                      )
                    ORDER BY next_sync_at ASC
                    LIMIT :batch
                )
                RETURNING id, user_id, module_name, consecutive_failures
            """)
            result = await session.execute(stmt, {
                "batch": batch_size,
                "stale_secs": claim_stale,
            })
            rows = result.fetchall()
            return [
                {
                    "id": r[0],
                    "user_id": r[1],
                    "module_name": r[2],
                    "consecutive_failures": r[3],
                }
                for r in rows
            ]


async def update_schedule_after_job(
    session_factory: async_sessionmaker[AsyncSession],
    schedule_id: int,
    success: bool,
    error_msg: Optional[str] = None,
    module_name: Optional[str] = None,
) -> None:
    """Update the schedule row after a job completes.

    On success:
    - last_synced_at = NOW()
    - next_sync_at = NOW() + module_ttl (with jitter)
    - consecutive_failures = 0
    - last_status = 'success'

    On failure:
    - next_sync_at = NOW() + backoff (exponential, up to 6x base TTL)
    - consecutive_failures += 1
    - last_status = 'failure'
    - If consecutive_failures >= 5, set last_status = 'disabled' (stop retrying)
    """
    ttl_seconds = config.MODULE_TTL_SECONDS.get(module_name, 3600) if module_name else 3600

    if success:
        # Add jitter: ±10% of TTL to avoid synchronized bursts
        jitter = random.uniform(-0.1, 0.1) * ttl_seconds
        total_ttl = max(60, ttl_seconds + jitter)

        async with session_factory() as session:
            async with session.begin():
                await session.execute(text("""
                    UPDATE module_sync_schedule
                    SET last_synced_at = NOW(),
                        next_sync_at = NOW() + make_interval(secs => :ttl),
                        last_status = 'success',
                        last_error = NULL,
                        consecutive_failures = 0,
                        scheduler_claimed_at = NULL,
                        updated_at = NOW()
                    WHERE id = :id
                """), {"ttl": total_ttl, "id": schedule_id})
    else:
        # Exponential backoff: 1x, 2x, 4x, 6x (capped)
        async with session_factory() as session:
            row = (await session.execute(
                text("SELECT consecutive_failures FROM module_sync_schedule WHERE id = :id"),
                {"id": schedule_id},
            )).first()
            failures = row[0] + 1 if row else 1

            backoff_multiplier = min(2 ** (failures - 1), 6)
            backoff_seconds = ttl_seconds * backoff_multiplier
            # Cap backoff at 24 hours
            backoff_seconds = min(backoff_seconds, 24 * 3600)

            # After 5 consecutive failures, disable this module for this user
            new_status = "disabled" if failures >= 5 else "failure"

            async with session.begin():
                await session.execute(text("""
                    UPDATE module_sync_schedule
                    SET next_sync_at = NOW() + make_interval(secs => :backoff),
                        last_status = :status,
                        last_error = :err,
                        consecutive_failures = :failures,
                        scheduler_claimed_at = NULL,
                        updated_at = NOW()
                    WHERE id = :id
                """), {
                    "backoff": backoff_seconds,
                    "status": new_status,
                    "err": (error_msg or "Unknown error")[:1000],
                    "failures": failures,
                    "id": schedule_id,
                })


async def ensure_schedule_exists(
    session_factory: async_sessionmaker[AsyncSession],
    user_id: int,
    module_name: str,
) -> None:
    """Create a schedule row if it doesn't exist (for new users).

    Called on registration. next_sync_at defaults to NOW() so the first
    sync happens on the next scheduler cycle.
    """
    async with session_factory() as session:
        async with session.begin():
            await session.execute(text("""
                INSERT INTO module_sync_schedule (user_id, module_name, next_sync_at, last_status)
                VALUES (:uid, :mod, NOW(), 'pending')
                ON CONFLICT (user_id, module_name) DO NOTHING
            """), {"uid": user_id, "mod": module_name})


async def run_scheduler_loop(bot=None) -> None:
    """Main scheduler loop. Call via asyncio.create_task from main.py.

    Every SCHEDULER_POLL_INTERVAL seconds:
    1. Wait for DB recovery
    2. Claim due rows
    3. Enqueue each as a LOW-priority job
    4. Sleep briefly and repeat

    NO mass sync. NO "sleep 2h". NO 5000 asyncio tasks.
    """
    poll_interval = config.SCHEDULER_POLL_INTERVAL
    logger.info(
        "Module sync scheduler started: poll=%ds, batch=%d, TTLs=%s",
        poll_interval, config.SCHEDULER_BATCH_SIZE,
        {k: f"{v}s" for k, v in config.MODULE_TTL_SECONDS.items()},
    )

    while True:
        try:
            await wait_for_db_recovery("Scheduler")

            # Check circuit breaker — don't claim work if NITRIS is down
            if nitris_gateway.is_circuit_open():
                logger.debug("Scheduler: NITRIS circuit open, skipping claim cycle")
                await asyncio.sleep(poll_interval)
                continue

            # Claim due rows
            claimed = await claim_due_schedule_rows(async_session_factory)

            if not claimed:
                # No due work — sleep and check again
                await asyncio.sleep(poll_interval)
                continue

            logger.info("Scheduler claimed %d due job(s)", len(claimed))

            # Enqueue each claimed row
            for row in claimed:
                schedule_id = row["id"]
                user_id = row["user_id"]
                module_name = row["module_name"]

                # Map module_name to job_type
                job_type = f"sync_{module_name}"

                try:
                    await nitris_job_queue.enqueue(
                        job_type=job_type,
                        user_id=user_id,
                        priority=Priority.LOW,
                        payload={
                            "schedule_id": schedule_id,
                            "module_name": module_name,
                        },
                    )
                    logger.debug(
                        "Enqueued %s for user_id=%d (schedule_id=%d)",
                        job_type, user_id, schedule_id,
                    )
                except NitrisCircuitOpenError:
                    # Circuit opened mid-batch — release the claim so it
                    # can be retried next cycle
                    logger.warning(
                        "Circuit opened during enqueue — releasing schedule_id=%d",
                        schedule_id,
                    )
                    await update_schedule_after_job(
                        async_session_factory, schedule_id,
                        success=False, error_msg="circuit_open_during_enqueue",
                        module_name=module_name,
                    )
                    break  # Stop enqueuing this cycle

        except asyncio.CancelledError:
            logger.info("Scheduler cancelled")
            return
        except Exception as e:
            logger.error("Scheduler cycle error: %r", e)
            await asyncio.sleep(10)  # Brief pause before retrying

        await asyncio.sleep(poll_interval)


async def init_scheduler() -> None:
    """Register sync job handlers. Call once on startup.

    The sync handlers (sync_attendance, sync_inbox) are registered here
    and use the job_queue's worker pool. They go through the gateway.
    """
    from app.nitris.job_handlers import _update_sync_state, _mark_credentials_invalid
    from app.db.crypto import decrypt_password
    from app.db.models import User
    from app.nitris.client import NitrisClient
    from app.services.attendance_service import get_attendance_data
    from app.workers.sync_worker import sync_messages_for_user

    @nitris_job_queue.handler("sync_attendance")
    async def handle_sync_attendance(job):
        """Background attendance sync (LOW priority)."""
        user_id = job.user_id
        schedule_id = job.payload.get("schedule_id")
        module_name = job.payload.get("module_name", "attendance")

        try:
            async with nitris_gateway.acquire():
                async with async_session_factory() as session:
                    user = await session.get(User, user_id)
                    if not user or not user.credentials_valid:
                        await update_schedule_after_job(
                            async_session_factory, schedule_id,
                            success=False, error_msg="user_invalid_or_missing",
                            module_name=module_name,
                        )
                        return {"success": False, "error": "user_invalid"}
                    roll_number = user.roll_number
                    password = decrypt_password(user.encrypted_password)

                client = NitrisClient()
                try:
                    await nitris_gateway.login_through_gateway(client, roll_number, password)
                    data = await get_attendance_data(roll_number, password, client=client)

                    # Save snapshot
                    try:
                        from app.services.snapshot_service import SnapshotService
                        async with get_db_session() as session:
                            async with session.begin():
                                snapshot_service = SnapshotService(session)
                                await snapshot_service.create_snapshot_if_changed(
                                    user_id=user_id,
                                    module_name="attendance",
                                    attendance_result=data,
                                )
                    except Exception as e:
                        logger.error("Failed to save attendance snapshot: %r", e)

                    await _update_sync_state(user_id, success=True)
                finally:
                    await client.close()

            await update_schedule_after_job(
                async_session_factory, schedule_id,
                success=True, module_name=module_name,
            )
            return {"success": True}

        except NitrisCircuitOpenError as e:
            await update_schedule_after_job(
                async_session_factory, schedule_id,
                success=False, error_msg=str(e), module_name=module_name,
            )
            return {"success": False, "error": str(e)}
        except Exception as e:
            logger.error("sync_attendance failed for user_id=%d: %r", user_id, e)
            await update_schedule_after_job(
                async_session_factory, schedule_id,
                success=False, error_msg=str(e), module_name=module_name,
            )
            await _update_sync_state(user_id, success=False, error_msg=str(e))
            return {"success": False, "error": str(e)}

    @nitris_job_queue.handler("sync_inbox")
    async def handle_sync_inbox(job):
        """Background inbox sync (LOW priority)."""
        user_id = job.user_id
        schedule_id = job.payload.get("schedule_id")
        module_name = job.payload.get("module_name", "inbox")

        try:
            async with nitris_gateway.acquire():
                async with async_session_factory() as session:
                    user = await session.get(User, user_id)
                    if not user or not user.credentials_valid:
                        await update_schedule_after_job(
                            async_session_factory, schedule_id,
                            success=False, error_msg="user_invalid_or_missing",
                            module_name=module_name,
                        )
                        return {"success": False, "error": "user_invalid"}
                    roll_number = user.roll_number
                    password = decrypt_password(user.encrypted_password)

                client = NitrisClient()
                try:
                    await nitris_gateway.login_through_gateway(client, roll_number, password)
                    await sync_messages_for_user(
                        user_id, roll_number, password, None, client=client
                    )
                finally:
                    await client.close()

            await update_schedule_after_job(
                async_session_factory, schedule_id,
                success=True, module_name=module_name,
            )
            return {"success": True}

        except NitrisCircuitOpenError as e:
            await update_schedule_after_job(
                async_session_factory, schedule_id,
                success=False, error_msg=str(e), module_name=module_name,
            )
            return {"success": False, "error": str(e)}
        except Exception as e:
            logger.error("sync_inbox failed for user_id=%d: %r", user_id, e)
            await update_schedule_after_job(
                async_session_factory, schedule_id,
                success=False, error_msg=str(e), module_name=module_name,
            )
            return {"success": False, "error": str(e)}

    logger.info("Registered scheduler sync handlers: sync_attendance, sync_inbox")
