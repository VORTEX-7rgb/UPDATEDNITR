"""Tests for admin user-activity stats system."""
import pytest
from unittest.mock import AsyncMock, MagicMock

from app.bot.handlers.admin_stats import (
    fetch_user_activity_stats,
    format_user_activity_section,
)


def test_format_user_activity_section_happy_path():
    """Verify that format_user_activity_section formats complete stats cleanly."""
    stats = {
        "new_users_24h": 5,
        "new_users_7d": 20,
        "new_users_30d": 50,
        "dau": 35,
        "wau": 80,
        "mau": 120,
        "invalid_credentials": 3,
        "sync_failures": 2,
        "per_module_24h": [
            {"module_name": "attendance", "user_count": 30},
            {"module_name": "inbox", "user_count": 25},
            {"module_name": "timetable", "user_count": 10},
        ],
    }

    result = format_user_activity_section(stats, total_users=150, valid_creds=147)

    assert "📈 <b>User Activity</b>" in result
    assert "Registered: 150 (valid: 147, invalid: 3)" in result
    assert "New today / 7d / 30d: 5 / 20 / 50" in result
    assert "Active 24h (DAU): 35" in result
    assert "Active 7d (WAU): 80" in result
    assert "Active 30d (MAU): 120" in result
    assert "Inactive (&gt;30d): 30" in result  # 150 - 120
    assert "Users with sync failures: 2" in result
    assert "🔄 <b>Per-Module Sync Health</b>" in result
    assert "attendance: 30 users synced" in result
    assert "inbox: 25 users synced" in result
    assert "timetable: 10 users synced" in result


def test_format_user_activity_section_empty_or_error():
    """Verify that partial/error stats render placeholders ('?') without crashing."""
    stats = {
        "new_users_24h": "ERROR",
        "new_users_7d": None,
        "dau": "ERROR",
        "per_module_24h": [],
    }

    result = format_user_activity_section(stats, total_users="ERROR", valid_creds="ERROR")

    assert "📈 <b>User Activity</b>" in result
    assert "Registered: ? (valid: ?, invalid: ?)" in result
    assert "(no syncs in last 24h)" in result


def test_format_user_activity_inactive_clamp():
    """Verify that inactive count clamps to 0 when MAU > total (e.g. edge condition)."""
    stats = {
        "new_users_24h": 0,
        "new_users_7d": 0,
        "new_users_30d": 0,
        "dau": 10,
        "wau": 20,
        "mau": 50,
        "invalid_credentials": 0,
        "sync_failures": 0,
        "per_module_24h": [],
    }

    result = format_user_activity_section(stats, total_users=40, valid_creds=40)
    assert "Inactive (&gt;30d): 0" in result


@pytest.mark.asyncio
async def test_fetch_user_activity_stats_mock_session():
    """Test fetch_user_activity_stats queries execution with mock DB session."""
    session = AsyncMock()

    mock_scalar_res = MagicMock()
    mock_scalar_res.scalar.return_value = 10

    mock_rows_res = MagicMock()
    mock_rows_res.fetchall.return_value = [("attendance", 10), ("inbox", 5)]

    # Mock execute returns scalar result for count queries, rows for module query
    call_count = 0

    async def fake_execute(stmt):
        nonlocal call_count
        call_count += 1
        sql_str = str(stmt)
        if "GROUP BY module_name" in sql_str:
            return mock_rows_res
        return mock_scalar_res

    session.execute = AsyncMock(side_effect=fake_execute)

    stats = await fetch_user_activity_stats(session)

    assert stats["new_users_24h"] == 10
    assert stats["dau"] == 10
    assert len(stats["per_module_24h"]) == 2
    assert stats["per_module_24h"][0]["module_name"] == "attendance"
    assert stats["per_module_24h"][0]["user_count"] == 10
