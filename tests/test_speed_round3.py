"""Speed round 3 proofs: check-first wait polls, attendance fast-path, tuned env."""
from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

import app.nitris.client as client_module
import app.services.attachment_service as attachment_module
import app.services.qpaper_service as qpaper_module
from app.db.models import AttachmentStatus
from app.nitris.client import NitrisClient, _cache_key, _resolved_url_cache
from app.nitris.constants import (
    ATTENDANCE_MODULE_NAME,
    ATTENDANCE_SIDEBAR_LINK_KEYWORD,
    ATTENDANCE_TABLE_ID,
    CTL_ACADEMIC_YEAR,
    CTL_SEMESTER,
    CTL_SESSION,
    STUDENT_INFO_LABEL_ID,
)
from app.nitris.exceptions import InvalidContextError
from app.nitris.job_queue import NitrisJobQueue, Priority
from app.services.qpaper_service import QPResult, QPStatus, QPaperService

REPO_ROOT = Path(__file__).resolve().parents[1]


def _attendance_html() -> str:
    return f"""
    <html><body>
      <span id="{STUDENT_INFO_LABEL_ID}">Test Student (1024)</span>
      <select name="{CTL_SESSION}"></select>
      <table id="{ATTENDANCE_TABLE_ID}">
        <tr><th>Code</th><th>Subject</th><th>Faculty</th><th>TC</th><th>UA</th>
            <th>LE</th><th>OA</th><th>L-T-P</th></tr>
        <tr><td>CS101</td><td>Intro CS</td><td>Prof X</td><td>10</td><td>2</td>
            <td>1</td><td>3</td><td>3-1-0</td></tr>
      </table>
    </body></html>
    """


def _patch_workflow_fakes(monkeypatch):
    """Standard no-postback workflow fakes (server already has everything selected)."""
    def fake_form_fields(page_html, exclude_placeholders=True):
        return {CTL_SEMESTER: "semA", CTL_ACADEMIC_YEAR: "Y26", CTL_SESSION: "sessX"}

    def fake_dropdowns(page_html, select_name):
        return {
            CTL_SEMESTER: [("semA", "Autumn"), ("semB", "Spring")],
            CTL_ACADEMIC_YEAR: [("Y25", "2025 - 26"), ("Y26", "2026 - 27")],
            CTL_SESSION: [("sessY", "Spring 2025"), ("sessX", "Autumn 2026")],
        }[select_name]

    postback = AsyncMock(side_effect=AssertionError("redundant postback fired!"))
    monkeypatch.setattr(client_module, "extract_form_fields", fake_form_fields)
    monkeypatch.setattr(client_module, "extract_dropdown_options", fake_dropdowns)
    monkeypatch.setattr(client_module, "submit_postback", postback)
    return postback


# ── B2: attendance direct-GET fast path ─────────────────────────────────────


@pytest.fixture
def clean_url_cache():
    yield
    _resolved_url_cache.clear()


async def test_fast_path_skips_resolver_entirely(monkeypatch, clean_url_cache):
    key = _cache_key(ATTENDANCE_MODULE_NAME, ATTENDANCE_SIDEBAR_LINK_KEYWORD)
    _resolved_url_cache[key] = ("/launcher", "/ClassAttendance.aspx?tok=1", time.monotonic() + 999)

    resolve_mock = AsyncMock(side_effect=AssertionError("resolver must NOT run on fresh cache"))
    monkeypatch.setattr(NitrisClient, "_resolve_module_subpage_url", resolve_mock)
    _patch_workflow_fakes(monkeypatch)

    c = NitrisClient()
    c._debug = False
    c.client = SimpleNamespace(
        get=AsyncMock(return_value=SimpleNamespace(status_code=200, text=_attendance_html()))
    )

    holder: dict = {}
    final_html = await c.fetch_attendance(prefer_key=None, parsed_out=holder)

    assert final_html == _attendance_html()
    resolve_mock.assert_not_awaited()
    assert len(holder["result"].records) == 1
    c.client.get.assert_awaited_once()


