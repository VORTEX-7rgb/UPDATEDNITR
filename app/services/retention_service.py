"""Retention sweeper — bounds the append-only snapshots and events tables.

Why this exists:
  - ``snapshots`` grows by one full JSONB row per detected state change. Every
    consumer (attendance dashboard, briefing, papers subject list) reads ONLY
    the latest row per (user, module); superseded rows are dead weight that
    bloats the table, its indexes, and vacuum time at scale.
  - ``events`` keeps delivered and permanently-failed notification rows forever.

Safety contract:
  - The newest RETENTION_SNAPSHOT_KEEP rows per (user_id, module_name) are
    ALWAYS preserved, so "latest snapshot" reads never regress.
  - Only terminal events (sent=true OR permanent_failure=true) past a grace
    window are deleted — pending/in-flight notifications are untouchable.
  - Deletes run in SMALL batched transactions with pauses between batches:
    no long-running locks, no RAM blowup (nothing is materialized in Python —
    ranking happens inside the database), autovacuum stays ahead.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from app.config import config

logger = logging.getLogger(__name__)


class RetentionService:
    """Periodic, batched purger for superseded snapshots and terminal events."""

    def __init__(self, session_factory) -> None:
        self._session_factory = session_factory
        self._task: Optional[asyncio.Task] = None

    async def purge_superseded_snapshots(self) -> int:
        """Drain all snapshots past the keep-window, batch by batch. Returns total deleted."""
        from app.db.repositories.snapshot_repository import SnapshotRepository

        total = 0
        while True:
            async with self._session_factory() as session:
                async with session.begin():
                    deleted = await SnapshotRepository(session).purge_superseded_batch(
                        keep_per_key=config.RETENTION_SNAPSHOT_KEEP,
                        limit=config.RETENTION_DELETE_BATCH,
                    )
            total += deleted
            if deleted < config.RETENTION_DELETE_BATCH:
                return total
            # Full batch — likely more victims; yield briefly before continuing.
            await asyncio.sleep(config.RETENTION_BATCH_PAUSE_SECONDS)

    async def purge_terminal_events(self) -> int:
        """Drain terminal events older than the grace window, batch by batch."""
        from app.db.repositories.event_repository import EventRepository

        cutoff = datetime.now(timezone.utc) - timedelta(days=config.RETENTION_EVENT_DAYS)
        total = 0
        while True:
            async with self._session_factory() as session:
                async with session.begin():
                    deleted = await EventRepository(session).purge_terminal_batch(
                        older_than=cutoff,
                        limit=config.RETENTION_DELETE_BATCH,
                    )
            total += deleted
            if deleted < config.RETENTION_DELETE_BATCH:
                return total
            await asyncio.sleep(config.RETENTION_BATCH_PAUSE_SECONDS)

    async def run_once(self) -> dict[str, int]:
        """One full sweep of both tables. Never raises for expected DB errors — logs them."""
        results = {"snapshots": 0, "events": 0}
        try:
            results["snapshots"] = await self.purge_superseded_snapshots()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Retention sweep (snapshots) failed: %r", exc)
        try:
            results["events"] = await self.purge_terminal_events()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Retention sweep (events) failed: %r", exc)
        logger.info(
            "Retention sweep done: %d snapshot(s), %d event(s) purged.",
            results["snapshots"],
            results["events"],
        )
        return results

    async def run_periodic(self) -> None:
        """Sweep loop. Call via asyncio.create_task from main.py."""
        while True:
            await self.run_once()
            await asyncio.sleep(config.RETENTION_INTERVAL_SECONDS)

    def start(self) -> None:
        """Start the background sweep task (idempotent)."""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.run_periodic())
            logger.info(
                "Retention sweeper started (interval=%ds, snapshot_keep=%d, event_days=%d).",
                config.RETENTION_INTERVAL_SECONDS,
                config.RETENTION_SNAPSHOT_KEEP,
                config.RETENTION_EVENT_DAYS,
            )

    async def stop(self) -> None:
        """Cancel and await the background sweep task."""
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None


retention_service: Optional[RetentionService] = None


def init_retention_service(session_factory) -> RetentionService:
    """Create + start the global retention sweeper. Call once from main.py."""
    global retention_service
    retention_service = RetentionService(session_factory)
    retention_service.start()
    return retention_service


async def shutdown_retention_service() -> None:
    """Stop the global retention sweeper. Safe to call even if never started."""
    if retention_service is not None:
        await retention_service.stop()
