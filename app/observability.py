"""Phase 0 instrumentation: latency histograms, queue metrics, job lifecycle tracing.

All metrics are in-process (no Prometheus/OTLP dependency). Designed to be
surfaced via the /status admin command and structured log lines.

Design:
  - LatencyHistogram: bucketed counts for op durations (p50/p95/p99)
  - JobLifecycleTracer: tracks per-job-type count + average duration + last run
  - GatewayMetrics: extended with latency tracking
  - All access is async-safe via a single asyncio.Lock

No external dependencies. No network IO. Pure in-memory.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Deque, Dict, Optional

logger = logging.getLogger(__name__)


@dataclass
class LatencyHistogram:
    """Bucketed latency tracker. Not thread-safe (callers must hold _lock)."""
    count: int = 0
    total_ms: float = 0.0
    min_ms: float = float("inf")
    max_ms: float = 0.0
    # Rolling window of last 100 samples for p50/p95/p99
    samples: Deque[float] = field(default_factory=lambda: deque(maxlen=100))

    def record(self, duration_seconds: float) -> None:
        ms = duration_seconds * 1000.0
        self.count += 1
        self.total_ms += ms
        if ms < self.min_ms:
            self.min_ms = ms
        if ms > self.max_ms:
            self.max_ms = ms
        self.samples.append(ms)

    def percentile(self, p: float) -> float:
        if not self.samples:
            return 0.0
        sorted_samples = sorted(self.samples)
        idx = max(0, min(len(sorted_samples) - 1, int(p * len(sorted_samples))))
        return sorted_samples[idx]

    def avg(self) -> float:
        return self.total_ms / self.count if self.count > 0 else 0.0

    def snapshot(self) -> dict:
        return {
            "count": self.count,
            "avg_ms": round(self.avg(), 2),
            "p50_ms": round(self.percentile(0.50), 2),
            "p95_ms": round(self.percentile(0.95), 2),
            "p99_ms": round(self.percentile(0.99), 2),
            "min_ms": round(self.min_ms, 2) if self.min_ms != float("inf") else 0.0,
            "max_ms": round(self.max_ms, 2),
        }


@dataclass
class JobTypeStats:
    completed: int = 0
    failed: int = 0
    active: int = 0
    last_run_at: Optional[float] = None
    last_duration_ms: Optional[float] = None
    last_error: Optional[str] = None
    histogram: LatencyHistogram = field(default_factory=LatencyHistogram)

    def snapshot(self) -> dict:
        return {
            "completed": self.completed,
            "failed": self.failed,
            "active": self.active,
            "last_run_at": self.last_run_at,
            "last_duration_ms": self.last_duration_ms,
            "last_error": self.last_error,
            "latency": self.histogram.snapshot(),
        }


class MetricsRegistry:
    """Singleton registry for all instrumentation. Async-safe."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._gateway_latencies: LatencyHistogram = LatencyHistogram()
        self._login_latencies: LatencyHistogram = LatencyHistogram()
        self._job_stats: Dict[str, JobTypeStats] = defaultdict(JobTypeStats)
        self._callback_latencies: LatencyHistogram = LatencyHistogram()
        self._started_at: float = time.monotonic()

    async def record_gateway_op(self, duration_seconds: float, is_login: bool = False) -> None:
        async with self._lock:
            self._gateway_latencies.record(duration_seconds)
            if is_login:
                self._login_latencies.record(duration_seconds)

    async def record_callback_latency(self, duration_seconds: float) -> None:
        async with self._lock:
            self._callback_latencies.record(duration_seconds)

    async def job_started(self, job_type: str) -> None:
        async with self._lock:
            stats = self._job_stats[job_type]
            stats.active += 1

    async def job_completed(self, job_type: str, duration_seconds: float, error: Optional[str] = None) -> None:
        async with self._lock:
            stats = self._job_stats[job_type]
            stats.active = max(0, stats.active - 1)
            stats.last_run_at = time.time()
            stats.last_duration_ms = round(duration_seconds * 1000, 2)
            stats.histogram.record(duration_seconds)
            if error is None:
                stats.completed += 1
                stats.last_error = None
            else:
                stats.failed += 1
                stats.last_error = error[:200]

    async def snapshot(self) -> dict:
        async with self._lock:
            return {
                "uptime_seconds": round(time.monotonic() - self._started_at, 1),
                "gateway_latency": self._gateway_latencies.snapshot(),
                "login_latency": self._login_latencies.snapshot(),
                "callback_latency": self._callback_latencies.snapshot(),
                "jobs": {jt: stats.snapshot() for jt, stats in self._job_stats.items()},
            }


# Singleton
metrics = MetricsRegistry()


async def timed_gateway_op(op_name: str, coro, *, is_login: bool = False):
    """Helper to time a gateway operation. Usage:
        result = await timed_gateway_op("attendance_refresh", some_coro())
    """
    start = time.monotonic()
    try:
        result = await coro
        await metrics.record_gateway_op(time.monotonic() - start, is_login=is_login)
        return result
    except Exception:
        await metrics.record_gateway_op(time.monotonic() - start, is_login=is_login)
        raise


async def timed_callback(coro):
    """Helper to time a Telegram callback handler end-to-end."""
    start = time.monotonic()
    try:
        result = await coro
        return result
    finally:
        await metrics.record_callback_latency(time.monotonic() - start)
