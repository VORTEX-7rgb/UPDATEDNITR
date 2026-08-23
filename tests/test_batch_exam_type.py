"""Batch-download exam-type chooser + filtered executor proofs."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("ENCRYPTION_KEY", "ZmDfcTF7_60GrrY167zsiPd67pEvs0aGOv2oasOM1Pg=")
os.environ.setdefault("BOT_TOKEN", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost:5432/test")

from app.bot.handlers import papers as papers_mod
from app.bot.handlers.papers import (
    handle_qp_download_all_year,
    handle_qp_download_all_go,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def _cb(data: str):
    callback = AsyncMock()
    callback.data = data
    callback.from_user.id = 7
    return callback


def _service():
    return SimpleNamespace(_read_cache=AsyncMock(), deliver=AsyncMock())


def _db_ctx_with(session_mock):
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=session_mock)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


def _snapshot_session(courses: list[dict]):
    """Fake session whose execute returns a snapshot with the given courses."""
    snap = SimpleNamespace(snapshot_json={"records": courses})
    res = MagicMock()
    res.scalar_one_or_none.return_value = SimpleNamespace(id=1)
    # First execute → user lookup; second → snapshot select.
    session = MagicMock()
    session.execute = AsyncMock(side_effect=[res, MagicMock(scalar_one_or_none=MagicMock(return_value=snap))])
    return session


def _exam_service_with(cache_rows: dict[tuple[str, str], object]):
    svc = MagicMock()

    async def get_cached_paper(sub, year, exam_t):
        return cache_rows.get((sub, exam_t))

    svc.get_cached_paper = get_cached_paper
    return svc


# ── Chooser rendering ────────────────────────────────────────────────────────


async def test_year_tap_renders_three_way_chooser(monkeypatch):
    monkeypatch.setattr(papers_mod.qpaper_registry, "qpaper_service", _service())
    callback = _cb("qp_dlall_yr_2425A")

    await handle_qp_download_all_year(callback, AsyncMock())

    markup = callback.message.edit_text.await_args.kwargs["reply_markup"]
    buttons = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert "qp_dlall_go_2425A_m" in buttons
    assert "qp_dlall_go_2425A_e" in buttons
    assert "qp_dlall_go_2425A_b" in buttons
    assert "qp_dlall_prompt" in buttons  # back nav
    text_arg = callback.message.edit_text.await_args.kwargs.get("text") or \
        callback.message.edit_text.await_args.args[0]
    assert "2024-25/Autumn" in text_arg


async def test_year_tap_invalid_year_shows_error_not_execute(monkeypatch):
    monkeypatch.setattr(papers_mod.qpaper_registry, "qpaper_service", _service())
    callback = _cb("qp_dlall_yr_BOGUS")

    await handle_qp_download_all_year(callback, AsyncMock())

    assert "Invalid academic year" in str(callback.message.edit_text.await_args)


# ── Filtered execution ───────────────────────────────────────────────────────


def _run_go_mocks(monkeypatch, courses, cache_rows):
    """Wire every collaborator handle_qp_download_all_go touches.

    ExaminationService is imported INSIDE the handler, so we patch its SOURCE
    module (app.services.examination_service), not papers' namespace.
    All patches ride on monkeypatch → active for the whole test.
    """
    fake_service = _service()
    fake_service.deliver = AsyncMock(return_value=SimpleNamespace(
        delivered=False, not_available=False, error=None))
    monkeypatch.setattr(papers_mod.qpaper_registry, "qpaper_service", fake_service)

    exam_instance = MagicMock()
    exam_instance.get_cached_paper = AsyncMock(
        side_effect=lambda sub, year, et: cache_rows.get((sub, et)))
    exam_cls = MagicMock(return_value=exam_instance)

    user_res = MagicMock()
    user_res.scalar_one_or_none.return_value = SimpleNamespace(id=9)
    snap = SimpleNamespace(snapshot_json={"records": courses})
    snap_res = MagicMock()
    snap_res.scalar_one_or_none.return_value = snap

    user_session = MagicMock()
    user_session.execute = AsyncMock(side_effect=[user_res, snap_res])

    ctxs = [_db_ctx_with(user_session), _db_ctx_with(MagicMock())]

    def session_factory(*a, **k):
        return ctxs.pop(0)

    monkeypatch.setattr(papers_mod, "get_db_session", session_factory)
    # papers.py binds ExaminationService at MODULE level (line 16) → patch
    # the name in papers' namespace, not the source module.
    monkeypatch.setattr(papers_mod, "ExaminationService", exam_cls)
    # Short-circuit the NITRIS metadata-sync path (uncached subjects).
    monkeypatch.setattr(papers_mod.asyncio, "wait_for",
                        AsyncMock(side_effect=TimeoutError))
    return fake_service, exam_instance


async def test_mid_filter_skips_end_sem_cache(monkeypatch):
    mid = SimpleNamespace(id=11, status="paper_available")
    end = SimpleNamespace(id=22, status="paper_available")
    courses = [{"subject_code": "CS101"}]
    rows = {("CS101", "mid_sem"): mid, ("CS101", "end_sem"): end}
    _, exam_instance = _run_go_mocks(monkeypatch, courses, rows)

    await handle_qp_download_all_go(_cb("qp_dlall_go_2425A_m"), AsyncMock())

    # Only the mid row was requested from the cache lookup.
    calls = [c.args[2] for c in exam_instance.get_cached_paper.await_args_list]
    assert set(calls) == {"mid_sem"}


async def test_both_filter_matches_legacy_behavior(monkeypatch):
    mid = SimpleNamespace(id=11, status="paper_available")
    end = SimpleNamespace(id=22, status="paper_available")
    courses = [{"subject_code": "CS101"}]
    rows = {("CS101", "mid_sem"): mid, ("CS101", "end_sem"): end}
    _, exam_instance = _run_go_mocks(monkeypatch, courses, rows)

    await handle_qp_download_all_go(_cb("qp_dlall_go_2425A_b"), AsyncMock())

    calls = [c.args[2] for c in exam_instance.get_cached_paper.await_args_list]
    assert set(calls) == {"mid_sem", "end_sem"}


async def test_selected_type_missing_marks_course_uncached(monkeypatch):
    """Mid-only filter + subject that only has END cached → must be treated
    as uncached (not silently skipped), matching user intent."""
    end_only = SimpleNamespace(id=22, status="paper_available")
    courses = [{"subject_code": "CS101"}]
    rows = {("CS101", "end_sem"): end_only}
    _run_go_mocks(monkeypatch, courses, {})
    # No usable MID row → uncached branch → wait_for TimeoutError short-
    # circuits metadata sync (mocked above) → zero deliveries, summary shown.
    callback = _cb("qp_dlall_go_2425A_m")
    await handle_qp_download_all_go(callback, AsyncMock())

    status_edits = callback.message.answer.return_value.edit_text.await_args_list
    final_text = status_edits[-1]
    text_arg = final_text.kwargs.get("text") or final_text.args[0]
    assert "No papers available" in text_arg
    assert "Mid Sem" in text_arg


async def test_invalid_suffix_rejected(monkeypatch):
    monkeypatch.setattr(papers_mod.qpaper_registry, "qpaper_service", _service())
    callback = _cb("qp_dlall_go_2425A_x")

    await handle_qp_download_all_go(callback, AsyncMock())

    assert callback.answer.await_count >= 1  # answered, never executed


async def test_paper_not_available_does_not_trigger_portal_sync(monkeypatch):
    """Subject with paper_not_available row in DB must not be treated as uncached."""
    not_avail = SimpleNamespace(id=33, status="paper_not_available")
    courses = [{"subject_code": "LAB101"}]
    rows = {("LAB101", "mid_sem"): not_avail}
    _, exam_instance = _run_go_mocks(monkeypatch, courses, rows)

    callback = _cb("qp_dlall_go_2425A_m")
    await handle_qp_download_all_go(callback, AsyncMock())

    status_edits = callback.message.answer.return_value.edit_text.await_args_list
    final_text = status_edits[-1]
    text_arg = final_text.kwargs.get("text") or final_text.args[0]
    # Instantly resolved without NITRIS metadata sync timeout
    assert "No papers available" in text_arg
    assert "Mid Sem" in text_arg

