"""Admin stats — user activity metrics for the /status command.

Surfaces the data the bot owner needs to monitor growth + health:
  - Signup trend (new today / 7d / 30d)
  - DAU / WAU / MAU (active users in last 1/7/30 days)
  - Inactive users (total - MAU)
  - Users with invalid credentials or sync failures
  - Per-module sync health (how many users synced each module recently)

ZERO schema change — uses existing tables:
  - users (created_at, credentials_valid)
  - module_sync_schedule (last_synced_at, consecutive_failures, module_name)
  - sync_states (last_success — alternate signal, kept for completeness)

"Active user" definition
-------------------------
A user is "active in the last N days" if they have ANY module_sync_schedule
row with last_synced_at > NOW() - INTERVAL 'N days'. This captures BOTH:
  - Users auto-synced by the background scheduler (Phase 5 TTL loop)
  - Users who manually pressed buttons (/attendance, /timetable, Refresh…)
Both code paths update last_synced_at, so the signal is reliable.
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import AsyncSession

from app.utils import esc

logger = logging.getLogger(__name__)


# ── SQL queries ──────────────────────────────────────────────────────────────
# All queries use PostgreSQL INTERVAL syntax — matches the existing cmd_status
# pattern. The bot's DB is Postgres (per .env.example DATABASE_URL).

Q_NEW_USERS_24H = "SELECT COUNT(*) FROM users WHERE created_at > NOW() - INTERVAL '1 day'"
Q_NEW_USERS_7D = "SELECT COUNT(*) FROM users WHERE created_at > NOW() - INTERVAL '7 days'"
Q_NEW_USERS_30D = "SELECT COUNT(*) FROM users WHERE created_at > NOW() - INTERVAL '30 days'"

Q_DAU = (
    "SELECT COUNT(DISTINCT user_id) FROM module_sync_schedule "
    "WHERE last_synced_at > NOW() - INTERVAL '1 day'"
)
Q_WAU = (
    "SELECT COUNT(DISTINCT user_id) FROM module_sync_schedule "
    "WHERE last_synced_at > NOW() - INTERVAL '7 days'"
)
Q_MAU = (
    "SELECT COUNT(DISTINCT user_id) FROM module_sync_schedule "
    "WHERE last_synced_at > NOW() - INTERVAL '30 days'"
)

Q_INVALID_CREDS = "SELECT COUNT(*) FROM users WHERE credentials_valid = FALSE"
Q_SYNC_FAILURES = (
    "SELECT COUNT(DISTINCT user_id) FROM module_sync_schedule "
    "WHERE consecutive_failures > 0"
)

Q_PER_MODULE_24H = (
    "SELECT module_name, COUNT(DISTINCT user_id) AS user_count "
    "FROM module_sync_schedule "
    "WHERE last_synced_at > NOW() - INTERVAL '1 day' "
    "GROUP BY module_name ORDER BY module_name"
)


async def fetch_user_activity_stats(session: AsyncSession) -> dict[str, Any]:
    """Run all user-activity queries in one session. Returns a dict.

    Every query is wrapped in its own try/except so a single failure
    (e.g. a column not existing in an old DB) doesn't break the whole
    status command. Failed queries return the string "ERROR" for that
    field — the formatter renders it as a red flag.
    """
    stats: dict[str, Any] = {}

    async def _safe_scalar(key: str, sql: str) -> None:
        try:
            result = await session.execute(sql_text(sql))
            stats[key] = result.scalar()
        except Exception as e:
            logger.warning("admin stats: query %r failed: %r", key, e)
            stats[key] = "ERROR"

    async def _safe_rows(key: str, sql: str) -> None:
        try:
            result = await session.execute(sql_text(sql))
            stats[key] = [
                {"module_name": row[0], "user_count": row[1]}
                for row in result.fetchall()
            ]
        except Exception as e:
            logger.warning("admin stats: query %r failed: %r", key, e)
            stats[key] = []

    # Signup trend
    await _safe_scalar("new_users_24h", Q_NEW_USERS_24H)
    await _safe_scalar("new_users_7d", Q_NEW_USERS_7D)
    await _safe_scalar("new_users_30d", Q_NEW_USERS_30D)

    # Active users
    await _safe_scalar("dau", Q_DAU)
    await _safe_scalar("wau", Q_WAU)
    await _safe_scalar("mau", Q_MAU)

    # Credential health
    await _safe_scalar("invalid_credentials", Q_INVALID_CREDS)
    await _safe_scalar("sync_failures", Q_SYNC_FAILURES)

    # Per-module sync health (last 24h)
    await _safe_rows("per_module_24h", Q_PER_MODULE_24H)

    return stats


def format_user_activity_section(stats: dict[str, Any], total_users: Any, valid_creds: Any) -> str:
    """Format the user-activity section as HTML for the /status response.

    Args:
        stats: Output of fetch_user_activity_stats()
        total_users: Total user count (from the existing /status query)
        valid_creds: Count of users with valid credentials (existing query)

    Returns:
        HTML string for the new section. Empty/ERROR stats render as "?"
        so the admin sees something is wrong rather than a misleading 0.
    """
    def _fmt(val: Any) -> str:
        """Render a stat value — '?' for None or 'ERROR'."""
        if val is None or val == "ERROR":
            return "?"
        return str(val)

    total_s = _fmt(total_users)
    valid_s = _fmt(valid_creds)
    invalid_s = _fmt(stats.get("invalid_credentials"))
    new_24h = _fmt(stats.get("new_users_24h"))
    new_7d = _fmt(stats.get("new_users_7d"))
    new_30d = _fmt(stats.get("new_users_30d"))
    dau = _fmt(stats.get("dau"))
    wau = _fmt(stats.get("wau"))
    mau = _fmt(stats.get("mau"))
    sync_fail = _fmt(stats.get("sync_failures"))

    # Compute "inactive" = total - MAU. Both must be numeric.
    inactive_s = "?"
    try:
        if (isinstance(total_users, (int, float)) and
            isinstance(stats.get("mau"), (int, float))):
            inactive_s = str(max(0, int(total_users) - int(stats["mau"])))
    except Exception:
        pass

    # Per-module breakdown
    per_module_rows = stats.get("per_module_24h", [])
    per_module_lines = []
    if per_module_rows:
        for row in per_module_rows:
            module_name = esc(str(row.get("module_name", "?")))
            user_count = row.get("user_count", "?")
            per_module_lines.append(
                f"  {module_name}: {_fmt(user_count)} users synced"
            )
    else:
        per_module_lines.append("  (no syncs in last 24h)")

    per_module_block = "\n".join(per_module_lines)

    return (
        f"📈 <b>User Activity</b>\n"
        f"  Registered: {esc(total_s)} (valid: {esc(valid_s)}, invalid: {esc(invalid_s)})\n"
        f"  New today / 7d / 30d: {esc(new_24h)} / {esc(new_7d)} / {esc(new_30d)}\n"
        f"  Active 24h (DAU): {esc(dau)}\n"
        f"  Active 7d (WAU): {esc(wau)}\n"
        f"  Active 30d (MAU): {esc(mau)}\n"
        f"  Inactive (&gt;30d): {esc(inactive_s)}\n"
        f"  Users with sync failures: {esc(sync_fail)}\n\n"
        f"🔄 <b>Per-Module Sync Health</b> (last 24h)\n"
        f"{per_module_block}\n"
    )
