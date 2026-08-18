"""Priority Job Queue with single-flight deduplication for NITRIS operations.

Guarantees:
  - NO plain passwords in job payloads (passwords decrypted strictly inside gateway workers)
  - Priority ordering (Interactive user requests [HIGH] > Background syncs [LOW])
  - Single-flight deduplication (collapses multiple simultaneous requests for same resource)
  - Fixed worker concurrency pool
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import time
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Callable, Coroutine, Dict, Optional, Union

from aiogram import Bot
from app.config import config

logger = logging.getLogger(__name__)


class Priority(IntEnum):
    HIGH = 1     # Direct interactive user button taps (e.g. /attendance, /inbox)
    MEDIUM = 2   # Secondary operations (e.g. metadata pre-fetch, search)
    NORMAL = 2   # Alias for MEDIUM
    LOW = 3      # Periodic background crawl cycles


# Backward compatibility alias
JobPriority = Priority


@dataclass(order=True)
class NitrisJob:
    priority: int
    created_at: float = field(compare=True)
    job_type: str = field(compare=False)
    user_id: Optional[int] = field(compare=False, default=None)
    dedup_key: Optional[str] = field(compare=False, default=None)
    payload: dict = field(compare=False, default_factory=dict)
    future: Optional[asyncio.Future] = field(compare=False, default=None)


# Alias
Job = NitrisJob

HandlerFunc = Union[
    Callable[[NitrisJob], Coroutine[Any, Any, Any]],
    Callable[[dict, Any], Coroutine[Any, Any, Any]],
]


class NitrisJobQueue:
    """Manages priority execution and single-flight deduplication of portal tasks."""

    def __init__(self, gateway: Any = None, num_workers: Optional[int] = None):
        from app.nitris.gateway import nitris_gateway
        self.gateway = gateway or nitris_gateway
        self.num_workers = num_workers if num_workers is not None else config.NITRIS_JOB_WORKERS
        self._queue: asyncio.PriorityQueue[NitrisJob] = asyncio.PriorityQueue()
        self._handlers: Dict[str, HandlerFunc] = {}
        self._in_flight: Dict[str, asyncio.Future] = {}
        self._workers: list[asyncio.Task] = []
        self._lock = asyncio.Lock()
        self._bot: Optional[Bot] = None
        self._running = False

    def register_handler(self, job_type: str, handler: HandlerFunc) -> None:
        """Register an async handler for a given job type."""
        self._handlers[job_type] = handler

    def handler(self, job_type: str):
        """Decorator to register a handler for a job type."""
        def decorator(fn: HandlerFunc):
            self.register_handler(job_type, fn)
            return fn
        return decorator

    async def start(self, bot: Optional[Bot] = None) -> None:
        """Start worker pool."""
        self._bot = bot
        self._running = True
        for i in range(self.num_workers):
            task = asyncio.create_task(self._worker_loop(i), name=f"nitris-job-worker-{i}")
            self._workers.append(task)
        logger.info(
            "NITRIS Job Queue started with %d workers. Registered handlers: %s",
            self.num_workers,
            list(self._handlers.keys()),
        )

    async def stop(self) -> None:
        """Gracefully stop worker pool."""
        self._running = False
        for task in self._workers:
            task.cancel()
        if self._workers:
            await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()
        logger.info("NITRIS Job Queue stopped.")

    async def enqueue(
        self,
        job_type: str,
        user_id: Optional[Union[int, dict]] = None,
        priority: Priority = Priority.MEDIUM,
        dedup_key: Optional[str] = None,
        payload: Optional[dict] = None,
        timeout: Optional[float] = None,
    ) -> asyncio.Future:
        """Enqueue a job. Returns a future that resolves when the job completes."""
        if isinstance(user_id, dict):
            payload_data = dict(user_id)
            actual_user_id = payload_data.get("user_id")
        else:
            payload_data = dict(payload) if payload else {}
            actual_user_id = user_id

        async with self._lock:
            # Single-flight deduplication: return existing in-flight future if duplicate
            if dedup_key and dedup_key in self._in_flight:
                existing_future = self._in_flight[dedup_key]
                if not existing_future.done():
                    logger.debug("Deduplicated job '%s' with key '%s'", job_type, dedup_key)
                    return existing_future

            loop = asyncio.get_running_loop()
            future = loop.create_future()
            job = NitrisJob(
                priority=int(priority),
                created_at=time.monotonic(),
                job_type=job_type,
                user_id=actual_user_id,
                dedup_key=dedup_key,
                payload=payload_data,
                future=future,
            )

            if dedup_key:
                self._in_flight[dedup_key] = future

            await self._queue.put(job)
            return future

    async def _worker_loop(self, worker_id: int) -> None:
        while self._running:
            try:
                job = await self._queue.get()
            except asyncio.CancelledError:
                break

            handler = self._handlers.get(job.job_type)
            if not handler:
                logger.error("No handler registered for job type: %s", job.job_type)
                if job.future and not job.future.done():
                    job.future.set_exception(ValueError(f"Unknown job type: {job.job_type}"))
                self._cleanup_dedup(job.dedup_key)
                self._queue.task_done()
                continue

            try:
                sig = inspect.signature(handler)
                if len(sig.parameters) >= 2:
                    result = await handler(job.payload, self._bot)
                else:
                    result = await handler(job)

                if job.future and not job.future.done():
                    job.future.set_result(result)
            except Exception as e:
                logger.error("Error processing job %s in worker %d: %r", job.job_type, worker_id, e)
                if job.future and not job.future.done():
                    job.future.set_exception(e)
            finally:
                self._cleanup_dedup(job.dedup_key)
                self._queue.task_done()

    def _cleanup_dedup(self, dedup_key: Optional[str]) -> None:
        if dedup_key:
            self._in_flight.pop(dedup_key, None)

    def get_queue_depth(self) -> int:
        """Return the count of pending jobs in queue."""
        return self._queue.qsize()

    def get_active_dedup_count(self) -> int:
        """Return the number of in-flight single-flight operations."""
        return len(self._in_flight)

    def get_registered_handlers(self) -> list[str]:
        """Return list of registered job handler names."""
        return list(self._handlers.keys())

    def get_stats(self) -> dict:
        """Return diagnostic statistics for the job queue."""
        return {
            "queue_depth": self.get_queue_depth(),
            "active_dedup_count": self.get_active_dedup_count(),
            "registered_handlers": self.get_registered_handlers(),
            "running": self._running,
        }


# Singleton job queue instance
nitris_job_queue = NitrisJobQueue()
