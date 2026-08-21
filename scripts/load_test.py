#!/usr/bin/env python3
"""Phase 8: Progressive load test for NITRClaw.

Simulates realistic user actions (registration, /attendance, /inbox, /papers,
attachment download, deregistration) against the bot's job queue + gateway,
WITHOUT touching real Telegram or real NITRIS. Both are mocked with
realistic latency profiles.

Usage:
    # 100 users over 5 minutes
    python scripts/load_test.py --users 100 --duration 300

    # 1000 users over 15 minutes
    python scripts/load_test.py --users 1000 --duration 900

    # Quick smoke test
    python scripts/load_test.py --users 20 --duration 60
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import random
import statistics
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

sys.path.insert(0, ".")

from app.config import config
from app.nitris.job_queue import NitrisJobQueue, Priority, nitris_job_queue
from app.nitris.gateway import nitris_gateway, CircuitState
from app.observability import metrics

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("load_test")


# ─── Latency profiles (mock NITRIS + DB) ──────────────────────────────

NITRIS_LATENCY_MS_FAST = (150, 400)     # 80% of requests
NITRIS_LATENCY_MS_SLOW = (800, 2500)   # 20% of requests
NITRIS_FAILURE_RATE = 0.02             # 2% transient failures

DB_LATENCY_MS = (5, 30)                # mock DB round-trip


async def mock_nitris_op(op_name: str = "default") -> dict:
    """Simulate a NITRIS operation with realistic latency."""
    if random.random() < 0.2:
        latency = random.uniform(*NITRIS_LATENCY_MS_SLOW) / 1000
    else:
        latency = random.uniform(*NITRIS_LATENCY_MS_FAST) / 1000
    await asyncio.sleep(latency)
    if random.random() < NITRIS_FAILURE_RATE:
        raise RuntimeError(f"transient_nitris_error ({op_name})")
    return {"ok": True, "latency_ms": int(latency * 1000)}


async def mock_db_op() -> None:
    """Simulate a DB round-trip."""
    latency = random.uniform(*DB_LATENCY_MS) / 1000
    await asyncio.sleep(latency)


# ─── Test metrics collector ───────────────────────────────────────────

@dataclass
class TestMetrics:
    callback_latencies: list[float] = field(default_factory=list)        # seconds
    queue_wait_times: list[float] = field(default_factory=list)         # seconds
    nitris_request_latencies: list[float] = field(default_factory=list)  # seconds
    queue_depth_samples: list[tuple[float, int, int]] = field(default_factory=list)  # (t, interactive_depth, bg_depth)
    gateway_saturation_samples: list[tuple[float, int]] = field(default_factory=list)  # (t, active_requests)
    action_counts: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    action_success: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    action_failure: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    retry_count: int = 0
    error_count: int = 0
    start_time: float = 0.0

    def percentile(self, values: list[float], p: float) -> float:
        if not values:
            return 0.0
        sorted_vals = sorted(values)
        idx = max(0, min(len(sorted_vals) - 1, int(p * len(sorted_vals))))
        return sorted_vals[idx]

    def summary(self) -> dict:
        return {
            "callback_latency_ms": {
                "p50": int(self.percentile(self.callback_latencies, 0.50) * 1000),
                "p95": int(self.percentile(self.callback_latencies, 0.95) * 1000),
                "p99": int(self.percentile(self.callback_latencies, 0.99) * 1000),
            },
            "queue_wait_ms": {
                "p50": int(self.percentile(self.queue_wait_times, 0.50) * 1000),
                "p95": int(self.percentile(self.queue_wait_times, 0.95) * 1000),
            },
            "nitris_request_ms": {
                "p50": int(self.percentile(self.nitris_request_latencies, 0.50) * 1000),
                "p95": int(self.percentile(self.nitris_request_latencies, 0.95) * 1000),
            },
            "action_counts": dict(self.action_counts),
            "action_success": dict(self.action_success),
            "action_failure": dict(self.action_failure),
            "success_rates": {
                k: round(self.action_success.get(k, 0) / max(1, self.action_counts.get(k, 1)) * 100, 1)
                for k in self.action_counts
            },
            "retry_count": self.retry_count,
            "error_count": self.error_count,
            "peak_gateway_saturation": max((s[1] for s in self.gateway_saturation_samples), default=0),
            "peak_interactive_queue_depth": max((s[1] for s in self.queue_depth_samples), default=0),
            "peak_background_queue_depth": max((s[2] for s in self.queue_depth_samples), default=0),
            "duration_seconds": round(time.monotonic() - self.start_time, 1),
        }


# ─── Test queue: instruments the job_queue to record metrics ────────

_test_metrics = TestMetrics()


def install_queue_instrumentation(queue: NitrisJobQueue) -> None:
    """Sample queue depth and gateway saturation."""
    async def sampler():
        while True:
            await asyncio.sleep(0.2)
            t = time.monotonic() - _test_metrics.start_time
            _test_metrics.queue_depth_samples.append(
                (t, queue.get_interactive_queue_depth(), queue.get_background_queue_depth())
            )
            _test_metrics.gateway_saturation_samples.append(
                (t, nitris_gateway.metrics.active_requests)
            )

    asyncio.create_task(sampler())


# ─── Realistic user actions ──────────────────────────────────────────

async def action_register(queue: NitrisJobQueue, user_id: int) -> bool:
    """Simulate /register: NITRIS login + attendance fetch + DB writes."""
    _test_metrics.action_counts["register"] += 1
    t0 = time.monotonic()
    try:
        future = await queue.enqueue(
            "sync_onboarding", user_id=user_id, priority=Priority.LOW,
            dedup_key=f"onboarding:user:{user_id}", payload={},
        )
        await asyncio.wait_for(future, timeout=30.0)
        _test_metrics.action_success["register"] += 1
        return True
    except Exception:
        _test_metrics.action_failure["register"] += 1
        _test_metrics.error_count += 1
        return False
    finally:
        _test_metrics.callback_latencies.append(time.monotonic() - t0)


async def action_attendance(queue: NitrisJobQueue, user_id: int) -> bool:
    """Simulate /attendance button tap (HIGH priority)."""
    _test_metrics.action_counts["attendance"] += 1
    t0 = time.monotonic()
    try:
        future = await queue.enqueue(
            "attendance_refresh", user_id=user_id, priority=Priority.HIGH,
            payload={"callback_chat_id": user_id, "callback_message_id": 1},
        )
        await asyncio.wait_for(future, timeout=15.0)
        _test_metrics.action_success["attendance"] += 1
        return True
    except Exception:
        _test_metrics.action_failure["attendance"] += 1
        return False
    finally:
        _test_metrics.callback_latencies.append(time.monotonic() - t0)


async def action_inbox(queue: NitrisJobQueue, user_id: int) -> bool:
    """Simulate /inbox button tap (HIGH priority)."""
    _test_metrics.action_counts["inbox"] += 1
    t0 = time.monotonic()
    try:
        future = await queue.enqueue(
            "inbox_refresh", user_id=user_id, priority=Priority.HIGH,
            dedup_key=f"inbox_refresh:user:{user_id}", payload={},
        )
        await asyncio.wait_for(future, timeout=15.0)
        _test_metrics.action_success["inbox"] += 1
        return True
    except Exception:
        _test_metrics.action_failure["inbox"] += 1
        return False
    finally:
        _test_metrics.callback_latencies.append(time.monotonic() - t0)


async def action_papers(queue: NitrisJobQueue, user_id: int) -> bool:
    """Simulate /papers button tap (MEDIUM priority)."""
    _test_metrics.action_counts["papers"] += 1
    t0 = time.monotonic()
    try:
        future = await queue.enqueue(
            "qp_metadata_fetch", user_id=user_id, priority=Priority.MEDIUM,
            dedup_key=f"qp_metadata:user:{user_id}:CS2001:2025-26",
            payload={"academic_year": "2025-26/Autumn", "subject_code": "CS2001"},
        )
        await asyncio.wait_for(future, timeout=30.0)
        _test_metrics.action_success["papers"] += 1
        return True
    except Exception:
        _test_metrics.action_failure["papers"] += 1
        return False
    finally:
        _test_metrics.callback_latencies.append(time.monotonic() - t0)


async def user_session(queue: NitrisJobQueue, user_id: int, duration_sec: float):
    """Simulate one user's session over `duration_sec` seconds."""
    actions = [
        (action_register, 1),       # always register first
        (action_attendance, 0.3),   # 30% chance after register
        (action_inbox, 0.5),        # 50% chance
        (action_papers, 0.2),       # 20% chance
        (action_attendance, 0.1),   # 10% chance again
        (action_inbox, 0.3),        # 30% chance again
    ]
    end_time = time.monotonic() + duration_sec
    while time.monotonic() < end_time:
        for action_fn, prob in actions:
            if random.random() < prob:
                await action_fn(queue, user_id)
                await asyncio.sleep(random.uniform(1.0, 15.0))
        if time.monotonic() >= end_time:
            break


