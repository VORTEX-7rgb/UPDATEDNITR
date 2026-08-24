"""Tests for Phase 4: Parallel inbox detail fetching, semaphore bounds, error handling,
and the INBOX_SYNC_DETAIL_LIMIT newest-N cap."""
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["ENCRYPTION_KEY"] = "ZmDfcTF7_60GrrY167zsiPd67pEvs0aGOv2oasOM1Pg="
os.environ["BOT_TOKEN"] = "test"
os.environ["DATABASE_URL"] = "postgresql+asyncpg://postgres:postgres@localhost:5432/test"

HTML_MESSAGES_LIST = """
<div>
    <a class="message-item" href="Message.aspx?i=AAA111">
        <div class="mail-contnet">
            <span class="message-title">Notice 1</span>
            <span class="mail-desc">Prof 1</span>
            <span class="time">21 Aug 2026</span>
        </div>
    </a>
    <a class="message-item" href="Message.aspx?i=BBB222">
        <div class="mail-contnet">
            <span class="message-title">Notice 2</span>
            <span class="mail-desc">Prof 2</span>
            <span class="time">21 Aug 2026</span>
        </div>
    </a>
    <a class="message-item" href="Message.aspx?i=CCC333">
        <div class="mail-contnet">
            <span class="message-title">Notice 3</span>
            <span class="mail-desc">Prof 3</span>
            <span class="time">21 Aug 2026</span>
        </div>
    </a>
</div>
"""


@pytest.mark.asyncio
async def test_prepare_inbox_sync_parallel_concurrency():
    """prepare_inbox_sync fetches detail pages in parallel bounded by semaphore."""
    from app.workers.sync_worker import prepare_inbox_sync

    client = AsyncMock()
    client.fetch_messages_list.return_value = HTML_MESSAGES_LIST

    concurrency_counter = 0
    max_observed_concurrency = 0

    async def mock_fetch_detail(token):
        nonlocal concurrency_counter, max_observed_concurrency
        concurrency_counter += 1
        max_observed_concurrency = max(max_observed_concurrency, concurrency_counter)
        await asyncio.sleep(0.05)
        concurrency_counter -= 1
        return "<table id='ctl00_ContentPlaceHolder2_tblSingleMsg'><span id='ContentPlaceHolder2_lblBody'>Notice body</span></table>"

    client.fetch_message_detail.side_effect = mock_fetch_detail

    with patch("app.workers.sync_worker.get_db_session") as mock_db_ctx, \
         patch("app.workers.sync_worker.wait_for_db_recovery", return_value=None):
        session = AsyncMock()
        mock_db_ctx.return_value.__aenter__.return_value = session
        
        with patch("app.db.repositories.inbox_repository.InboxRepository.get_by_portal_message_ids", return_value=[]):
            scraped, details, existing = await prepare_inbox_sync(client, user_id=1)

    assert len(scraped) == 3
    assert len(details) == 3
    assert max_observed_concurrency > 1, "Detail fetching should be parallel"


@pytest.mark.asyncio
async def test_prepare_inbox_sync_session_expired_propagates():
    """SessionExpiredError in detail fetch propagates out of prepare_inbox_sync."""
    from app.workers.sync_worker import prepare_inbox_sync
    from app.nitris.exceptions import SessionExpiredError

    client = AsyncMock()
    client.fetch_messages_list.return_value = HTML_MESSAGES_LIST
    client.fetch_message_detail.side_effect = SessionExpiredError("Session timed out")

    with patch("app.workers.sync_worker.get_db_session") as mock_db_ctx, \
         patch("app.workers.sync_worker.wait_for_db_recovery", return_value=None):
        session = AsyncMock()
        mock_db_ctx.return_value.__aenter__.return_value = session
        
        with patch("app.db.repositories.inbox_repository.InboxRepository.get_by_portal_message_ids", return_value=[]):
            with pytest.raises(SessionExpiredError):
                await prepare_inbox_sync(client, user_id=1)


