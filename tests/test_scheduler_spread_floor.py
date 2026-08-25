"""ensure_schedule_exists spread floor — registration-race regression guard.

Incident 2026-08-25 (VM logs 16:51 UTC): new schedules were spread with
uniform(0, TTL), so a brand-new user's inbox sync could come due SECONDS
after registration. The scheduler's non-baseline sync then raced the silent
onboarding prefetch and burst "new message" notifications for the user's
whole historical backlog.

Guards:
  1. New schedules never start before SCHEDULER_INITIAL_SPREAD_FLOOR_SECONDS.
  2. Tiny TTLs clamp the floor to half the window so spread stays sane.
"""
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("ENCRYPTION_KEY", "ZmDfcTF7_60GrrY167zsiPd67pEvs0aGOv2oasOM1Pg=")
os.environ.setdefault("BOT_TOKEN", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost:5432/test")

from contextlib import asynccontextmanager

import app.services.scheduler_service as sched
from app.config import config


class _AsyncCM:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *exc):
        return False


def _make_session_factory():
    """Zero-arg factory returning a fresh fake session CM per call — mirrors
    async_sessionmaker semantics (ensure_schedule_exists invokes it inline)."""
    session = MagicMock()
    session.begin.return_value = _AsyncCM()
    session.execute = AsyncMock()

    @asynccontextmanager
    async def _ctx():
        yield session

    def factory():
        return _ctx()

    return factory


@pytest.mark.asyncio
async def test_new_schedule_spread_starts_at_floor(monkeypatch):
    """Default inbox TTL (4h): spread low bound must be the configured floor,
    never ~zero — the onboarding prefetch needs time to finish first."""
    captured = {}

    def fake_uniform(low, high):
        captured["low"], captured["high"] = low, high
        return low

    monkeypatch.setattr(sched.random, "uniform", fake_uniform)

    await sched.ensure_schedule_exists(_make_session_factory(), user_id=9, module_name="inbox")

    floor = config.SCHEDULER_INITIAL_SPREAD_FLOOR_SECONDS
    ttl = float(config.MODULE_TTL_SECONDS["inbox"])
    assert captured["low"] == pytest.approx(min(floor, ttl / 2))
    assert captured["high"] >= captured["low"]


@pytest.mark.asyncio
async def test_tiny_ttl_clamps_the_floor(monkeypatch):
    """A TTL smaller than the floor must not produce an inverted/oversized
    spread window — floor clamps to half the TTL."""
    captured = {}

    def fake_uniform(low, high):
        captured["low"], captured["high"] = low, high
        return low

    monkeypatch.setattr(sched.random, "uniform", fake_uniform)
    monkeypatch.setattr(
        config, "MODULE_TTL_SECONDS", {**config.MODULE_TTL_SECONDS, "inbox": 60}
    )

    await sched.ensure_schedule_exists(_make_session_factory(), user_id=9, module_name="inbox")

    assert captured["low"] == pytest.approx(30.0)  # 60s TTL → floor clamps to 30s
    assert captured["high"] >= captured["low"]