# ─── Mock handlers (replace real NITRIS handlers) ────────────────────

def register_mock_handlers(queue: NitrisJobQueue) -> None:
    """Replace real handlers with mock versions that simulate NITRIS work."""

    async def mock_attendance_refresh(job):
        async with nitris_gateway.acquire():
            t0 = time.monotonic()
            await mock_nitris_op("attendance_refresh")
            _test_metrics.nitris_request_latencies.append(time.monotonic() - t0)
            await mock_db_op()
        return {"success": True}

    async def mock_inbox_refresh(job):
        async with nitris_gateway.acquire():
            t0 = time.monotonic()
            await mock_nitris_op("inbox_refresh")
            await asyncio.gather(*[mock_nitris_op("inbox_detail") for _ in range(5)])
            _test_metrics.nitris_request_latencies.append(time.monotonic() - t0)
        await mock_db_op()
        return {"success": True}

    async def mock_sync_onboarding(job):
        async with nitris_gateway.acquire():
            t0 = time.monotonic()
            await mock_nitris_op("login")
            await mock_nitris_op("inbox_list")
            await asyncio.gather(*[mock_nitris_op("inbox_detail") for _ in range(5)])
            await mock_nitris_op("timetable_fetch")
            _test_metrics.nitris_request_latencies.append(time.monotonic() - t0)
        await mock_db_op()
        return {"success": True, "modules": {"inbox": {}, "timetable": {}}}

    async def mock_qp_metadata_fetch(job):
        async with nitris_gateway.acquire():
            t0 = time.monotonic()
            await mock_nitris_op("qp_metadata")
            _test_metrics.nitris_request_latencies.append(time.monotonic() - t0)
        return {"success": True, "parsed_records": []}

    queue.register_handler("attendance_refresh", mock_attendance_refresh)
    queue.register_handler("inbox_refresh", mock_inbox_refresh)
    queue.register_handler("sync_onboarding", mock_sync_onboarding)
    queue.register_handler("qp_metadata_fetch", mock_qp_metadata_fetch)