@pytest.mark.asyncio
async def test_persist_inbox_sync_stale_timestamp_on_failed_detail():
    """When detail fetch returned body=None, body_fetched_at is set to a past timestamp for lazy refetch."""
    from app.workers.sync_worker import persist_inbox_sync
    from app.config import config

    created_message = MagicMock()
    created_message.id = 101
    created_message.portal_message_id = 999
    created_message.body_fetched_at = None

    scraped = [{
        "portal_message_id": 999,
        "token": "token_xyz",
        "sender": "Prof X",
        "subject": "Test Notice",
        "sent_on": datetime.now(timezone.utc),
    }]
    detail_cache = {999: {"body": None, "attachment_url": None}}
    existing_by_id = {}

    with patch("app.workers.sync_worker.get_db_session") as mock_db_ctx, \
         patch("app.workers.sync_worker.wait_for_db_recovery", return_value=None):
        session = AsyncMock()
        mock_db_ctx.return_value.__aenter__.return_value = session

        begin_ctx = MagicMock()
        begin_ctx.__aenter__ = AsyncMock(return_value=None)
        begin_ctx.__aexit__ = AsyncMock(return_value=None)
        session.begin = MagicMock(return_value=begin_ctx)

        with patch("app.db.repositories.inbox_repository.InboxRepository.get_by_portal_message_ids", return_value=[]), \
             patch("app.db.repositories.inbox_repository.InboxRepository.create_message", return_value=created_message), \
             patch("app.db.repositories.event_repository.EventRepository.has_message_event", new_callable=AsyncMock, return_value=False), \
             patch("app.db.repositories.event_repository.EventRepository.create_event", return_value=None):
            
            await persist_inbox_sync(1, scraped, detail_cache, existing_by_id, baseline=False)

    assert created_message.body_fetched_at is not None
    # Verify the timestamp is at least config.INBOX_BODY_TTL_SECONDS in the past
    now = datetime.now(timezone.utc)
    diff = (now - created_message.body_fetched_at).total_seconds()
    assert diff >= config.INBOX_BODY_TTL_SECONDS, f"Expected stale timestamp, got diff={diff}s"


@pytest.mark.asyncio
async def test_prepare_inbox_sync_caps_detail_fetches_to_newest_15(monkeypatch):
    """Only the newest INBOX_SYNC_DETAIL_LIMIT missing messages are detail-fetched
    during a sync; older ones are returned header-only for lazy fetch on open."""
    from app.workers.sync_worker import prepare_inbox_sync
    from app.config import config

    limit = config.INBOX_SYNC_DETAIL_LIMIT
    total = limit + 5  # more missing messages than the cap

    now = datetime.now(timezone.utc)
    scraped = [
        {
            "portal_message_id": 1000 + i,
            "token": f"tok_{i}",
            "sender": f"Prof {i}",
            "subject": f"Notice {i}",
            "sent_on": now - timedelta(hours=i),  # i=0 is the newest
        }
        for i in range(total)
    ]

    def fake_parse_list(html):
        return scraped

    def fake_parse_detail(html):
        return {"body": "body text", "attachment_url": None}

    monkeypatch.setattr("app.nitris.parser.parse_messages_list_html", fake_parse_list)
    monkeypatch.setattr("app.nitris.parser.parse_message_detail_html", fake_parse_detail)

    fetched_tokens: list[str] = []
    client = AsyncMock()
    client.fetch_messages_list.return_value = "<html>list</html>"

    async def mock_fetch_detail(token):
        fetched_tokens.append(token)
        return "<html>detail</html>"

    client.fetch_message_detail.side_effect = mock_fetch_detail

    with patch("app.workers.sync_worker.get_db_session") as mock_db_ctx:
        session = AsyncMock()
        mock_db_ctx.return_value.__aenter__.return_value = session
        with patch(
            "app.db.repositories.inbox_repository.InboxRepository.get_by_portal_message_ids",
            return_value=[],
        ):
            scraped_out, details, existing = await prepare_inbox_sync(client, user_id=1)

    # ALL scraped messages are returned so persist_inbox_sync can create a row
    # for every one (header-only for the uncapped tail).
    assert len(scraped_out) == total
    # ONLY the newest N were detail-fetched.
    assert len(details) == limit
    assert len(fetched_tokens) == limit
    assert set(fetched_tokens) == {f"tok_{i}" for i in range(limit)}
