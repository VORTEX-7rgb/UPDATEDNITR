"""Attendance fast-path cliff fix: successful scrapes keep URLs warm."""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

import app.nitris.client as client_module
from app.bot.handlers.papers import YEAR_MAP  # noqa: F401 (ensures app importable)
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


@pytest.fixture
def clean_url_cache():
    _resolved_url_cache.clear()
    yield
    _resolved_url_cache.clear()


async def test_fast_path_success_extends_entry_expiry(monkeypatch, clean_url_cache):
    """THE cliff fix: a successful fast-path scrape must refresh the cached
    URL pair's expiry so continuously-active students never degrade to the
    launcher-visit resolve."""
    key = _cache_key(ATTENDANCE_MODULE_NAME, ATTENDANCE_SIDEBAR_LINK_KEYWORD)
    original_expiry = time.monotonic() + 60  # barely alive
    _resolved_url_cache[key] = ("/launcher", "/ClassAttendance.aspx?tok=1", original_expiry)

    resolve_mock = AsyncMock(side_effect=AssertionError("resolver must NOT run"))
    monkeypatch.setattr(NitrisClient, "_resolve_module_subpage_url", resolve_mock)
    _patch_workflow_fakes(monkeypatch)

    c = NitrisClient()
    c._debug = False
    c.client = SimpleNamespace(
        get=AsyncMock(return_value=SimpleNamespace(status_code=200, text=_attendance_html()))
    )

    holder: dict = {}
    await c.fetch_attendance(prefer_key=None, parsed_out=holder)

    assert len(holder["result"].records) == 1
    entry = _resolved_url_cache[key]
    assert entry[0] == "/launcher"           # hrefs preserved…
    assert entry[1] == "/ClassAttendance.aspx?tok=1"
    assert entry[2] > original_expiry + 100  # …expiry extended well past old


def test_touch_preserves_hrefs_and_is_noop_on_missing():
    _resolved_url_cache.clear()
    # Missing entry → harmless no-op.
    NitrisClient._touch_resolved_url("Nope", "nope")

    key = _cache_key("M", "K")
    _resolved_url_cache[key] = ("/L", "/S", time.monotonic() + 10)
    NitrisClient._touch_resolved_url("M", "K")
    e = _resolved_url_cache[key]
    assert e[0] == "/L" and e[1] == "/S"
    assert e[2] > time.monotonic() + 100     # bumped to full TTL


async def test_expired_entry_still_falls_back_cleanly(monkeypatch, clean_url_cache):
    """An already-expired entry behaves exactly as before: no fast path,
    one full resolve, one fetch — the touch only prevents FUTURE expiry."""
    key = _cache_key(ATTENDANCE_MODULE_NAME, ATTENDANCE_SIDEBAR_LINK_KEYWORD)
    _resolved_url_cache[key] = ("/launcher", "/stale.aspx", time.monotonic() - 5)

    working_url = httpx.URL("https://portal/ClassAttendance.aspx?fresh")
    resolve_mock = AsyncMock(return_value=working_url)
    monkeypatch.setattr(NitrisClient, "_resolve_module_subpage_url", resolve_mock)
    _patch_workflow_fakes(monkeypatch)

    c = NitrisClient()
    c._debug = False
    get_mock = AsyncMock(return_value=SimpleNamespace(status_code=200, text=_attendance_html()))
    c.client = SimpleNamespace(get=get_mock)

    holder: dict = {}
    final_html = await c.fetch_attendance(prefer_key=None, parsed_out=holder)

    assert final_html == _attendance_html()
    assert resolve_mock.await_count == 1
    assert len(holder["result"].records) == 1


def test_ttl_default_aligned_with_session_pool():
    from app.config import Config
    c = Config()
    assert float(c.NITRIS_URL_CACHE_TTL_SECONDS) == 1800.0