# ─── Test runner ──────────────────────────────────────────────────────

async def run_load_test(num_users: int, duration_sec: int) -> dict:
    """Run a load test with `num_users` users over `duration_sec` seconds."""
    print(f"\n{'=' * 70}")
    print(f"  LOAD TEST: {num_users} users over {duration_sec}s ({duration_sec/60:.1f} min)")
    print(f"{'=' * 70}\n")

    _test_metrics.start_time = time.monotonic()

    nitris_gateway._reset_metrics_for_testing()

    queue = NitrisJobQueue(gateway=nitris_gateway, num_workers=config.NITRIS_JOB_WORKERS)
    register_mock_handlers(queue)
    install_queue_instrumentation(queue)

    await queue.start()
    print(f"  Queue started: {queue.num_interactive_workers} interactive + "
          f"{queue.num_background_workers} background workers")
    print(f"  Gateway: max_concurrent={nitris_gateway.current_max_concurrent}, "
          f"login_interval={nitris_gateway.current_login_interval}s")
    print()

    stagger_interval = duration_sec / num_users

    tasks = []
    for i in range(num_users):
        session_duration = random.uniform(60, min(180, duration_sec))
        tasks.append(user_session(queue, i + 1, session_duration))
        await asyncio.sleep(stagger_interval * 0.5)

    try:
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=duration_sec * 1.2)
    except asyncio.TimeoutError:
        print(f"  (Some users still active at duration limit — that's OK)")

    print(f"\n  Draining queue ({queue.get_queue_depth()} pending)...")
    drain_start = time.monotonic()
    while queue.get_queue_depth() > 0 and time.monotonic() - drain_start < 60:
        await asyncio.sleep(1.0)

    await queue.stop()

    obs_metrics = await metrics.snapshot()
    summary = _test_metrics.summary()
    summary["observability"] = obs_metrics

    return summary


