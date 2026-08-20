"""Attendance service — orchestrates login, workflow, and parsing with retry."""

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
    username: str, password: str, client: Optional[NitrisClient] = None
) -> AttendanceResult:
    """Login → fetch attendance via postback workflow → parse.

    Retry policy:
      - SessionExpiredError mid-workflow → re-login the SAME client and retry the
        full fetch_attendance workflow from Step 1. Max 1 re-login per sync call.
      - AttendanceWorkflowError / httpx.TransportError → retry with exponential
        backoff, no re-login (session likely still valid, just transient error).
      - LoginError / AttendanceParseError → raise immediately (auth/parse problems
        are not transient; don't waste retries).
    """
    should_close = False
    if client is None:
        # Direct-login path removed: callers MUST pass a pre-authenticated client
        # (logged in through the gateway) so every login stays behind the
        # credential-quarantine gate.
        raise RuntimeError(
            "get_attendance_data requires a pre-authenticated client (logged in "
            "through the gateway)."
        )

    try:
        relogins_used = 0
        last_error: Exception | None = None
        backoff = RETRY_DELAY

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                html = await client.fetch_attendance()
                return parse_attendance_html(html)
            except SessionExpiredError as e:
                # Session dropped mid-workflow — re-login and retry the WHOLE workflow.
                if relogins_used >= MAX_RELOGINS_PER_SYNC:
                    logger.error(
                        "Session expired again after re-login attempt #%d. Giving up.",
                        relogins_used,
                    )
                    raise
                relogins_used += 1
                logger.warning(
                    "Session expired on attempt %d/%d — re-logging in and retrying (re-login #%d).",
                    attempt,
                    MAX_RETRIES,
                    relogins_used,
                )
                try:
                    await client.login(username, password)
                except LoginError as le:
                    logger.error("Re-login failed: %s", le)
                    raise le
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

    finally:
        if should_close:
            await client.close()
