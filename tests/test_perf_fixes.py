"""Proof tests for the three perf/reliability fixes:

A. BS4 parsing runs OFF the event loop (asyncio.to_thread at async call sites).
B. The attendance workflow's winning page is parsed exactly ONCE (parsed_out).
C. LoginUnavailableError no longer amplifies through queue-level retries.
"""
from __future__ import annotations

import asyncio
import re
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import app.services.attendance_service as attendance_service_module
from app.nitris.exceptions import AttendanceParseError, LoginUnavailableError
from app.nitris.job_queue import NitrisJobQueue, Priority
from app.nitris.parser import AttendanceResult

REPO_ROOT = Path(__file__).resolve().parents[1]


def _src(rel: str) -> str:
    return (REPO_ROOT / rel).read_text(encoding="utf-8")


# ── Fix A: event-loop offload ────────────────────────────────────────────────


async def test_final_attendance_parse_runs_off_event_loop(monkeypatch):
    loop_thread = threading.get_ident()
    seen = {}

    def fake_parse(html):
        seen["thread"] = threading.get_ident()
        if "no-table" in html:
            raise AttendanceParseError("bad")
        return AttendanceResult(student_info="x", records=[])

    monkeypatch.setattr(
        attendance_service_module, "parse_attendance_html", fake_parse
    )
    client = SimpleNamespace(
        fetch_attendance=AsyncMock(return_value="<html>whatever</html>")
    )

    await attendance_service_module.get_attendance_data("u", "p", client=client)

    assert seen["thread"] != loop_thread


async def test_no_sync_bs4_calls_remain_in_async_hot_paths():
    """Guardrail: known parse call sites must be offloaded via to_thread."""
    client_src = _src("app/nitris/client.py")
    assert not re.search(r"^\s*form_state = extract_form_fields\(", client_src, re.M)
    assert not re.search(r"^\s*(sem|year|session|dept)_options = extract_dropdown_options\(", client_src, re.M)
    assert "_seed_module_urls_from_home(resp.text)" not in client_src  # must be to_thread'd

    sync_src = _src("app/workers/sync_worker.py")
    assert "= parse_messages_list_html(" not in sync_src
    assert "return (msg[\"portal_message_id\"], parse_message_detail_html(" not in sync_src

    exam_src = _src("app/services/examination_service.py")
    assert "return parse_question_papers_html(html)" not in exam_src

    tt_src = _src("app/services/timetable_service.py")
    assert "slots = parse_home_page(html)" not in tt_src

    jh_src = _src("app/nitris/job_handlers.py")
    assert "records.extend(parse_question_papers_html(" not in jh_src
    assert "return parse_home_page(home_html).timetable" not in jh_src


# ── Fix B: single-parse contract ─────────────────────────────────────────────


async def test_pre_parsed_result_is_used_without_reparse(monkeypatch):
    sentinel = AttendanceResult(student_info="S", records=[])

    async def fake_fetch(*args, **kwargs):
        holder = kwargs.get("parsed_out")
        assert isinstance(holder, dict)
        holder["result"] = sentinel
        return "<html>winning page</html>"

    def must_not_run(html):
        raise AssertionError("parse_attendance_html must NOT run when parsed_out is supplied")

    monkeypatch.setattr(attendance_service_module, "parse_attendance_html", must_not_run)
    client = SimpleNamespace(fetch_attendance=fake_fetch)

    result = await attendance_service_module.get_attendance_data("u", "p", client=client)

    assert result is sentinel


async def test_fallback_parses_when_parsed_out_absent(monkeypatch):
    """Mocked/alternate clients that ignore parsed_out still work via fallback."""

    async def fake_fetch(*args, **kwargs):
        # Simulates a client that does not honour parsed_out.
        return "<html>table</html>"

    monkeypatch.setattr(
        attendance_service_module,
        "parse_attendance_html",
        lambda html: AttendanceResult(student_info="fallback", records=[]),
    )
    client = SimpleNamespace(fetch_attendance=fake_fetch)

    result = await attendance_service_module.get_attendance_data("u", "p", client=client)

    assert result.student_info == "fallback"


def test_client_fetch_attendance_has_single_parse_contract():
    """Source guardrail: fetch_attendance accepts parsed_out and publishes the
    trial-parsed result instead of discarding it."""
    src = _src("app/nitris/client.py")
    assert "parsed_out: Optional[dict] = None" in src
    assert 'parsed_out["result"] = parsed_candidate' in src
    # Trial parse must run off-loop too.
    assert re.search(
        r"await asyncio\.to_thread\(\s*parse_attendance_html,\s*temp_html",
        src,
    )


# ── Fix C: login-storm cap ───────────────────────────────────────────────────


class _StubQueue:
    def task_done(self):
        pass


async def _run_one_job(queue_obj, job_type, exc):
    calls = {"n": 0}

    async def handler(payload, bot):
        calls["n"] += 1
        raise exc

    queue_obj.register_handler(job_type, handler)
    fut = await queue_obj.enqueue(job_type, user_id=1, priority=Priority.HIGH)
    job = await queue_obj._interactive_queue.get()
    await queue_obj._run_job(job, _StubQueue(), 0, "interactive")
    return fut, calls


async def test_login_unavailable_fails_fast_without_queue_retry():
    q = NitrisJobQueue(num_workers=0)
    fut, calls = await _run_one_job(q, "storm_test", LoginUnavailableError("portal down"))

    assert calls["n"] == 1  # exactly ONE handler execution — no retry storm
    assert fut.done() and fut.exception() is not None
    assert not getattr(fut, "_retry_scheduled", False)


async def test_transient_work_errors_still_retry():
    q = NitrisJobQueue(num_workers=0)
    fut, calls = await _run_one_job(q, "transient_test", RuntimeError("mid-scrape hiccup"))

    assert calls["n"] == 1  # ran once; retry scheduled for later
    assert getattr(fut, "_retry_scheduled", False) or not fut.done()


async def test_login_error_still_fails_fast():
    from app.nitris.exceptions import LoginError

    q = NitrisJobQueue(num_workers=0)
    fut, calls = await _run_one_job(q, "login_err_test", LoginError("bad creds"))

    assert calls["n"] == 1
    assert fut.done() and fut.exception() is not None
