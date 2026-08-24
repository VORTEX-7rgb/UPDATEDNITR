"""Priority Job Queue with worker lane isolation, single-flight deduplication, and retry.

Architecture:
  - TWO queues: interactive (HIGH) and background (MEDIUM/LOW).
  - Dedicated interactive workers for HIGH priority (user button taps).
  - Dedicated background workers for MEDIUM/LOW (periodic background syncs).
  - Single-worker mode uses shared worker loop for strict priority ordering.
  - Phase 6.4: Exponential backoff retry for transient errors.
  - Phase 7.3: cancel_dedup() to cancel in-flight futures.
  - Hard queue bounds to prevent memory exhaustion.
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
from app.observability import metrics

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
    """Manages priority execution, worker lane isolation, and single-flight dedup."""

    def __init__(
        self,
        gateway: Any = None,
        num_workers: Optional[int] = None,
        interactive_workers: Optional[int] = None,
        max_queue_depth: Optional[int] = None,
    ):
        from app.nitris.gateway import nitris_gateway
        self.gateway = gateway or nitris_gateway
        total_workers = num_workers if num_workers is not None else config.NITRIS_JOB_WORKERS

        if total_workers == 0:
            self.num_interactive_workers = 0
            self.num_background_workers = 0
            self.shared_workers = 0
        elif total_workers == 1:
            self.num_interactive_workers = 0
            self.num_background_workers = 0
            self.shared_workers = 1
        else:
            if interactive_workers is not None:
                self.num_interactive_workers = interactive_workers
            else:
                self.num_interactive_workers = min(config.NITRIS_INTERACTIVE_WORKERS, total_workers - 1)
            self.num_background_workers = max(1, total_workers - self.num_interactive_workers)
            self.shared_workers = 0

        self.num_workers = self.num_interactive_workers + self.num_background_workers + self.shared_workers
        self.interactive_workers = self.num_interactive_workers
        self.background_workers = self.num_background_workers
        self.max_queue_depth = (
            max_queue_depth if max_queue_depth is not None else config.NITRIS_JOB_QUEUE_MAX_DEPTH
        )

        self._interactive_queue: asyncio.PriorityQueue[NitrisJob] = asyncio.PriorityQueue()
        self._background_queue: asyncio.PriorityQueue[NitrisJob] = asyncio.PriorityQueue()
        # Backward compatibility alias for tests inspecting _queue
        self._queue = self._background_queue

        self._job_event = asyncio.Event()
        self._handlers: Dict[str, HandlerFunc] = {}
        # PERF: inspect.signature() is computed ONCE per handler at
        # registration instead of on EVERY job execution (it is surprisingly
        # expensive and this runs for all ~thousands of jobs/hour).
        self._handler_min_params: Dict[str, int] = {}
        self._in_flight: Dict[str, asyncio.Future] = {}
        self._workers: list[asyncio.Task] = []
        self._lock = asyncio.Lock()
        self._bot: Optional[Bot] = None
        self._running = False

    def register_handler(self, job_type: str, handler: HandlerFunc) -> None:
        """Register an async handler for a given job type."""
        self._handlers[job_type] = handler
        try:
            self._handler_min_params[job_type] = len(inspect.signature(handler).parameters)
        except (TypeError, ValueError):
            self._handler_min_params[job_type] = 2  # assume payload+bot contract

    def handler(self, job_type: str):
        """Decorator to register a handler for a job type."""
        def decorator(fn: HandlerFunc):
            self.register_handler(job_type, fn)
            return fn
        return decorator

    async def start(self, bot: Optional[Bot] = None) -> None:
        """Start worker pool across interactive and background lanes."""
        self._bot = bot
        self._running = True

        for i in range(self.num_interactive_workers):
            task = asyncio.create_task(
                self._worker_loop(self._interactive_queue, i, "interactive"),
                name=f"nitris-interactive-{i}",
            )
            self._workers.append(task)

        for i in range(self.num_background_workers):
            task = asyncio.create_task(
                self._worker_loop(self._background_queue, i, "background"),
                name=f"nitris-bg-{i}",
            )
            self._workers.append(task)

        for i in range(self.shared_workers):
            task = asyncio.create_task(
                self._shared_worker_loop(i, "shared"),
                name=f"nitris-shared-{i}",
            )
            self._workers.append(task)

        logger.info(
            "NITRIS Job Queue started: %d interactive + %d background + %d shared = %d total workers. Handlers: %s",
            self.num_interactive_workers,
            self.num_background_workers,
            self.shared_workers,
            self.num_workers,
            list(self._handlers.keys()),
        )

    async def stop(self) -> None:
        """Gracefully stop worker pool."""
        self._running = False
        self._job_event.set()
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
        """Enqueue a job into the appropriate worker lane."""
        if isinstance(user_id, dict):
            payload_data = dict(user_id)
            actual_user_id = payload_data.get("user_id")
        else:
            payload_data = dict(payload) if payload else {}
            actual_user_id = user_id

        async with self._lock:
            # Hard queue depth bound. Background (MEDIUM/LOW) admission stops
            # at the cap; interactive HIGH taps bypass it — up to an absolute
            # safety valve at 2× cap — so a LOW/prewarm surge filling the
            # shared budget can never reject a user-facing tap while dedicated
            # interactive workers sit idle.
            depth = self.get_queue_depth()
            if priority == Priority.HIGH:
                if depth >= self.max_queue_depth * 2:
                    raise RuntimeError(
                        f"NITRIS job queue saturated (depth={depth} >= "
                        f"{self.max_queue_depth * 2}). NITRIS may be down."
                    )
            elif depth >= self.max_queue_depth:
                raise RuntimeError(
                    f"NITRIS job queue full (depth={depth} >= "
                    f"{self.max_queue_depth}). NITRIS may be down."
                )

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

            if priority == Priority.HIGH:
                await self._interactive_queue.put(job)
            else:
                await self._background_queue.put(job)

            self._job_event.set()
            return future

    async def _worker_loop(
        self, queue: asyncio.PriorityQueue[NitrisJob], worker_id: int, lane: str
    ) -> None:
        while self._running:
            try:
                job = await queue.get()
            except asyncio.CancelledError:
                break

            await self._run_job(job, queue, worker_id, lane)

    async def _shared_worker_loop(self, worker_id: int, lane: str) -> None:
        while self._running:
            try:
                if not self._interactive_queue.empty():
                    job = self._interactive_queue.get_nowait()
                    queue = self._interactive_queue
                elif not self._background_queue.empty():
                    job = self._background_queue.get_nowait()
                    queue = self._background_queue
                else:
                    self._job_event.clear()
                    if not self._interactive_queue.empty():
                        job = self._interactive_queue.get_nowait()
                        queue = self._interactive_queue
                    elif not self._background_queue.empty():
                        job = self._background_queue.get_nowait()
                        queue = self._background_queue
                    else:
                        await self._job_event.wait()
                        continue
            except asyncio.CancelledError:
                break

            await self._run_job(job, queue, worker_id, lane)

    async def _run_job(
        self, job: NitrisJob, queue: asyncio.PriorityQueue[NitrisJob], worker_id: int, lane: str
    ) -> None:
        handler = self._handlers.get(job.job_type)
        if not handler:
            logger.error("No handler registered for job type: %s", job.job_type)
            if job.future and not job.future.done():
                job.future.set_exception(ValueError(f"Unknown job type: {job.job_type}"))
            self._cleanup_dedup(job.dedup_key)
            queue.task_done()
            return

        start_time = time.monotonic()
        try:
            try:
                await metrics.job_started(job.job_type)
            except Exception:
                pass

            min_params = self._handler_min_params.get(job.job_type)
            if min_params is None:
                # Handler swapped in without register_handler — compute once now.
                try:
                    min_params = len(inspect.signature(handler).parameters)
                except (TypeError, ValueError):
                    min_params = 2
                self._handler_min_params[job.job_type] = min_params
            if min_params >= 2:
                result = await handler(job.payload, self._bot)
            else:
                result = await handler(job)

            if job.future and not job.future.done():
                job.future.set_result(result)

            try:
                await metrics.job_completed(job.job_type, time.monotonic() - start_time, error=None)
            except Exception:
                pass

        except Exception as e:
            duration = time.monotonic() - start_time
            try:
                await metrics.job_completed(job.job_type, duration, error=str(e))
            except Exception:
                pass

            # Phase 6.4: Retry with exponential backoff for transient errors.
            # PERF (retry-storm fix): LoginUnavailableError is deliberately
            # PERMANENT here. client.login() already retries the portal 3×
            # internally while HOLDING a gateway slot; queue-level retries on
            # top of that used to multiply into ~9 full login sequences per
            # operation during an outage. Work-phase transient errors (timeouts,
            # workflow faults after a successful login) still retry normally.
            from app.nitris.exceptions import (
                LoginError,
                CredentialsQuarantinedError,
                LoginUnavailableError,
            )
            is_permanent = isinstance(
                e, (LoginError, CredentialsQuarantinedError, LoginUnavailableError)
            )
            attempt_count = job.payload.get("_retry_attempt", 0)
            if is_permanent or attempt_count >= config.JOB_MAX_RETRIES:
                logger.error(
                    "Job %s failed permanently in %s worker %d after %d attempts: %r",
                    job.job_type, lane, worker_id, attempt_count + 1, e,
                )
                if job.future and not job.future.done():
                    job.future.set_exception(e)
            else:
                # Re-enqueue with backoff
                backoff = config.JOB_RETRY_BASE_DELAY * (2 ** attempt_count)
                logger.warning(
                    "Job %s failed in %s worker %d (attempt %d/%d): %r — retrying in %.1fs",
                    job.job_type, lane, worker_id, attempt_count + 1,
                    config.JOB_MAX_RETRIES, e, backoff,
                )
                new_payload = dict(job.payload)
                new_payload["_retry_attempt"] = attempt_count + 1
                # Mark the job so `finally` keeps the single-flight dedup entry
                # alive across the backoff window - identical requests arriving
                # during that window must JOIN this retry, not race a duplicate.
                job._retry_scheduled = True
                from app.utils import spawn_tracked
                spawn_tracked(self._schedule_retry(
                    job.job_type, job.user_id, job.priority, job.dedup_key,
                    new_payload, backoff, job.future,
                ), name=f"job-retry-{job.job_type}")
        finally:
            if not getattr(job, "_retry_scheduled", False):
                self._cleanup_dedup(job.dedup_key)
            try:
                queue.task_done()
            except ValueError:
                pass

    async def _schedule_retry(
        self, job_type: str, user_id: Optional[int], priority: int,
        dedup_key: Optional[str], payload: dict, backoff: float,
        original_future: Optional[asyncio.Future],
    ) -> None:
        """Sleep for backoff seconds, then re-enqueue the job.

        Owns the single-flight dedup entry for the whole backoff+execution
        window and releases it when the chain settles (result, exception, or
        enqueue rejection).
        """
        try:
            await asyncio.sleep(backoff)
            try:
                retry_future = await self.enqueue(
                    job_type=job_type,
                    user_id=user_id,
                    priority=Priority(priority),
                    dedup_key=dedup_key,
                    payload=payload,
                )
                try:
                    result = await retry_future
                    if original_future and not original_future.done():
                        original_future.set_result(result)
                except Exception as e:
                    if original_future and not original_future.done():
                        original_future.set_exception(e)
            except Exception as e:
                logger.error("Retry enqueue failed for %s: %r", job_type, e)
                if original_future and not original_future.done():
                    original_future.set_exception(e)
        finally:
            self._cleanup_dedup(dedup_key)

    def cancel_dedup(self, dedup_key: str) -> bool:
        """Phase 7.3: Cancel an in-flight job by dedup_key. Returns True if
        a job was cancelled, False if no in-flight job matches."""
        if dedup_key in self._in_flight:
            future = self._in_flight[dedup_key]
            if not future.done():
                future.cancel()
                logger.info("Cancelled in-flight job with dedup_key=%s", dedup_key)
                return True
        return False

    def _cleanup_dedup(self, dedup_key: Optional[str]) -> None:
        if dedup_key:
            self._in_flight.pop(dedup_key, None)

    def get_interactive_queue_depth(self) -> int:
        return self._interactive_queue.qsize()

    def get_background_queue_depth(self) -> int:
        return self._background_queue.qsize()

    def get_queue_depth(self) -> int:
        """Return total pending jobs across both lanes."""
        return self._interactive_queue.qsize() + self._background_queue.qsize()

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
            "interactive_queue_depth": self.get_interactive_queue_depth(),
            "background_queue_depth": self.get_background_queue_depth(),
            "active_dedup_count": self.get_active_dedup_count(),
            "interactive_workers": self.num_interactive_workers,
            "background_workers": self.num_background_workers,
            "shared_workers": self.shared_workers,
            "total_workers": self.num_workers,
            "registered_handlers": self.get_registered_handlers(),
            "running": self._running,
        }


# Singleton job queue instance
nitris_job_queue = NitrisJobQueue()
