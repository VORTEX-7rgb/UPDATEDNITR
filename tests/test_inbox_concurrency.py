"""Burst stress tests for inbox and attachment concurrency."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import pytest
from unittest.mock import AsyncMock, patch

from app.services.attachment_service import AttachmentService, AttachmentResult
from app.db.models import AttachmentStatus


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
    def __init__(self, store: dict, lock: asyncio.Lock):
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
                for cid, row in self.store.items():
                    if row.get("attachment_path") == path:
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

            if "UPDATE attachment_caches" in sql and "SET status = 'fetch_in_progress'" in sql:
                cid = merged.get("cache_id")
                row = self.store.get(cid)
                if not row:
                    return _FakeResult([])
                cur_status = row.get("status")
                if cur_status == AttachmentStatus.RETRYABLE_FAILURE.value:
                    row["status"] = AttachmentStatus.FETCH_IN_PROGRESS.value
                    row["acquired_by"] = merged.get("job_id")
                    row["acquired_at"] = datetime.now(timezone.utc)
                    return _FakeResult([cid])
                return _FakeResult([])

            if "UPDATE attachment_caches" in sql and "SET status = 'available'" in sql:
                cid = merged.get("cache_id")
                row = self.store.get(cid)
                if row:
                    row["status"] = AttachmentStatus.AVAILABLE.value
                    row["telegram_file_id"] = merged.get("file_id")
                    row["file_kind"] = merged.get("kind")
                    return _FakeResult([cid])
                return _FakeResult([])

            if "attachment_caches.attachment_path =" in sql:
                path = merged.get("attachment_path_1") or merged.get("attachment_path") or merged.get("path")
                for cid, row in self.store.items():
                    if path and row.get("attachment_path") == path:
                        return _FakeResult([cid])
                return _FakeResult([])

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

    async def send_document(self, chat_id: int, document: any, caption: str = ""):
        doc = FakeRecord(file_id=f"cached_fid_{len(self.sent_docs) + 1}")
        self.sent_docs.append({"chat_id": chat_id, "document": document})
        return FakeRecord(document=doc)


@pytest.mark.asyncio
async def test_burst_100_concurrent_attachment_deliveries():
    """100 concurrent students requesting the same notice attachment results in 1 download and 100 deliveries."""
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

    async def slow_download(*args, **kwargs):
        nonlocal download_count
        download_count += 1
        await asyncio.sleep(0.05)
        return (b"%PDF-1.4 sample content", "exam_schedule.pdf", "pdf")

    with patch.object(svc, "_nitris_download", side_effect=slow_download):
        tasks = [
            svc.deliver(
                attachment_url="/docs/exam_schedule.pdf",
                telegram_id=2000 + i,
                source_user_id=i,
                source_roll_number=f"125AI{i:04d}",
                encrypted_password="enc",
                subject="Exam Schedule 2026",
            )
            for i in range(100)
        ]
        results = await asyncio.gather(*tasks)

        assert download_count == 1
        assert len(results) == 100
        for res in results:
            assert res.delivered is True
            assert res.file_kind == "pdf"
