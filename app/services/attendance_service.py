"""Attendance service — orchestrates login, workflow, and parsing with retry.

Phase 7.1 — Session-expiry recovery (RELOGIN FIX):
  Previously, on SessionExpiredError, this module called `client.login()`
  DIRECTLY — bypassing the gateway's quarantine gate, login pacing, and
  metrics. That was a critical security invariant violation. Now it routes
  re-login through `nitris_gateway._do_login()` which enforces pacing +
  metrics but skips the quarantine check (the user is already authenticated,
  just session-expired).
"""

import logging
import asyncio
from typing import Optional

import httpx

from app.nitris.client import NitrisClient
from app.nitris.parser import parse_attendance_html, AttendanceResult
from app.nitris.exceptions import (
    LoginError,
    AttendanceParseError,
    AttendanceWorkflowError,
    SessionExpiredError,
    AttendanceTableMissingError,
)

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
RETRY_DELAY = 1.0
MAX_RELOGINS_PER_SYNC = 1  # prevent infinite loops on actually-invalid credentials


async def get_attendance_data(
    username: str,
    password: str,
    client: Optional[NitrisClient] = None,
    *,
    user_id: Optional[int] = None,
) -> AttendanceResult:
    """Login → fetch attendance via postback workflow → parse.

    Phase 7.1 fix:
      The re-login path on SessionExpiredError now goes through the gateway
      (via `_do_login`) so login pacing + metrics are still enforced. The
      quarantine guard is intentionally bypassed for re-login (the user is
      already authenticated; their session just dropped).

    Retry policy:
      - SessionExpiredError mid-workflow → re-login via gateway._do_login
        (pacing + metrics enforced, quarantine guard skipped) and retry.
        Max 1 re-login per sync call.
      - AttendanceWorkflowError / httpx.TransportError → retry with exponential
        backoff, no re-login (session likely still valid, just transient error).
      - LoginError / AttendanceParseError → raise immediately (auth/parse problems
        are not transient; don't waste retries).
    """
    if client is None:
        # Direct-login path removed: callers MUST pass a pre-authenticated client
        # (logged in through the gateway) so every login stays behind the
        # credential-quarantine gate.
        raise RuntimeError(
            "get_attendance_data requires a pre-authenticated client (logged in "
            "through the gateway)."
        )

    relogins_used = 0
    last_error: Exception | None = None
    backoff = RETRY_DELAY

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            # PERF: prefer_key enables the year/session hint cache — repeat
            # scrapes for the same student skip the dropdown probe postbacks.
            html = await client.fetch_attendance(prefer_key=username)
            return parse_attendance_html(html)
        except SessionExpiredError:
            # Session dropped mid-workflow — re-login and retry the WHOLE workflow.
            if relogins_used >= MAX_RELOGINS_PER_SYNC:
                logger.error(
                    "Session expired again after re-login attempt #%d. Giving up.",
                    relogins_used,
                )
                raise
            relogins_used += 1
            logger.warning(
                "Session expired on attempt %d/%d — re-logging in via gateway and retrying (re-login #%d).",
                attempt,
                MAX_RETRIES,
                relogins_used,
            )
            # Phase 7.1 fix: route through the gateway so login pacing + metrics
            # are still enforced. _do_login does NOT check the quarantine guard
            # (the user is already authenticated; their session just expired).
            try:
                from app.nitris.gateway import nitris_gateway
                await nitris_gateway._do_login(client, username, password)
            except LoginError as le:
                # Re-login failed — credentials may have changed. Propagate
                # so the caller can mark the user quarantined.
                logger.error("Re-login via gateway failed: %s", le)
                if user_id is not None:
                    from app.nitris.auth_gate import on_login_failure
                    await on_login_failure(user_id, str(le))
                raise
            # Don't count this against MAX_RETRIES — give the retry a clean slate
            continue
        except (LoginError, AttendanceParseError, AttendanceTableMissingError):
            # Auth or parse errors are not transient — propagate immediately.
            raise
        except (AttendanceWorkflowError, httpx.TransportError) as e:
            last_error = e
            logger.warning(
                "Attempt %d/%d failed: %s", attempt, MAX_RETRIES, e
            )
            if attempt < MAX_RETRIES:
                await asyncio.sleep(backoff)
                backoff *= 2.0

    raise last_error or AttendanceWorkflowError("All attempts failed.")
