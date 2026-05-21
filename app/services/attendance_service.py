"""Attendance service — orchestrates login, workflow, and parsing with retry."""

import logging
import asyncio

import httpx

from app.nitris.client import NitrisClient
from app.nitris.parser import parse_attendance_html, AttendanceResult
from app.nitris.exceptions import LoginError, AttendanceParseError, AttendanceWorkflowError, SessionExpiredError

logger = logging.getLogger(__name__)

MAX_RETRIES = 2
RETRY_DELAY = 2.0


async def get_attendance_data(username: str, password: str) -> AttendanceResult:
    """Login → fetch attendance via postback workflow → parse.

    Retries ONLY on transient network/workflow failures.
    Auth and parse errors are raised immediately.
    ONE client instance — session persists across retries.
    """
    client = NitrisClient()
    try:
        # Login once
        await client.login(username, password)

        # Fetch with retry on transient failures
        last_error: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                html = await client.fetch_attendance()
                return parse_attendance_html(html)
            except (LoginError, AttendanceParseError, SessionExpiredError):
                raise
            except (AttendanceWorkflowError, httpx.TransportError) as e:
                last_error = e
                logger.warning("Attempt %d/%d failed: %s", attempt, MAX_RETRIES, e)
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(RETRY_DELAY)

        raise last_error or AttendanceWorkflowError("All attempts failed.")

    finally:
        await client.close()
