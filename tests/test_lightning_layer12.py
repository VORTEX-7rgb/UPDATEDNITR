"""Lightning L1+L2 proofs: session warm seeding + fire-and-forget attendance."""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("ENCRYPTION_KEY", "ZmDfcTF7_60GrrY167zsiPd67pEvs0aGOv2oasOM1Pg=")
os.environ.setdefault("BOT_TOKEN", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost:5432/test")

import app.services.session_warmer as warmer
from app.nitris.job_handlers import handle_session_warm


@pytest.fixture(autouse=True)
def _reset_warmer(monkeypatch):
    warmer._throttle.clear()
    yield
    warmer._throttle.clear()


def _pool(warm: bool):
    mod = SimpleNamespace(is_session_warm=lambda uid: warm)
    return mod


# ── Layer 1: seeding ─────────────────────────────────────────────────────────


async def test_cold_user_gets_low_priority_warm_job(monkeypatch):
    monkeypatch.setattr(warmer, "is_session_warm", lambda uid: False)
    enqueued = []

    class FakeQueue:
        async def enqueue(self, **kw):
            enqueued.append(kw)

    monkeypatch.setattr(
        "app.nitris.job_queue.nitris_job_queue", FakeQueue(), raising=False)

    res = await warmer.request_session_warm(user_id=7)

    assert res == "queued"
    kw = enqueued[0]
    assert kw["job_type"] == "session_warm"
    assert kw["priority"].name == "LOW"
    assert kw["dedup_key"] == "session_warm:7"


async def test_warm_user_skips_entirely(monkeypatch):
    monkeypatch.setattr(warmer, "is_session_warm", lambda uid: True)
    called = {"n": 0}

    class FakeQueue:
        async def enqueue(self, **kw):
            called["n"] += 1

    monkeypatch.setattr(
        "app.nitris.job_queue.nitris_job_queue", FakeQueue(), raising=False)

    res = await warmer.request_session_warm(user_id=7)

    assert res == "warm"
    assert called["n"] == 0


async def test_throttle_blocks_render_storms(monkeypatch):
    monkeypatch.setattr(warmer, "is_session_warm", lambda uid: False)

    class FakeQueue:
        def __init__(self):
            self.n = 0

        async def enqueue(self, **kw):
            self.n += 1

    q = FakeQueue()
    monkeypatch.setattr("app.nitris.job_queue.nitris_job_queue", q, raising=False)

    a = await warmer.request_session_warm(9)
    b = await warmer.request_session_warm(9)   # immediate re-render

    assert (a, b) == ("queued", "throttled")
    assert q.n == 1


async def test_handle_session_warm_runs_noop_through_pool(monkeypatch):
    ran = {"work": False}

    async def fake_with_pooled_session(**kw):
        res = await kw["work"](None, None)
        ran["work"] = bool(res)
        return res

    async def fake_execute(*a, **k):
        row = MagicMock()
        row.first.return_value = ("roll1", "encpw")
        return row

    fake_db = SimpleNamespace(execute=AsyncMock(side_effect=fake_execute))
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=fake_db)
    ctx.__aexit__ = AsyncMock(return_value=False)

    with patch("app.db.database.get_db_session", return_value=ctx), \
         patch("app.nitris.session_pool.with_pooled_session",
               fake_with_pooled_session):
        result = await handle_session_warm({"user_id": 3}, None)

    assert result == {"success": True}
    assert ran["work"] is True


# ── Layer 2: fire-and-forget attendance ─────────────────────────────────────


class _FakeSurf:
    def __init__(self):
        self.edits = []
        self.finals = []
        self.poked = []

    async def edit(self, text, kb=None):
        self.edits.append(text)

    async def final(self, text, kb=None):
        self.finals.append(text)

    def poke_later(self, *a, **k):  # must NEVER be used anymore
        self.poked.append(a)


async def test_run_flow_returns_without_awaiting_future(monkeypatch):
    from app.bot.handlers import attendance as att

    hung_future = asyncio.get_event_loop().create_future()  # never resolves
    captured = {}

    class FakeQueue:
        async def enqueue(self, **kw):
            captured.update(kw)
            return hung_future

    monkeypatch.setattr(
        "app.nitris.job_queue.nitris_job_queue", FakeQueue(), raising=False)

    surf = _FakeSurf()
    # Must return promptly even though the future NEVER resolves.
    await asyncio.wait_for(
        att._run_flow(surf, user_id=1, cached=None,
                      chat_id=555, message_id=777),
        timeout=2,
    )

    assert captured["payload"]["callback_chat_id"] == 555
    assert captured["payload"]["callback_message_id"] == 777
    assert captured["priority"].name == "HIGH"
    assert len(surf.edits) == 1          # UPDATING render only
    assert surf.finals == []             # no inline final — job owns it now
    assert surf.poked == []              # poke retired


async def test_job_success_self_renders_fresh_list(monkeypatch):
    from app.nitris import job_handlers as jh
    from app.nitris.parser import AttendanceResult, AttendanceRecord

    edits = []

    async def fake_edit(chat_id, message_id, text, reply_markup=None):
        edits.append((text, reply_markup))

    monkeypatch.setattr(jh, "_edit_callback_message", fake_edit)
    monkeypatch.setattr(jh, "_bot", object())

    data = AttendanceResult(student_info="S", records=[
        AttendanceRecord(subject_code="CS101", subject_name="Intro",
                         faculty="X", tc="10", ua="1", le="0", oa="1",
                         ltp="3-0-0"),
    ])

    user = SimpleNamespace(roll_number="r1", encrypted_password="enc",
                           credentials_valid=True)
    session = MagicMock()
    session.get = AsyncMock(return_value=user)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=False)

    job = SimpleNamespace(user_id=1, payload={
        "callback_chat_id": 555, "callback_message_id": 777})

    with patch.object(jh, "async_session_factory", return_value=ctx), \
         patch.object(jh, "get_attendance_data", AsyncMock(return_value=data)), \
         patch("app.nitris.session_pool.with_pooled_session",
               AsyncMock(return_value=data)), \
         patch.object(jh, "SnapshotService") as SS, \
         patch.object(jh, "_update_sync_state", AsyncMock()):
        SS.return_value.create_snapshot_if_changed = AsyncMock()

        result = await jh.handle_attendance_refresh(job)

    assert result["success"] is True
    assert len(edits) >= 1
    text, markup = edits[-1]
    assert "CS101" in text and "Updated just now" in text
    assert markup is not None          # fresh keyboard rendered by the JOB
