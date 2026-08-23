"""Pre-warm job-handler + admin command proofs (stop flag, worklist, gate)."""
from __future__ import annotations

import asyncio
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

from app.bot import qpaper_registry
from app.nitris.job_handlers import handle_qp_prewarm_subject
from app.services import prewarm_state as ps_mod

REPO_ROOT = Path(__file__).resolve().parents[1]


def _fresh_state() -> ps_mod.PrewarmState:
    return ps_mod.PrewarmState()


def _wire(monkeypatch, state, svc):
    monkeypatch.setattr(ps_mod, "prewarm_state", state)
    monkeypatch.setattr(qpaper_registry, "qpaper_service", svc)


async def test_job_handler_respects_stop_flag(monkeypatch):
    state = _fresh_state()
    state.stop_event.set()
    svc = MagicMock(prewarm_one=AsyncMock(
        side_effect=AssertionError("must not run when stopped")))
    _wire(monkeypatch, state, svc)

    result = await handle_qp_prewarm_subject(
        {"subject_code": "CS101", "academic_year": "2025-26/Autumn",
         "donor_user_id": 1}, None)

    assert result.get("stopped") is True
    svc.prewarm_one.assert_not_awaited()


async def test_job_handler_counts_and_skips_missing_rows(monkeypatch):
    """Both exam rows missing AND metadata sync fails → skipped counters,
    no crash; subjects_done increments."""
    state = _fresh_state()
    calls = {"prewarm": 0}

    async def fake_prewarm(cache_id, donor):
        calls["prewarm"] += 1
        return "available"

    svc = MagicMock(prewarm_one=fake_prewarm)
    svc._fetch_metadata_via_pool = AsyncMock(side_effect=RuntimeError("no net"))
    _wire(monkeypatch, state, svc)

    # get_cached_paper → None both times; metadata path explodes harmlessly.
    exam_instance = MagicMock()
    exam_instance.get_cached_paper = AsyncMock(return_value=None)
    exam_cls = MagicMock(return_value=exam_instance)
    exam_instance.persist_subject_metadata = AsyncMock()

    session = MagicMock()
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=False)

    with patch("app.db.database.get_db_session", side_effect=[ctx]), \
         patch("app.services.examination_service.ExaminationService", exam_cls):
        result = await handle_qp_prewarm_subject(
            {"subject_code": "CS101", "academic_year": "2025-26/Autumn",
             "donor_user_id": 1}, None)

    assert result["success"] is True
    assert state.counters["subjects_done"] == 1
    assert state.counters["skipped"] >= 2  # no rows created → both skipped
    assert calls["prewarm"] == 0


async def test_job_handler_full_flow_counts_available(monkeypatch):
    state = _fresh_state()
    async def fake_prewarm(cache_id, donor):
        return "available" if cache_id == 11 else "not_available"
    svc = MagicMock(prewarm_one=fake_prewarm)
    _wire(monkeypatch, state, svc)

    mid_row = SimpleNamespace(id=11, exam_type="mid_sem")
    end_row = SimpleNamespace(id=22, exam_type="end_sem")
    exam_instance = MagicMock()
    exam_instance.get_cached_paper = AsyncMock(side_effect=[
        mid_row, end_row])  # both already exist → no metadata fetch
    exam_cls = MagicMock(return_value=exam_instance)

    session = MagicMock()
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=False)

    with patch("app.db.database.get_db_session", side_effect=[ctx]), \
         patch("app.services.examination_service.ExaminationService", exam_cls):
        result = await handle_qp_prewarm_subject(
            {"subject_code": "CS201", "academic_year": "2025-26/Autumn",
             "donor_user_id": 1}, None)

    assert result["success"] is True
    # mid row (id=11) → available; end row (id=22) → not_available
    assert sorted(result["results"]) == ["CS201:end_sem:not_available",
                                         "CS201:mid_sem:available"]
    assert state.counters["available"] == 1
    assert state.counters["not_available"] == 1
    assert state.counters["subjects_done"] == 1


# ── Admin command guards ─────────────────────────────────────────────────────


def test_worklist_sql_targets_snapshots_module():
    src = (REPO_ROOT / "app/bot/handlers/admin.py").read_text(encoding="utf-8")
    assert "s.module_name = 'attendance'" in src
    assert "PREWARM_MAX_ITEMS" in src


async def test_non_admin_gets_silently_ignored(monkeypatch):
    from app.bot.handlers import admin as admin_mod

    cfg = SimpleNamespace(ADMIN_TELEGRAM_IDS=frozenset({999}))
    monkeypatch.setattr(admin_mod.config, "ADMIN_TELEGRAM_IDS",
                        frozenset({999}), raising=False)

    message = AsyncMock()
    message.from_user.id = 123456789  # NOT admin
    message.text = "/admin_prewarm dry"

    await admin_mod.cmd_admin_prewarm(message)

    message.answer.assert_not_awaited()
