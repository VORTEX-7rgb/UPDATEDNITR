"""Tests proving the rate limiter cannot leak memory (pruned cooldowns)."""
from __future__ import annotations

import time

import pytest

from app.nitris.rate_limiter import OperationCooldown, check_and_set_cooldown, clear_cooldown


@pytest.fixture
def limiter(monkeypatch):
    monkeypatch.setattr("app.nitris.rate_limiter._PRUNE_EVERY_WRITES", 8)
    return OperationCooldown()


async def test_expired_entries_pruned_after_write_cadence(limiter):
    for i in range(100):  # far more than the prune cadence (8)
        await limiter.check(1, f"op_{i}", cooldown_seconds=60)
    # All entries are still active → none prunable yet.
    assert len(limiter._cooldowns) == 100

    # Age every entry past expiry, then cross ONE full prune cadence (8
    # writes) — the automatic sweep must fire mid-loop and drop them all.
    expired_past = time.monotonic() - 1
    for k in list(limiter._cooldowns):
        limiter._cooldowns[k] = expired_past

    for i in range(8):
        await limiter.check(2, f"trigger_{i}", cooldown_seconds=60)

    keys = set(limiter._cooldowns.keys())
    assert len(keys) == 8
    assert all(k.startswith("2:") for k in keys)


async def test_active_entries_survive_pruning(limiter):
    await limiter.check(7, "attendance_refresh", cooldown_seconds=300)
    stale_past = time.monotonic() - 1
    for i in range(20):
        limiter._cooldowns[f"stale:{i}"] = stale_past

    removed = await limiter.prune_expired()

    assert removed == 20
    assert list(limiter._cooldowns.keys()) == ["7:attendance_refresh"]


async def test_get_stats_prunes_and_counts_active_only(limiter):
    now = time.monotonic()
    limiter._cooldowns["a:live"] = now + 30
    limiter._cooldowns["b:dead"] = now - 5
    stats = limiter.get_stats()

    assert stats["active_cooldowns"] == 1
    assert "b:dead" not in limiter._cooldowns


async def test_check_still_enforces_and_sets(limiter):
    allowed, remaining = await limiter.check(42, "inbox_refresh", cooldown_seconds=60)
    assert allowed is True and remaining == 0

    allowed, remaining = await limiter.check(42, "inbox_refresh", cooldown_seconds=60)
    assert allowed is False and remaining > 0

    await limiter.clear(42, "inbox_refresh")
    allowed, _ = await limiter.check(42, "inbox_refresh", cooldown_seconds=60)
    assert allowed is True


def test_legacy_sync_helpers_remain_pruned():
    clear_cooldown(99, "attendance")
    allowed, _ = check_and_set_cooldown(99, "attendance", cooldown_seconds=1)
    assert allowed is True
    allowed, remaining = check_and_set_cooldown(99, "attendance", cooldown_seconds=1)
    assert allowed is False and remaining > 0
    clear_cooldown(99, "attendance")
    allowed, _ = check_and_set_cooldown(99, "attendance", cooldown_seconds=1)
    assert allowed is True
