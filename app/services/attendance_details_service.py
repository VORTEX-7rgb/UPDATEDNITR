"""Attendance-details service — orchestrates the per-subject date-wise fetch.

Mirrors app/services/attendance_service.py EXACTLY in its retry semantics:

  * SessionExpiredError mid-workflow → ONE re-login through the gateway
    (`nitris_gateway._do_login` — pacing + metrics enforced, quarantine
    guard skipped since the user is already authenticated), then retry.
  * AttendanceWorkflowError / httpx.TransportError → exponential-backoff
    retries (transient portal / network trouble).
  * LoginError / AttendanceParseError → raise immediately (auth / parse
    problems are not transient — burning retries would only delay the
    user's error bubble).

The workflow itself (`NitrisClient.fetch_attendance_details`) escalates
warm → heal → force, so a cold subject costs at most a handful of GETs and
a warm one exactly two — all inside ONE pooled gateway slot, same lease
boundary discipline as every other NITRIS touchpoint.
"""
from __future__ import annotations

import logging
import asyncio
from typing import Optional

import httpx

from app.nitris.client import NitrisClient
from app.nitris.attendance_details_parser import (
    SubjectAttendanceDetails,
    parse_attendance_details_html,
)
from app.nitris.exceptions import (
    LoginError,
    AttendanceParseError,
    AttendanceWorkflowError,
    SessionExpiredError,
)

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
RETRY_DELAY = 1.0
MAX_RELOGINS_PER_SYNC = 1  # prevent infinite loops on actually-invalid credentials


async def get_attendance_details_data(
    username: str,
    password: str,
    client: NitrisClient,
    subject_code: str,
    *,
    user_id: Optional[int] = None,
) -> SubjectAttendanceDetails:
    """Fetch + parse ONE subject's date-wise attendance matrix.

    `client` MUST be a pre-authenticated NitrisClient (logged in through the
    gateway — see attendance_service for why the direct-login path is
    forbidden). Re-login on session expiry routes through the gateway too.

    Retry policy (identical to get_attendance_data):
      - SessionExpiredError → re-login via gateway._do_login (max 1) and
        retry the WHOLE workflow.
      - AttendanceWorkflowError / httpx.TransportError → exponential backoff.
      - LoginError / AttendanceParseError → propagate immediately.
    """
    if client is None:
        raise RuntimeError(
            "get_attendance_details_data requires a pre-authenticated client "
            "(logged in through the gateway)."
        )

    relogins_used = 0
    last_error: Exception | None = None
    backoff = RETRY_DELAY

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            html, _url = await client.fetch_attendance_details(subject_code)
            # BS4 over the full details page must never run on the event loop
            # while we hold a scarce gateway slot (event-loop offload).
            return await asyncio.to_thread(parse_attendance_details_html, html)
        except SessionExpiredError:
            if relogins_used >= MAX_RELOGINS_PER_SYNC:
                logger.error(
                    "Attendance details: session expired again after re-login "
                    "attempt #%d. Giving up.",
                    relogins_used,
                )
                raise
            relogins_used += 1
            logger.warning(
                "Attendance details: session expired on attempt %d/%d — "
                "re-logging in via gateway and retrying (re-login #%d).",
                attempt, MAX_RETRIES, relogins_used,
            )
            try:
                from app.nitris.gateway import nitris_gateway
                await nitris_gateway._do_login(client, username, password)
            except LoginError as le:
                logger.error("Re-login via gateway failed: %s", le)
                if user_id is not None:
                    from app.nitris.auth_gate import on_login_failure
                    await on_login_failure(user_id, str(le))
                raise
            continue  # clean slate — not counted against MAX_RETRIES
        except (LoginError, AttendanceParseError):
            raise
        except (AttendanceWorkflowError, httpx.TransportError) as e:
            last_error = e
            logger.warning(
                "Attendance details attempt %d/%d failed: %s",
                attempt, MAX_RETRIES, e,
            )
            if attempt < MAX_RETRIES:
                await asyncio.sleep(backoff)
                backoff *= 2.0

    raise last_error or AttendanceWorkflowError(
        "All attendance-details attempts failed."
    )
