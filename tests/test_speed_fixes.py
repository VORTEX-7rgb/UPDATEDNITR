"""Speed-round proofs: postback skips, dispatcher/scheduler drain, cached
signatures, set-based duplicate-check, and new hot-path indexes."""
from __future__ import annotations

import asyncio
import inspect
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import app.nitris.client as client_module
import app.nitris.job_queue as job_queue_module
import app.services.event_dispatcher_service as dispatcher_module
from app.db.models import Base, Event, InboxMessage
from app.db.repositories.event_repository import EventRepository
from app.nitris.client import NitrisClient
from app.nitris.constants import (
    ATTENDANCE_TABLE_ID,
    CTL_ACADEMIC_YEAR,
    CTL_SEMESTER,
    CTL_SESSION,
    STUDENT_INFO_LABEL_ID,
)
from app.nitris.exceptions import AttendanceParseError
from app.nitris.job_queue import NitrisJobQueue, Priority
from app.services.event_dispatcher_service import EventDispatcherService

REPO_ROOT = Path(__file__).resolve().parents[1]


def _src(rel: str) -> str:
    return (REPO_ROOT / rel).read_text(encoding="utf-8")


# ── 1. Skip-if-selected postbacks (the ~2s win) ──────────────────────────────

def _attendance_html() -> str:
    return f"""
    <html><body>
      <span id="{STUDENT_INFO_LABEL_ID}">Test Student (1024)</span>
      <select name="{CTL_SESSION}"></select>
      <table id="{ATTENDANCE_TABLE_ID}">
        <tr><th>Code</th><th>Subject</th><th>Faculty</th><th>TC</th><th>UA</th>
            <th>LE</th><th>OA</th><th>L-T-P</th></tr>
        <tr><td>CS101</td><td>Intro CS</td><td>Prof X</td><td>10</td><td>2</td>
            <td>1</td><td>3</td><td>3-1-0</td></tr>
      </table>
    </body></html>
    """


async def test_warm_scrape_makes_zero_postbacks(monkeypatch):
    """Server already has hint year+session selected → steps 3/4 must NOT POST."""
    html = _attendance_html()

    async def fake_resolve(self, module_name, keyword):
        return "http://portal/ClassAttendance.aspx"

    def fake_form_fields(page_html, exclude_placeholders=True):
        return {
            CTL_SEMESTER: "semA",
            CTL_ACADEMIC_YEAR: "Y26",
            CTL_SESSION: "sessX",
        }

    def fake_dropdowns(page_html, select_name):
        # SYNC on purpose: production invokes this via asyncio.to_thread,
        # which requires a plain callable, not a coroutine function.
        return {
            CTL_SEMESTER: [("semA", "Autumn"), ("semB", "Spring")],
            CTL_ACADEMIC_YEAR: [("Y25", "2025 - 26"), ("Y26", "2026 - 27")],
            CTL_SESSION: [("sessY", "Spring 2025"), ("sessX", "Autumn 2026")],
        }[select_name]

    postback = AsyncMock(side_effect=AssertionError("redundant postback fired!"))

    monkeypatch.setattr(NitrisClient, "_resolve_module_subpage_url", fake_resolve)
    monkeypatch.setattr(client_module, "extract_form_fields", fake_form_fields)
    monkeypatch.setattr(client_module, "extract_dropdown_options", fake_dropdowns)
    monkeypatch.setattr(client_module, "submit_postback", postback)

    c = NitrisClient()
    c._debug = False
    c.client = SimpleNamespace(
        get=AsyncMock(return_value=SimpleNamespace(status_code=200, text=html))
    )

    holder: dict = {}
    final_html = await c.fetch_attendance(prefer_key=None, parsed_out=holder)

    assert final_html == html
    postback.assert_not_awaited()
    # Single-parse contract still holds through the skip path.
    assert len(holder["result"].records) == 1
    assert holder["result"].records[0].subject_code == "CS101"


def test_skip_if_selected_guards_present_in_source():
    src = _src("app/nitris/client.py")
    assert "form_state.get(CTL_ACADEMIC_YEAR) == year_value" in src
    assert "form_state.get(CTL_SESSION) == session_value" in src


# ── 2. Dispatcher drain-mode ────────────────────────────────────────────────


