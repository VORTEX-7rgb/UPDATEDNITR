"""Tests for the retention sweeper (snapshots + events bounded purge)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import app.services.retention_service as retention_module
from app.db.repositories.event_repository import EventRepository
from app.db.repositories.snapshot_repository import SnapshotRepository
from app.services.retention_service import RetentionService


class _FakeResult:
    def __init__(self, rowcount: int):
        self.rowcount = rowcount


class FakeSession:
    """Records executed statements/params and replays scripted rowcounts."""

    def __init__(self, script: list[int], log: list):
        self._script = script
        self.log = log

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def begin(self):
        return self

    async def execute(self, stmt, params=None):
        rowcount = self._script.pop(0) if self._script else 0
        try:
            compiled_params = dict(stmt.compile().params)
        except Exception:
            compiled_params = {}
        self.log.append({"sql": str(stmt), "params": compiled_params, "rowcount": rowcount})
        return _FakeResult(rowcount)


def make_factory(script: list[int], log: list):
    def factory():
        return FakeSession(script, log)
    return factory


@pytest.fixture
def fast_config(monkeypatch):
    monkeypatch.setattr(retention_module.config, "RETENTION_SNAPSHOT_KEEP", 10)
    monkeypatch.setattr(retention_module.config, "RETENTION_DELETE_BATCH", 5)
    monkeypatch.setattr(retention_module.config, "RETENTION_BATCH_PAUSE_SECONDS", 0.0)
    monkeypatch.setattr(retention_module.config, "RETENTION_EVENT_DAYS", 14)
    return retention_module.config


async def test_snapshot_ranking_sql_keeps_newest_per_key(fast_config):
    log: list = []
    repo = SnapshotRepository(FakeSession([0], log))

    deleted = await repo.purge_superseded_batch(keep_per_key=10, limit=100)

    assert deleted == 0
    sql = log[0]["sql"].lower()
    # Ranking must partition per (user, module), order newest-first, and only
    # victimize rows past the keep window — never rank <= N.
    assert "row_number() over" in sql
    assert "partition by" in sql
    assert "snapshots.user_id" in sql and "snapshots.module_name" in sql
    assert "desc" in sql
    assert "limit" in sql


async def test_snapshot_purge_respects_keep_parameter(fast_config):
    log: list = []
    repo = SnapshotRepository(FakeSession([0], log))
    await repo.purge_superseded_batch(keep_per_key=7, limit=100)
    assert 7 in log[0]["params"].values()


async def test_snapshot_purge_guards_invalid_inputs():
    log: list = []
    repo = SnapshotRepository(FakeSession([], log))
    assert await repo.purge_superseded_batch(keep_per_key=0, limit=100) == 0
    assert await repo.purge_superseded_batch(keep_per_key=10, limit=0) == 0
    assert log == []  # no SQL executed at all


async def test_event_terminal_only_filter(fast_config):
    log: list = []
    repo = EventRepository(FakeSession([0], log))
    cutoff = datetime.now(timezone.utc)
    await repo.purge_terminal_batch(older_than=cutoff, limit=50)

    sql = log[0]["sql"].lower()
    # Only terminal rows qualify: sent OR permanent_failure, both age-filtered.
    assert "sent" in sql and "permanent_failure" in sql
    assert "created_at" in sql
    assert any(isinstance(v, datetime) for v in log[0]["params"].values())
    assert 50 in log[0]["params"].values()


async def test_snapshots_drain_multiple_batches_with_pauses(fast_config, monkeypatch):
    pauses: list[float] = []

    async def fake_sleep(seconds):
        pauses.append(seconds)

    monkeypatch.setattr(retention_module.asyncio, "sleep", fake_sleep)

    log: list = []
    service = RetentionService(make_factory([5, 5, 2], log))  # batch=5 → full, full, partial
    total = await service.purge_superseded_snapshots()

    assert total == 12
    assert len(log) == 3
    assert pauses == [0.0, 0.0]  # paused between full batches only


async def test_events_single_partial_batch_no_pause(fast_config, monkeypatch):
    pauses: list[float] = []

    async def fake_sleep(seconds):
        pauses.append(seconds)

    monkeypatch.setattr(retention_module.asyncio, "sleep", fake_sleep)

    log: list = []
    service = RetentionService(make_factory([3], log))
    total = await service.purge_terminal_events()

    assert total == 3
    assert len(log) == 1
    assert pauses == []


async def test_run_once_swallows_one_side_failure(fast_config, monkeypatch):
    calls: list[str] = []

    async def fail():
        calls.append("snap")
        raise RuntimeError("db down")

    async def ok():
        calls.append("events")
        return 4

    service = RetentionService(None)
    monkeypatch.setattr(service, "purge_superseded_snapshots", fail)
    monkeypatch.setattr(service, "purge_terminal_events", ok)

    results = await service.run_once()

    assert results == {"snapshots": 0, "events": 4}
    assert calls == ["snap", "events"]


async def test_start_stop_lifecycle(fast_config):
    started: list[bool] = []

    class _Task:
        def done(self):
            return False

        def cancel(self):
            pass

    async def fake_periodic():
        started.append(True)

    service = RetentionService(make_factory([], []))
    service.run_periodic = fake_periodic  # type: ignore[method-assign]
    service.start()
    # Second start while running must be a no-op (idempotent).
    service.start()
    await service.stop()