def print_summary(summary: dict, num_users: int, duration_sec: int):
    """Pretty-print the test results."""
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    print(f"\n{'=' * 70}")
    print(f"  RESULTS: {num_users} users / {duration_sec}s")
    print(f"{'=' * 70}\n")

    cl = summary["callback_latency_ms"]
    qw = summary["queue_wait_ms"]
    nr = summary["nitris_request_ms"]
    sr = summary["success_rates"]

    print(f"  LATENCY (ms)")
    print(f"     Callback:    p50={cl['p50']:>5}   p95={cl['p95']:>5}   p99={cl['p99']:>5}")
    print(f"     Queue wait:   p50={qw['p50']:>5}   p95={qw['p95']:>5}")
    print(f"     NITRIS req:   p50={nr['p50']:>5}   p95={nr['p95']:>5}")
    print()

    print(f"  SUCCESS RATES")
    for action, rate in sr.items():
        total = summary["action_counts"].get(action, 0)
        ok = summary["action_success"].get(action, 0)
        fail = summary["action_failure"].get(action, 0)
        marker = "[OK]  " if rate >= 95 else ("[WARN]" if rate >= 80 else "[FAIL]")
        print(f"     {marker} {action:<20} {rate:>5.1f}%  ({ok}/{total} ok, {fail} failed)")
    print()

    print(f"  SATURATION")
    print(f"     Peak gateway concurrency: {summary['peak_gateway_saturation']}")
    print(f"     Peak interactive queue:   {summary['peak_interactive_queue_depth']}")
    print(f"     Peak background queue:    {summary['peak_background_queue_depth']}")
    print(f"     Retries: {summary['retry_count']}")
    print(f"     Errors:  {summary['error_count']}")
    print()

    print(f"  Total duration: {summary['duration_seconds']}s")
    print(f"{'=' * 70}\n")


def main():
    parser = argparse.ArgumentParser(description="NITRClaw load test")
    parser.add_argument("--users", type=int, default=100, help="Number of simulated users")
    parser.add_argument("--duration", type=int, default=300, help="Test duration in seconds")
    parser.add_argument("--output", type=str, default=None, help="Write JSON summary to file")
    args = parser.parse_args()

    summary = asyncio.run(run_load_test(args.users, args.duration))
    print_summary(summary, args.users, args.duration)

    if args.output:
        with open(args.output, "w") as f:
            json.dump(summary, f, indent=2, default=str)
        print(f"  Summary written to: {args.output}\n")

    failing = [k for k, v in summary["success_rates"].items() if v < 80]
    if failing:
        print(f"  ❌ FAIL: success rates below 80% for: {', '.join(failing)}")
        sys.exit(1)
    else:
        print(f"  ✅ PASS: all actions above 80% success rate")
        sys.exit(0)


if __name__ == "__main__":
    main()