async def test_dispatcher_loops_immediately_while_backlog_exists(monkeypatch):
    events = [
        {"id": 1, "user_id": 7, "event_type": "new_message_received",
         "payload_json": {"message_id": 11}, "attempt_count": 0},
        {"id": 2, "user_id": 7, "event_type": "new_message_received",
         "payload_json": {"message_id": 12}, "attempt_count": 0},
    ]
    sent_marks = []

    async def fake_claim(factory, worker_id):
        return list(events) if len(sent_marks) == 0 else []

    svc = EventDispatcherService(bot=None, session_factory=None, worker_id="t")
    monkeypatch.setattr(dispatcher_module, "claim_events", fake_claim)
    monkeypatch.setattr(
        dispatcher_module,
        "get_telegram_ids_for_users",
        AsyncMock(return_value={7: 424242}),
    )
    monkeypatch.setattr(
        dispatcher_module,
        "_format_notification",
        lambda t, p: ("hi", None),
    )
    monkeypatch.setattr(svc, "_send_with_retry", AsyncMock(return_value=(True, None)))
    monkeypatch.setattr(
        dispatcher_module,
        "mark_event_sent",
        AsyncMock(side_effect=lambda factory, eid: sent_marks.append(eid)),
    )

    recorded_delays: list[float] = []
    real_sleep = asyncio.sleep

    async def spying_sleep(delay, *a, **k):
        recorded_delays.append(delay)
        return await real_sleep(min(float(delay), 0.005), *a, **k)

    monkeypatch.setattr(dispatcher_module.asyncio, "sleep", spying_sleep)
    monkeypatch.setattr(svc, "start_reaper", lambda: None)

    task = asyncio.create_task(svc.run_forever())
    snapshot_at_drain: list[float] = []
    for _ in range(200):
        if len(sent_marks) >= 2:
            snapshot_at_drain = list(recorded_delays)
            break
        await real_sleep(0.005)
    svc._stop = True
    try:
        await asyncio.wait_for(task, timeout=2)
    except asyncio.TimeoutError:
        task.cancel()

    assert sorted(sent_marks) == [1, 2]
    # While draining the backlog, the long poll sleep must never fire.
    assert dispatcher_module.DISPATCH_INTERVAL_SECONDS not in snapshot_at_drain


# ── 3. Scheduler drain-mode guardrail ────────────────────────────────────────


def test_scheduler_reloops_fast_on_full_batch():
    src = _src("app/services/scheduler_service.py")
    assert "len(claimed) >= config.SCHEDULER_BATCH_SIZE" in src
    # claimed must be pre-initialized so early-failure paths cannot NameError.
    assert re.search(r"while True:\s*\n\s*claimed: list = \[\]", src)


# ── 4. Cached handler signatures ────────────────────────────────────────────


async def test_signature_computed_once_per_handler(monkeypatch):
    real_signature = job_queue_module.inspect.signature
    counter = {"n": 0}

    def counting_signature(fn):
        counter["n"] += 1
        return real_signature(fn)

    monkeypatch.setattr(job_queue_module.inspect, "signature", counting_signature)

    q = NitrisJobQueue(num_workers=0)

    async def boom(payload, bot):
        raise RuntimeError("mid-scrape")

    q.register_handler("sig_test_a", boom)
    q.register_handler("sig_test_b", boom)
    assert counter["n"] == 2  # once per registration

    class _StubQueue:
        def task_done(self):
            pass

    fut = await q.enqueue("sig_test_a", user_id=1, priority=Priority.HIGH)
    job = await q._interactive_queue.get()
    await q._run_job(job, _StubQueue(), 0, "interactive")

    assert counter["n"] == 2  # execution did NOT re-inspect
    # Transient failure → future intentionally stays pending (retry scheduled).
    assert not fut.done()


# ── 5. Set-based has_message_event ───────────────────────────────────────────


class _ExecResult:
    def __init__(self, value):
        self._v = value

    def scalar_one_or_none(self):
        return self._v


class _CaptureSession:
    def __init__(self, value):
        self.captured = None
        self._value = value

    async def execute(self, stmt):
        self.captured = stmt
        return _ExecResult(self._value)


async def test_has_message_event_single_indexed_lookup():
    repo = EventRepository(_CaptureSession(None))
    assert await repo.has_message_event(user_id=9, event_type="new_message_received", message_id=55) is False

    sql = str(repo.session.captured).lower()
    assert "limit" in sql
    # The JSONB accessor binds 'message_id' and drives the expression index.
    params = repo.session.captured.compile().params
    assert "message_id" in str(params)

    hit_repo = EventRepository(_CaptureSession(42))
    assert await hit_repo.has_message_event(user_id=9, event_type="new_message_received", message_id=55) is True


# ── 6. New hot-path indexes are declared ─────────────────────────────────────


def test_speed_indexes_declared_in_metadata():
    names = set()
    for table in Base.metadata.tables.values():
        for idx in table.indexes:
            names.add(idx.name)
    assert "idx_inbox_user_sent_on" in names
    assert "idx_inbox_user_unread" in names
    assert "idx_events_user_type_msgid" in names
