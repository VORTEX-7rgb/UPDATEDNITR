"""Tests for Question Paper download rate limiting, cooldowns, and in-flight deduplication."""
import asyncio
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["ENCRYPTION_KEY"] = "ZmDfcTF7_60GrrY167zsiPd67pEvs0aGOv2oasOM1Pg="
os.environ["BOT_TOKEN"] = "test"
os.environ["DATABASE_URL"] = "postgresql+asyncpg://postgres:postgres@localhost:5432/test"


@pytest.mark.asyncio
async def test_paper_download_cooldown():
    """handle_paper_download must enforce cooldown between consecutive requests."""
    from app.bot.handlers.papers import handle_paper_download, _inflight_downloads
    from app.nitris.rate_limiter import operation_cooldown
    from app.services.qpaper_service import QPResult

    _inflight_downloads.clear()
    await operation_cooldown.clear(42, "qp_download")

    callback = AsyncMock()
    callback.data = "qp_dl_99"
    callback.from_user.id = 123456789
    callback.message = AsyncMock()

    user_mock = MagicMock()
    user_mock.id = 42
    user_mock.telegram_id = 123456789

    state = AsyncMock()

    mock_qp_service = AsyncMock()
    mock_qp_service._read_cache.return_value = ("paper_available", "file_abc123")
    mock_qp_service.deliver.return_value = QPResult(delivered=True, file_kind="pdf")

    with patch("app.bot.handlers.papers.qpaper_registry") as mock_reg, \
         patch("app.bot.handlers.papers.get_db_session") as mock_db_ctx, \
         patch("app.bot.handlers.papers.UserRepository") as mock_user_repo_cls:

        mock_reg.qpaper_service = mock_qp_service
        mock_session = AsyncMock()
        mock_db_ctx.return_value.__aenter__.return_value = mock_session
        mock_repo = MagicMock()
        mock_repo.get_by_telegram_id = AsyncMock(return_value=user_mock)
        mock_user_repo_cls.return_value = mock_repo

        # 1st call — should be allowed
        await handle_paper_download(callback, state)
        assert mock_qp_service.deliver.call_count == 1

        # 2nd immediate call — should be blocked by cooldown without hitting deliver
        callback.answer.reset_mock()
        await handle_paper_download(callback, state)
        assert mock_qp_service.deliver.call_count == 1
        # Should have toasted with wait time
        callback.answer.assert_called()
        answer_text = callback.answer.call_args[0][0]
        assert "wait" in answer_text.lower() or "downloads" in answer_text.lower()


@pytest.mark.asyncio
async def test_paper_download_inflight_dedup():
    """Concurrent duplicate clicks for the same paper should be dropped via in-flight set."""
    from app.bot.handlers.papers import handle_paper_download, _inflight_downloads

    _inflight_downloads.clear()
    _inflight_downloads.add((123456789, 99))  # simulate in-flight download

    callback = AsyncMock()
    callback.data = "qp_dl_99"
    callback.from_user.id = 123456789
    callback.message = AsyncMock()
    state = AsyncMock()

    with patch("app.bot.handlers.papers.qpaper_registry") as mock_reg:
        mock_qp_service = AsyncMock()
        mock_reg.qpaper_service = mock_qp_service

        await handle_paper_download(callback, state)

        # deliver should NOT have been called because in-flight blocked it
        assert mock_qp_service.deliver.call_count == 0
        callback.answer.assert_called()
        assert "already" in callback.answer.call_args[0][0].lower()

    _inflight_downloads.clear()


@pytest.mark.asyncio
async def test_batch_download_cooldown():
    """handle_qp_download_all_go must enforce batch download cooldown."""
    from app.bot.handlers.papers import handle_qp_download_all_go, _inflight_batch_downloads
    from app.nitris.rate_limiter import operation_cooldown

    _inflight_batch_downloads.clear()
    await operation_cooldown.clear(77, "qp_batch_download")

    callback = AsyncMock()
    callback.data = "qp_dlall_go_2324A_m"
    callback.from_user.id = 987654321
    callback.message = AsyncMock()
    state = AsyncMock()

    user_mock = MagicMock()
    user_mock.id = 77
    user_mock.telegram_id = 987654321

    with patch("app.bot.handlers.papers.qpaper_registry") as mock_reg, \
         patch("app.bot.handlers.papers.get_db_session") as mock_db_ctx, \
         patch("app.bot.handlers.papers.UserRepository") as mock_user_repo_cls, \
         patch("app.bot.handlers.papers._run_qp_batch_download") as mock_run_batch:

        mock_reg.qpaper_service = AsyncMock()
        mock_session = AsyncMock()
        mock_db_ctx.return_value.__aenter__.return_value = mock_session
        mock_repo = MagicMock()
        mock_repo.get_by_telegram_id = AsyncMock(return_value=user_mock)
        mock_user_repo_cls.return_value = mock_repo
        mock_run_batch.return_value = None

        # 1st call — allowed
        await handle_qp_download_all_go(callback, state)
        assert mock_run_batch.call_count == 1

        # 2nd call — cooldown blocks it
        callback.answer.reset_mock()
        await handle_qp_download_all_go(callback, state)
        assert mock_run_batch.call_count == 1
        callback.answer.assert_called()
        answer_text = callback.answer.call_args[0][0]
        assert "wait" in answer_text.lower()
