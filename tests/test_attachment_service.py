"""Unit and concurrency test suite for AttachmentService."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any, List
import pytest
from unittest.mock import AsyncMock, patch

from aiogram.exceptions import TelegramRetryAfter, TelegramBadRequest, TelegramForbiddenError
from app.db.models import AttachmentCache, AttachmentStatus
from app.services.attachment_service import (
    AttachmentService, AttachmentResult,
)
from app.utils import normalize_attachment_path, attachment_basename


class FakeRecord:
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None

    def first(self):
        return self._rows[0] if self._rows else None

    def all(self):
        return self._rows

    @property
    def rowcount(self):
        return len(self._rows)


class FakeSession:
    """In-memory DB session that emulates SQL queries for AttachmentService."""

    def __init__(self, store: Dict[int, Dict[str, Any]], lock: asyncio.Lock):
        self.store = store
        self.lock = lock

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    def begin(self):
        return self

    def add(self, obj):
        if not hasattr(obj, "id") or obj.id is None:
            new_id = max(self.store.keys(), default=0) + 1
            obj.id = new_id
            self.store[new_id] = {
                "id": new_id,
                "attachment_path": obj.attachment_path,
                "status": obj.status,
                "attempt_count": obj.attempt_count,
                "telegram_file_id": None,
                "file_kind": None,
                "acquired_at": None,
                "acquired_by": None,
                "lease_expires_at": None,
                "error_message": None,
            }

    async def flush(self):
        pass

    async def execute(self, stmt, params=None):
        sql = str(stmt)
        merged = {}
        if hasattr(stmt, "compile"):
            try:
                for k, v in stmt.compile().params.items():
                    if v is not None:
                        merged[k] = v
            except Exception:
                pass
        if params:
            merged.update(params)

        async with self.lock:
            # 0. INSERT ... ON CONFLICT (attachment_path) DO NOTHING RETURNING id
            if "INSERT INTO attachment_caches" in sql and "ON CONFLICT" in sql:
                path = merged.get("path")
                # Check uniqueness
                for cid, row in self.store.items():
                    if row.get("attachment_path") == path:
                        # Conflict -> DO NOTHING
                        return _FakeResult([])
                new_id = max(self.store.keys(), default=0) + 1
                self.store[new_id] = {
                    "id": new_id,
                    "attachment_path": path,
                    "status": merged.get("status", AttachmentStatus.RETRYABLE_FAILURE.value),
                    "attempt_count": 0,
                    "telegram_file_id": None,
                    "file_kind": None,
                    "acquired_at": None,
                    "acquired_by": None,
                    "lease_expires_at": None,
                    "error_message": None,
                }
                return _FakeResult([new_id])

            # 1. CAS claim
            if "UPDATE attachment_caches" in sql and "SET status = 'fetch_in_progress'" in sql:
                cid = merged.get("cache_id")
                row = self.store.get(cid)
                if not row:
                    return _FakeResult([])
                cur_status = row.get("status")
                acq_at = row.get("acquired_at")
                stale_secs = int(merged.get("stale_secs") or 300)
                now = datetime.now(timezone.utc)
                is_stale = (
                    cur_status == AttachmentStatus.FETCH_IN_PROGRESS.value
                    and (acq_at is None or (now - acq_at).total_seconds() > stale_secs)
                )
                if cur_status == AttachmentStatus.RETRYABLE_FAILURE.value or is_stale:
                    row["status"] = AttachmentStatus.FETCH_IN_PROGRESS.value
                    row["acquired_by"] = merged.get("job_id")
                    row["acquired_at"] = now
                    row["attempt_count"] = row.get("attempt_count", 0) + 1
                    return _FakeResult([cid])
                return _FakeResult([])

            # 2. Mark available
            if "UPDATE attachment_caches" in sql and "SET status = 'available'" in sql:
                cid = merged.get("cache_id")
                row = self.store.get(cid)
                if row and row.get("status") == AttachmentStatus.FETCH_IN_PROGRESS.value:
                    row["status"] = AttachmentStatus.AVAILABLE.value
                    row["telegram_file_id"] = merged.get("file_id")
                    row["content_hash"] = merged.get("hash")
                    row["portal_filename"] = merged.get("filename")
                    row["file_kind"] = merged.get("kind")
                    row["file_size_bytes"] = merged.get("size")
                    return _FakeResult([cid])
                return _FakeResult([])

            # 3. Mark failure
            if "UPDATE attachment_caches" in sql and "SET status = :status" in sql:
                cid = merged.get("cache_id")
                row = self.store.get(cid)
                if row and row.get("status") == AttachmentStatus.FETCH_IN_PROGRESS.value:
                    row["status"] = merged.get("status")
                    row["error_message"] = merged.get("err")
                    return _FakeResult([cid])
                return _FakeResult([])

            # 4. Stale-lock reaper
            if "UPDATE attachment_caches" in sql and "[stale-lock-reaped]" in sql:
                stale_secs = int(merged.get("stale_secs") or 300)
                now = datetime.now(timezone.utc)
                reaped = []
                for cid, row in self.store.items():
                    if row.get("status") == AttachmentStatus.FETCH_IN_PROGRESS.value:
                        acq_at = row.get("acquired_at")
                        if acq_at and (now - acq_at).total_seconds() > stale_secs * 2:
                            row["status"] = AttachmentStatus.RETRYABLE_FAILURE.value
                            row["acquired_by"] = None
                            reaped.append(cid)
                return _FakeResult(reaped)

            # 5. Reset invalid file_id
            if "UPDATE attachment_caches" in sql and "telegram_file_id = NULL" in sql:
                cid = merged.get("cache_id")
                row = self.store.get(cid)
                if row:
                    row["status"] = AttachmentStatus.RETRYABLE_FAILURE.value
                    row["telegram_file_id"] = None
                    return _FakeResult([cid])
                return _FakeResult([])

            # 6. SELECT by path
            if "attachment_caches.attachment_path =" in sql:
                path = merged.get("attachment_path_1") or merged.get("attachment_path") or merged.get("path")
                for cid, row in self.store.items():
                    if path and row.get("attachment_path") == path:
                        return _FakeResult([cid])
                return _FakeResult([])

            # 7. SELECT status, file_id, file_kind
            if "SELECT attachment_caches.status" in sql:
                cid = merged.get("id_1") or merged.get("id")
                if cid and cid in self.store:
                    r = self.store[cid]
                    return _FakeResult([(r["status"], r.get("telegram_file_id"), r.get("file_kind"))])
                return _FakeResult([])

            return _FakeResult([])


class FakeBot:
    def __init__(self):
        self.sent_docs = []
        self.send_doc_hook = None

    async def send_document(self, chat_id: int, document: Any, caption: str = ""):
        if self.send_doc_hook:
            return await self.send_doc_hook(chat_id, document, caption)
        doc_obj = FakeRecord(file_id=f"tg_file_{len(self.sent_docs) + 1}")
        msg = FakeRecord(document=doc_obj)
        self.sent_docs.append({"chat_id": chat_id, "document": document, "caption": caption})
        return msg


@pytest.mark.asyncio
async def test_path_normalization():
    """Test URL normalization for global dedup."""
    assert normalize_attachment_path("../../docs/ReachYourStudent/notice.pdf?tk=123") == "/docs/ReachYourStudent/notice.pdf"
    assert normalize_attachment_path("https://nitris.nitrkl.ac.in/docs/foo.pdf") == "/docs/foo.pdf"
    assert attachment_basename("/docs/foo.pdf") == "foo.pdf"


@pytest.mark.asyncio
async def test_attachment_cache_hit():
    """Cached attachment delivers instantly without acquiring."""
    store = {
        1: {
            "id": 1,
            "attachment_path": "/docs/notice.pdf",
            "status": AttachmentStatus.AVAILABLE.value,
            "telegram_file_id": "file_12345",
            "file_kind": "pdf",
            "attempt_count": 1,
        }
    }
    lock = asyncio.Lock()
    bot = FakeBot()
    svc = AttachmentService(
        bot=bot,
        session_factory=lambda: FakeSession(store, lock),
        storage_chat_id=123,
    )

    res = await svc.deliver(
        attachment_url="/docs/notice.pdf",
        telegram_id=999,
        source_user_id=1,
        source_roll_number="125AI0001",
        encrypted_password="enc",
        subject="Notice Title",
    )

    assert res.delivered is True
    assert res.file_kind == "pdf"
    assert len(bot.sent_docs) == 1
    assert bot.sent_docs[0]["chat_id"] == 999
    assert bot.sent_docs[0]["document"] == "file_12345"


@pytest.mark.asyncio
async def test_attachment_first_acquisition():
    """First acquisition downloads from NITRIS and stores in cache."""
    store = {}
    lock = asyncio.Lock()
    bot = FakeBot()
    svc = AttachmentService(
        bot=bot,
        session_factory=lambda: FakeSession(store, lock),
        storage_chat_id=123,
    )

    with patch.object(svc, "_nitris_download", new_callable=AsyncMock) as mock_dl:
        mock_dl.return_value = (b"%PDF-1.4 test bytes", "notice.pdf", "pdf")

        res = await svc.deliver(
            attachment_url="/docs/notice.pdf",
            telegram_id=999,
            source_user_id=1,
            source_roll_number="125AI0001",
            encrypted_password="enc",
            subject="Test Notice",
        )

        assert res.delivered is True
        assert res.file_kind == "pdf"
        mock_dl.assert_called_once()
        assert len(bot.sent_docs) == 2


@pytest.mark.asyncio
async def test_attachment_concurrent_collapse():
    """Concurrent requests for the same attachment collapse into a single acquisition."""
    store = {}
    lock = asyncio.Lock()
    bot = FakeBot()
    svc = AttachmentService(
        bot=bot,
        session_factory=lambda: FakeSession(store, lock),
        storage_chat_id=123,
        wait_poll_interval=0.02,
    )

    download_count = 0

    async def fake_download(*args, **kwargs):
        nonlocal download_count
        download_count += 1
        await asyncio.sleep(0.08)  # simulate slow download
        return (b"%PDF-1.4 sample content", "notice.pdf", "pdf")

    with patch.object(svc, "_nitris_download", side_effect=fake_download):
        tasks = [
            svc.deliver(
                attachment_url="/docs/notice.pdf",
                telegram_id=1000 + i,
                source_user_id=1,
                source_roll_number="125AI0001",
                encrypted_password="enc",
                subject="Test Notice",
            )
            for i in range(5)
        ]
        results = await asyncio.gather(*tasks)

        assert download_count == 1
        for res in results:
            assert res.delivered is True


@pytest.mark.asyncio
async def test_attachment_concurrent_cold_start_no_race():
    """8 parallel cold-start delivers on a brand new path result in 1 row, 1 download, and 0 exceptions."""
    store = {}
    lock = asyncio.Lock()
    bot = FakeBot()
    svc = AttachmentService(
        bot=bot,
        session_factory=lambda: FakeSession(store, lock),
        storage_chat_id=123,
        wait_poll_interval=0.01,
    )

    download_count = 0

    async def fake_download(*args, **kwargs):
        nonlocal download_count
        download_count += 1
        await asyncio.sleep(0.05)
        return (b"%PDF-1.4 brand new content", "cold_start.pdf", "pdf")

    with patch.object(svc, "_nitris_download", side_effect=fake_download):
        tasks = [
            svc.deliver(
                attachment_url="/docs/brand_new_cold_start.pdf",
                telegram_id=5000 + i,
                source_user_id=i + 1,
                source_roll_number=f"125AI{i:04d}",
                encrypted_password="enc",
                subject="Cold Start Notice",
            )
            for i in range(8)
        ]
        results = await asyncio.gather(*tasks)

        assert download_count == 1
        assert len(store) == 1
        assert len(results) == 8
        for res in results:
            assert res.delivered is True


@pytest.mark.asyncio
async def test_attachment_stale_reaper():
    """Reaper resets stuck fetch_in_progress rows."""
    stale_time = datetime.now(timezone.utc) - timedelta(seconds=700)
    store = {
        1: {
            "id": 1,
            "attachment_path": "/docs/stuck.pdf",
            "status": AttachmentStatus.FETCH_IN_PROGRESS.value,
            "acquired_at": stale_time,
            "attempt_count": 1,
        }
    }
    lock = asyncio.Lock()
    bot = FakeBot()
    svc = AttachmentService(
        bot=bot,
        session_factory=lambda: FakeSession(store, lock),
        stale_seconds=300,
    )

    await svc._reap_stale_locks()
    assert store[1]["status"] == AttachmentStatus.RETRYABLE_FAILURE.value
