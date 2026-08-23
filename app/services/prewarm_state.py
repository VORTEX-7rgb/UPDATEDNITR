"""Shared runtime state for the /admin_prewarm paper cache pre-warmer.

Lives in its own tiny module so both the admin command handlers and the
background job handler can import it without circular-import risk.

Safety model:
  - Semaphore(2) hard-caps concurrent portal acquisitions so pre-warming can
    never starve interactive students (gateway slots remain the outer bound).
  - stop_event is checked before EVERY item; in-flight item finishes, nothing
    new starts.
  - Counters are plain ints guarded by the event loop's single-threadedness
    (no locking needed for += from coroutines).
"""
from __future__ import annotations

import asyncio
import time
from typing import Optional


class PrewarmState:
    def __init__(self) -> None:
        self.stop_event = asyncio.Event()
        self.semaphore = asyncio.Semaphore(2)
        self.running: bool = False
        self.started_at: Optional[float] = None
        self.academic_year: str = ""
        self.counters: dict[str, int] = {
            "subjects_done": 0,
            "available": 0,
            "not_available": 0,
            "failed": 0,
            "skipped": 0,
        }

    def start_run(self, academic_year: str) -> None:
        self.stop_event.clear()
        self.running = True
        self.started_at = time.monotonic()
        self.academic_year = academic_year
        self.counters = {
            "subjects_done": 0,
            "available": 0,
            "not_available": 0,
            "failed": 0,
            "skipped": 0,
        }

    def stop(self) -> None:
        self.stop_event.set()

    @property
    def stopped(self) -> bool:
        return self.stop_event.is_set()

    def snapshot_text(self) -> str:
        elapsed = ""
        if self.started_at:
            secs = int(time.monotonic() - self.started_at)
            elapsed = f"\n⏱ Elapsed: <b>{secs // 60}m {secs % 60}s</b>"
        c = self.counters
        status = "🏃 RUNNING" if self.running else ("🛑 STOPPED" if self.stopped else "💤 idle")
        return (
            f"🔥 <b>Pre-warm status</b> — {status}\n"
            f"📅 Year: <b>{self.academic_year or '—'}</b>"
            f"{elapsed}\n"
            f"✅ Available: <b>{c['available']}</b>\n"
            f"ℹ️ Not available (permanent negative): <b>{c['not_available']}</b>\n"
            f"❌ Failed: <b>{c['failed']}</b>\n"
            f"⏭ Skipped (cached/busy): <b>{c['skipped']}</b>\n"
            f"📚 Subjects finished: <b>{c['subjects_done']}</b>"
        )


prewarm_state = PrewarmState()