async def test_context_loss_falls_back_to_full_resolve_once(monkeypatch, clean_url_cache):
    key = _cache_key(ATTENDANCE_MODULE_NAME, ATTENDANCE_SIDEBAR_LINK_KEYWORD)
    _resolved_url_cache[key] = ("/launcher", "/ClassAttendance.aspx?stale=1", time.monotonic() + 999)

    working_url = httpx.URL("https://portal/ClassAttendance.aspx?fresh=2")
    resolve_mock = AsyncMock(return_value=working_url)
    monkeypatch.setattr(NitrisClient, "_resolve_module_subpage_url", resolve_mock)
    _patch_workflow_fakes(monkeypatch)

    bad_resp = SimpleNamespace(
        status_code=302,
        headers={"Location": "https://portal/Error%20Pages/503.aspx"},
        text="",
    )
    good_resp = SimpleNamespace(status_code=200, text=_attendance_html())

    c = NitrisClient()
    c._debug = False
    get_mock = AsyncMock(side_effect=[bad_resp, good_resp])
    c.client = SimpleNamespace(get=get_mock)

    holder: dict = {}
    final_html = await c.fetch_attendance(prefer_key=None, parsed_out=holder)

    assert final_html == _attendance_html()
    assert resolve_mock.await_count == 1          # exactly ONE fallback resolve
    assert get_mock.await_count == 2              # failed probe + retry
    assert len(holder["result"].records) == 1


async def test_session_expiry_on_fast_path_does_not_retry(monkeypatch, clean_url_cache):
    """Login redirect on the fast path must propagate — a dead session cannot
    be fixed by re-resolving the module URL."""
    key = _cache_key(ATTENDANCE_MODULE_NAME, ATTENDANCE_SIDEBAR_LINK_KEYWORD)
    _resolved_url_cache[key] = ("/launcher", "/ClassAttendance.aspx?tok=1", time.monotonic() + 999)

    resolve_mock = AsyncMock(side_effect=AssertionError("must not resolve"))
    monkeypatch.setattr(NitrisClient, "_resolve_module_subpage_url", resolve_mock)
    _patch_workflow_fakes(monkeypatch)

    login_resp = SimpleNamespace(
        status_code=302,
        headers={"Location": "https://portal/Login.aspx"},
        text="",
    )
    c = NitrisClient()
    c._debug = False
    c.client = SimpleNamespace(get=AsyncMock(return_value=login_resp))

    from app.nitris.exceptions import SessionExpiredError

    with pytest.raises(SessionExpiredError):
        await c.fetch_attendance(prefer_key=None)
    resolve_mock.assert_not_awaited()


# ── B1: check-first wait polls ───────────────────────────────────────────────


def _install_sleep_recorder(monkeypatch, module, order):
    real_sleep = asyncio.sleep

    async def spying_sleep(delay, *a, **k):
        order.append(("sleep", delay))
        await real_sleep(0)

    monkeypatch.setattr(module.asyncio, "sleep", spying_sleep)


async def test_qp_wait_poll_checks_before_first_sleep(monkeypatch):
    order: list = []
    _install_sleep_recorder(monkeypatch, qpaper_module, order)

    svc = QPaperService(bot=object(), session_factory=None, creds_provider=None)
    snap_available = (
        QPStatus.PAPER_AVAILABLE.value, "fileid", "pdf",
        "CS101", "2026/27", "mid_sem", None, None, None,
    )

    async def fake_read(cache_id):
        order.append("read")
        return snap_available

    sentinel = QPResult(delivered=True)
    svc._read_cache = fake_read
    svc._deliver_cached = AsyncMock(return_value=sentinel)

    result = await svc._wait_and_deliver(cache_id=1, telegram_id=42)

    assert result is sentinel
    # Leading bare yield (sleep 0.0), then an IMMEDIATE read; the full poll
    # interval never precedes the first check.
    assert order[0] == ("sleep", 0.0)
    assert order[1] == "read"
    assert all(
        entry[1] != qpaper_module.WAIT_POLL_INTERVAL_SEC
        for entry in order
        if isinstance(entry, tuple) and entry[0] == "sleep"
    )


