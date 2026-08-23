"""UX-latency fix proofs (F1-F6): no dead RTTs on repeat taps, ack-first
paper delivery, single-bubble dereg flows, visible cooldown, typing action."""
from __future__ import annotations

import sys
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("ENCRYPTION_KEY", "ZmDfcTF7_60GrrY167zsiPd67pEvs0aGOv2oasOM1Pg=")
os.environ.setdefault("BOT_TOKEN", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost:5432/test")

from aiogram.exceptions import TelegramBadRequest

from app.ui.surface import show

REPO_ROOT = Path(__file__).resolve().parents[1]


def _not_modified() -> TelegramBadRequest:
    return TelegramBadRequest(method=MagicMock(), message="Bad Request: message is not modified")


# ── F1: idempotent re-renders are free and never escalate ───────────────────


async def test_show_swallows_not_modified_without_fallback_send():
    msg = AsyncMock()
    msg.edit_text.side_effect = _not_modified()

    result = await show(msg, "same text")

    assert result is msg          # treated as success — no fresh send
    msg.answer.assert_not_awaited()


async def test_timetable_day_switch_survives_not_modified(monkeypatch):
    """Re-tapping the same day used to raise into the global error handler."""
    from app.bot.handlers import timetable as tt

    monkeypatch.setattr(tt, "_handle_day_display", AsyncMock(return_value=("DAY", None)))

    callback = AsyncMock()
    callback.data = "tt_day_2"
    callback.from_user.id = 1
    callback.message.edit_text.side_effect = _not_modified()

    await tt.cb_select_day(callback)  # must NOT raise

    # No fallback bubble was spawned for a benign idempotent tap.
    callback.message.answer.assert_not_awaited()


def test_raw_edit_sites_routed_through_safe_renderer():
    """Guardrail: known repeat-tap screens must use show(), not raw edit_text."""
    for rel in ("app/bot/handlers/papers.py", "app/bot/handlers/timetable.py"):
        src = (REPO_ROOT / rel).read_text(encoding="utf-8")
        assert "callback.message.edit_text(" not in src, rel


# ── F3: qp_dl_ acks BEFORE any DB work ───────────────────────────────────────


async def test_paper_download_acks_before_cache_read():
    from app.bot.handlers import papers as papers_mod
    from app.bot.handlers.papers import handle_paper_download
    from app.services.qpaper_service import QPResult

    order: list[str] = []

    async def mock_answer(*a, **k):
        order.append("answer")

    async def mock_read(cache_id):
        order.append("cache_read")
        return ("paper_available", "fid", "pdf", "CS", "Y", "T", None, None, None)

    fake_service = SimpleNamespace(
        _read_cache=mock_read,
        deliver=AsyncMock(return_value=QPResult(delivered=True)),
    )

    callback = AsyncMock()
    callback.data = "qp_dl_42"
    callback.from_user.id = 7
    callback.answer.side_effect = mock_answer

    db_ctx = MagicMock()
    db_ctx.__aenter__ = AsyncMock(return_value=AsyncMock())
    db_ctx.__aexit__ = AsyncMock(return_value=False)

    repo = MagicMock()
    repo.get_by_telegram_id = AsyncMock(return_value=None)

    with patch("app.bot.handlers.papers.qpaper_registry") as reg, \
         patch("app.bot.handlers.papers.get_db_session", return_value=db_ctx), \
         patch("app.bot.handlers.papers.UserRepository", return_value=repo):
        reg.qpaper_service = fake_service
        state = AsyncMock()
        await handle_paper_download(callback, state)

    assert "answer" in order and "cache_read" in order
    assert order.index("answer") < order.index("cache_read"), (
        "callback.answer() must fire before the cache/DB reads (spinner-first)"
    )


# ── F4: deregister collapses to one bubble edit ──────────────────────────────


def _dash_mocks():
    """Shared mocks for the dereg handlers: registered user + canned render."""
    user = MagicMock()
    user.id = 5
    res = MagicMock()
    res.scalar_one_or_none.return_value = user
    session = AsyncMock()
    session.execute = AsyncMock(return_value=res)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


async def test_cancel_deregister_edits_bubble_never_deletes(monkeypatch):
    from app.bot.handlers import registration as reg_mod
    from app.bot.handlers.registration import handle_cancel_deregister

    monkeypatch.setattr(reg_mod, "get_db_session", lambda: _dash_mocks())
    monkeypatch.setattr(reg_mod, "render_dashboard", AsyncMock(return_value="DASHBOARD"))
    kb = MagicMock()
    monkeypatch.setattr(reg_mod, "get_dashboard_keyboard", lambda n: kb)

    callback = AsyncMock()
    callback.from_user.id = 9

    await handle_cancel_deregister(callback, AsyncMock())

    callback.message.delete.assert_not_awaited()
    # Exactly ONE render of the combined cancelled+dashboard screen.
    assert callback.message.edit_text.await_count == 1
    sent = callback.message.edit_text.await_args.kwargs["text"] if \
        callback.message.edit_text.await_args.kwargs.get("text") else \
        callback.message.edit_text.await_args.args[0]
    assert "cancelled" in sent.lower()
    assert "DASHBOARD" in sent
    # The old flow's two extra fresh sends are gone.
    callback.message.answer.assert_not_awaited()


async def test_confirm_deregister_edits_instead_of_delete_plus_send(monkeypatch):
    from app.bot.handlers import registration as reg_mod
    from app.bot.handlers.registration import handle_confirm_deregister

    user = MagicMock()
    user.id = 5
    repo_inst = MagicMock()
    repo_inst.get_by_telegram_id = AsyncMock(return_value=user)
    repo_inst.delete_user = AsyncMock()

    begin_cm = MagicMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)

    session = MagicMock()
    session.begin.return_value = begin_cm

    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=False)

    monkeypatch.setattr(reg_mod, "get_db_session", lambda: ctx)

    with patch("app.bot.handlers.registration.UserRepository", return_value=repo_inst):
        callback = AsyncMock()
        callback.from_user.id = 9
        await handle_confirm_deregister(callback, AsyncMock())

    callback.message.delete.assert_not_awaited()
    assert callback.message.edit_text.await_count == 1
    callback.message.answer.assert_not_awaited()
    repo_inst.delete_user.assert_awaited_once()


# ── F2/F6 guardrails ─────────────────────────────────────────────────────────


def test_attachment_cooldown_has_visible_fresh_feedback():
    """Second callback.answer on the same query is ignored by Telegram — the
    cooldown branch MUST also surface feedback via a fresh message."""
    src = (REPO_ROOT / "app/bot/handlers/inbox.py").read_text(encoding="utf-8")
    needle = "before downloading this attachment again"
    assert needle in src
    assert src.index(needle) > src.index("show_alert=True")


def test_start_shows_typing_action():
    src = (REPO_ROOT / "app/bot/handlers/registration.py").read_text(encoding="utf-8")
    assert 'send_chat_action(chat_id=message.chat.id, action="typing")' in src
