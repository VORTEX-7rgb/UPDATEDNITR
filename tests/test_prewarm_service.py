"""Pre-warm service proofs: channel-only uploads, safe failure modes."""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("ENCRYPTION_KEY", "ZmDfcTF7_60GrrY167zsiPd67pEvs0aGOv2oasOM1Pg=")
os.environ.setdefault("BOT_TOKEN", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost:5432/test")

from app.nitris.exceptions import CredentialsQuarantinedError, PaperNotAvailableError
from app.services.qpaper_service import QPaperService

REPO_ROOT = Path(__file__).resolve().parents[1]


def _svc() -> QPaperService:
    return QPaperService.__new__(QPaperService)  # skip bot/session wiring


def _snap(status="retryable_failure", postback="tl$btn", fid=None):
    return (status, fid, None, "CS101", "2025-26/Autumn", "mid_sem",
            postback, None, None)


async def test_happy_path_channel_only_no_user_chat():
    svc = _svc()
    seen_fallback = {"v": "unset"}

    async def fake_upload(file_bytes, kind, sub, yr, et,
                          fallback_chat_id=None, nav_markup=None):
        seen_fallback["v"] = fallback_chat_id
        return ("FILEID123", False)

    svc._read_cache = AsyncMock(return_value=_snap())
    svc._claim_for_acquisition = AsyncMock(return_value=True)
    svc._acquire_sem = asyncio.Semaphore(2)
    svc._nitris_download = AsyncMock(return_value=(b"%PDF-1.4 x", "pdf"))
    svc._telegram_upload = fake_upload
    svc._mark_available = AsyncMock()

    res = await svc.prewarm_one(cache_id=1, donor_user_id=42)

    assert res == "available"
    assert seen_fallback["v"] is None  # NEVER a user chat
    svc._mark_available.assert_awaited_once_with(1, "FILEID123", "pdf", len(b"%PDF-1.4 x"))


async def test_channel_down_leaves_retryable_never_user_upload():
    svc = _svc()
    marks = []

    svc._read_cache = AsyncMock(return_value=_snap())
    svc._claim_for_acquisition = AsyncMock(return_value=True)
    svc._acquire_sem = asyncio.Semaphore(2)
    svc._nitris_download = AsyncMock(return_value=(b"%PDF", "pdf"))

    async def fail_upload(*a, **k):
        raise RuntimeError("channel gone")

    async def mark_fail(cid, status, exc):
        marks.append(status)

    svc._telegram_upload = fail_upload
    svc._mark_failure = mark_fail

    res = await svc.prewarm_one(cache_id=2, donor_user_id=42)

    assert res == "channel-down"
    assert marks == ["retryable_failure"]


async def test_negative_paper_marked_permanent():
    svc = _svc()
    svc._read_cache = AsyncMock(return_value=_snap())
    svc._claim_for_acquisition = AsyncMock(return_value=True)
    svc._acquire_sem = asyncio.Semaphore(2)
    svc._nitris_download = AsyncMock(
        side_effect=PaperNotAvailableError("none uploaded"))
    svc._mark_not_available = AsyncMock()

    res = await svc.prewarm_one(cache_id=3, donor_user_id=42)

    assert res == "not_available"
    svc._mark_not_available.assert_awaited_once()


async def test_busy_row_skipped_without_download():
    svc = _svc()
    svc._read_cache = AsyncMock(return_value=_snap())
    svc._claim_for_acquisition = AsyncMock(return_value=False)
    svc._nitris_download = AsyncMock()

    res = await svc.prewarm_one(cache_id=4, donor_user_id=42)

    assert res == "busy"
    svc._nitris_download.assert_not_awaited()


async def test_already_cached_short_circuits_before_claim():
    svc = _svc()
    svc._read_cache = AsyncMock(
        return_value=_snap(status="paper_available", fid="HAVE"))
    svc._claim_for_acquisition = AsyncMock(
        side_effect=AssertionError("must not claim an available row"))

    res = await svc.prewarm_one(cache_id=5, donor_user_id=42)

    assert res == "already"


async def test_donor_quarantine_keeps_row_retryable():
    """Donor creds broken → shared row stays RETRYABLE (another donor/run can
    pick it up); never poisoned to permanent."""
    svc = _svc()
    marks = []
    svc._read_cache = AsyncMock(return_value=_snap())
    svc._claim_for_acquisition = AsyncMock(return_value=True)
    svc._acquire_sem = asyncio.Semaphore(2)
    svc._nitris_download = AsyncMock(
        side_effect=CredentialsQuarantinedError("donor locked"))

    async def mark_fail(cid, status, exc):
        marks.append(status)

    svc._mark_failure = mark_fail

    res = await svc.prewarm_one(cache_id=6, donor_user_id=42)

    assert res == "donor-creds"
    assert marks == ["retryable_failure"]