async def test_qp_wait_poll_still_paces_between_checks(monkeypatch):
    order: list = []
    _install_sleep_recorder(monkeypatch, qpaper_module, order)

    svc = QPaperService(bot=object(), session_factory=None, creds_provider=None)
    in_progress = (QPStatus.FETCH_IN_PROGRESS.value, None, None, None, None, None, None, None, None)
    available = (
        QPStatus.PAPER_AVAILABLE.value, "fileid", "pdf",
        "CS101", "2026/27", "mid_sem", None, None, None,
    )
    reads = {"n": 0}

    async def fake_read(cache_id):
        order.append("read")
        reads["n"] += 1
        return in_progress if reads["n"] == 1 else available

    sentinel = QPResult(delivered=True)
    svc._read_cache = fake_read
    svc._deliver_cached = AsyncMock(return_value=sentinel)

    await svc._wait_and_deliver(cache_id=1, telegram_id=42)

    assert order == [
        "read",
        ("sleep", qpaper_module.WAIT_POLL_INTERVAL_SEC),
        "read",
    ] or order == [
        ("sleep", 0.0),
        "read",
        ("sleep", qpaper_module.WAIT_POLL_INTERVAL_SEC),
        "read",
    ]


async def test_attachment_wait_poll_checks_before_first_sleep(monkeypatch):
    order: list = []
    _install_sleep_recorder(monkeypatch, attachment_module, order)

    svc = attachment_module.AttachmentService(bot=object(), session_factory=None)
    snap = (AttachmentStatus.AVAILABLE.value, "fileid", "pdf")

    async def fake_read(cache_id):
        order.append("read")
        return snap

    sentinel = attachment_module.AttachmentResult(delivered=True, cache_id=7)
    svc._read_cache = fake_read
    svc._deliver_cached = AsyncMock(return_value=sentinel)

    result = await svc._wait_and_deliver(
        cache_id=7, telegram_id=42, canonical_path="/a.pdf", subject="s"
    )

    assert result is sentinel
    assert order[0] == ("sleep", 0.0)
    assert order[1] == "read"


# ── Config tuning landed ─────────────────────────────────────────────────────


def test_env_tuning_lines_present():
    env_text = (REPO_ROOT / ".env").read_text(encoding="utf-8")
    assert re.search(r"^NITRIS_JOB_WORKERS=16\s*$", env_text, re.M)
    assert re.search(r"^NITRIS_INTERACTIVE_WORKERS=8\s*$", env_text, re.M)
    assert re.search(r"^DISPATCH_INTERVAL_SECONDS=2\s*$", env_text, re.M)
    assert re.search(r"^SCHEDULER_POLL_INTERVAL=10\s*$", env_text, re.M)


def test_config_loads_tuned_values_from_dotenv():
    """With the four keys stripped from the process env, dotenv (.env) must win.
    Proves the deployed .env tuning actually reaches Config."""
    env = {
        k: v for k, v in os.environ.items()
        if k not in (
            "NITRIS_INTERACTIVE_WORKERS",
            "NITRIS_JOB_WORKERS",
            "DISPATCH_INTERVAL_SECONDS",
            "SCHEDULER_POLL_INTERVAL",
        )
    }
    proc = subprocess.run(
        [sys.executable, "-c",
         "from app.config import Config; c = Config(); "
         "print(c.NITRIS_INTERACTIVE_WORKERS, c.NITRIS_JOB_WORKERS, "
         "c.DISPATCH_INTERVAL_SECONDS, c.SCHEDULER_POLL_INTERVAL)"],
        capture_output=True, text=True, env=env, cwd=str(REPO_ROOT), timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.split() == ["8", "16", "2", "10"]
