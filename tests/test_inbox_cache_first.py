"""Tests for cache-first inbox list rendering and body TTL behavior."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone, timedelta
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.config import config
from app.db.models import User, InboxMessage
from app.bot.telegram import render_single_message, _render_inbox_list_text


class FakeRecord:
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


class FakeEvent:
    def __init__(self):
        self.answers = []
        self.message = self

    async def answer(self, text, reply_markup=None, parse_mode=None):
        self.answers.append(text)
        return self

    async def edit_text(self, text, reply_markup=None, parse_mode=None):
        self.answers.append(text)
        return self

    async def delete(self):
        pass


@pytest.mark.asyncio
async def test_inbox_list_rendering():
    """Test _render_inbox_list_text formatting."""
    msgs = [
        FakeRecord(
            id=1,
            sender="Prof Smith",
            subject="Midsem Schedule Announced",
            sent_on=datetime(2026, 8, 20, 10, 0, tzinfo=timezone.utc),
            is_read=False,
        ),
        FakeRecord(
            id=2,
            sender="Dean Academic",
            subject="Registration Deadline Extended",
            sent_on=datetime(2026, 8, 19, 14, 30, tzinfo=timezone.utc),
            is_read=True,
        ),
    ]

    text = _render_inbox_list_text(msgs, page=1, refreshing=False)
    assert "Midsem Schedule Announced" in text
    assert "Registration Deadline Extended" in text
    assert "🔴" in text  # unread
    assert "⚪" in text  # read

    text_refreshing = _render_inbox_list_text(msgs, page=1, refreshing=True)
    assert "Refreshing from NITRIS in background" in text_refreshing


@pytest.mark.asyncio
async def test_render_single_message_cached():
    """Fresh cached body renders instantly without enqueuing a background fetch."""
    user = FakeRecord(id=10, roll_number="125AI0001")
    fresh_msg = FakeRecord(
        id=100,
        user_id=10,
        sender="Prof Smith",
        subject="Exam Notice",
        sent_on=datetime(2026, 8, 20, 10, 0, tzinfo=timezone.utc),
        body="This is the full notice body cached in DB.",
        body_fetched_at=datetime.now(timezone.utc) - timedelta(minutes=10),
        attachment_url=None,
        is_read=True,
    )

    event = FakeEvent()
    fake_session = AsyncMock()

    with patch("app.nitris.job_queue.nitris_job_queue.enqueue") as mock_enqueue:
        await render_single_message(event, user, fresh_msg, fake_session)
        mock_enqueue.assert_not_called()
        assert len(event.answers) == 1
        assert "This is the full notice body" in event.answers[0]


@pytest.mark.asyncio
async def test_render_single_message_old_body_served_cached():
    """CACHE-FIRST FOREVER: an old stored body is served as-is — no time-based
    refetching. Freshness comes from sync edit-detection + Refresh Now."""
    user = FakeRecord(id=10, roll_number="125AI0001")
    old_msg = FakeRecord(
        id=100,
        user_id=10,
        sender="Prof Smith",
        subject="Exam Notice",
        sent_on=datetime(2026, 8, 20, 10, 0, tzinfo=timezone.utc),
        body="Old body",
        body_fetched_at=datetime.now(timezone.utc) - timedelta(days=3),  # very old
        attachment_url=None,
        is_read=True,
    )

    event = FakeEvent()

    with patch("app.nitris.job_queue.nitris_job_queue.enqueue") as mock_enqueue:
        await render_single_message(event, user, old_msg)
        mock_enqueue.assert_not_called()
        assert len(event.answers) == 1
        assert "Old body" in event.answers[0]


@pytest.mark.asyncio
async def test_render_single_message_missing_body_fetches():
    """body=None (true first-ever open) triggers exactly one deduplicated fetch."""
    from datetime import datetime as _dt

    user = FakeRecord(id=10, roll_number="125AI0001")
    never_fetched = FakeRecord(
        id=101,
        user_id=10,
        sender="Prof Smith",
        subject="Backlog Notice",
        sent_on=datetime(2026, 8, 20, 10, 0, tzinfo=timezone.utc),
        body=None,
        body_fetched_at=None,
        attachment_url=None,
        is_read=True,
    )

    event = FakeEvent()
    future = asyncio.Future()
    future.set_result({"success": True})

    with patch("app.nitris.job_queue.nitris_job_queue.enqueue", return_value=future) as mock_enqueue:
        await render_single_message(event, user, never_fetched)
        mock_enqueue.assert_called_once()
        call_kwargs = mock_enqueue.call_args.kwargs
        assert call_kwargs["dedup_key"] == "inbox_detail:user:10:msg:101"
