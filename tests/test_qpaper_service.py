"""Comprehensive unit + stress test suite for QPaperService.

Uses an in-memory FakeSession and FakeBot to test all edge cases deterministically
without live network or Postgres.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any, List
import pytest

from app.db.models import QuestionPaperCache, QPStatus
from app.services.qpaper_service import (
    QPaperService, QPResult,
    _sniff_kind, _is_permanent_error, _make_caption,
    MAX_CONCURRENT_ACQUISITIONS, MAX_CONCURRENT_DELIVERIES,
)
from app.nitris.exceptions import InvalidContextError, AttendanceWorkflowError, PaperNotAvailableError


# ── Fakes ──────────────────────────────────────────────────────────


class FakeRecord:
    def __init__(self, **kw):
        self.not_available_until = None
        for k, v in kw.items():
            setattr(self, k, v)


class FakeSession:
    """In-memory DB session that emulates the SQL queries used by QPaperService."""

    def __init__(self, store: Dict[int, Dict[str, Any]], lock: asyncio.Lock):
        self.store = store
        self.lock = lock

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    def begin(self):
        return self

    async def get(self, model, id_: int):
        async with self.lock:
            row = self.store.get(id_)
            if row is None:
                return None
            return FakeRecord(**row)

    async def execute(self, stmt, params=None):
        sql = str(stmt)
        params = params or {}
        async with self.lock:
            if "UPDATE question_paper_caches" in sql and "RETURNING" in sql:
                # Compare-and-swap claim
                cid = params.get("cache_id")
                row = self.store.get(cid)
                if not row:
                    return _FakeResult([])
                cur_status = row.get("status")
                acq_at = row.get("acquired_at")
                stale_secs = int(params.get("stale_secs", 300))
                now = datetime.now(timezone.utc)
                is_stale = (
                    cur_status == QPStatus.FETCH_IN_PROGRESS.value
                    and (acq_at is None or (now - acq_at).total_seconds() > stale_secs)
                )
                if cur_status == QPStatus.RETRYABLE_FAILURE.value or is_stale:
                    row["status"] = QPStatus.FETCH_IN_PROGRESS.value
                    row["acquired_by"] = params.get("job_id")
                    row["acquired_at"] = now
                    row["last_attempt_at"] = now
                    row["attempt_count"] = row.get("attempt_count", 0) + 1
                    row["error_message"] = None
                    row["updated_at"] = now
                    return _FakeResult([(cid, row.get("subject_code"), row.get("academic_year"),
                                         row.get("exam_type"), row.get("portal_postback_target"))])
                return _FakeResult([])

            if "UPDATE question_paper_caches" in sql and "SET status = :status" in sql:
                # Mark available / mark failure / mark not available
                cid = params.get("cache_id")
                row = self.store.get(cid)
                if row:
                    row["status"] = params.get("status")
                    if "file_id" in params:
                        row["telegram_file_id"] = params["file_id"]
                        row["file_kind"] = params.get("kind")
                        row["file_size_bytes"] = params.get("size")
                        row["error_message"] = None
                    if "ttl" in params:
                        ttl = int(params["ttl"])
                        row["not_available_until"] = datetime.now(timezone.utc) + timedelta(seconds=ttl)
                    if "err" in params:
                        row["error_message"] = params["err"]
                    row["acquired_by"] = None
                    row["acquired_at"] = None
                    row["updated_at"] = datetime.now(timezone.utc)
                return _FakeResult([], rowcount=1 if row else 0)

            if "SELECT attempt_count" in sql:
                cid = params.get("id")
                row = self.store.get(cid)
                cnt = row.get("attempt_count", 0) if row else 0
                return _FakeResult([(cnt,)])

            if "UPDATE question_paper_caches" in sql and "[stale-lock-reaped]" in sql:
                # Reaper
                reaped = 0
                now = datetime.now(timezone.utc)
                stale_threshold = int(params.get("stale_secs", 300)) * 2
                for row in self.store.values():
                    if row.get("status") == QPStatus.FETCH_IN_PROGRESS.value:
                        acq_at = row.get("acquired_at")
                        if acq_at and (now - acq_at).total_seconds() > stale_threshold:
                            row["status"] = QPStatus.RETRYABLE_FAILURE.value
                            row["acquired_by"] = None
                            row["error_message"] = (row.get("error_message") or "") + " [stale-lock-reaped]"
                            row["updated_at"] = now
                            reaped += 1
                return _FakeResult([], rowcount=reaped)

            return _FakeResult([])


class _FakeResult:
    def __init__(self, rows: List, rowcount: int = 0):
        self._rows = rows
        self.rowcount = rowcount or len(rows)

    def first(self):
        return self._rows[0] if self._rows else None


class FakeDocument:
    def __init__(self, file_id: str):
        self.file_id = file_id


class FakeMessage:
    def __init__(self, file_id: str):
        self.document = FakeDocument(file_id)


class FakeBot:
    def __init__(self):
        self.sent_documents: List[Dict[str, Any]] = []
        self.upload_delay = 0.0
        self.send_delay = 0.0
        self.upload_error: Optional[Exception] = None
        self.send_error: Optional[Exception] = None
        self._counter = 0

    async def send_document(self, chat_id: int, document: Any, caption: str = "", parse_mode: str = "HTML"):
        if self.upload_error and chat_id < 0:  # storage channel
            err = self.upload_error
            self.upload_error = None
            raise err
        if self.send_error and chat_id > 0:    # user chat
            err = self.send_error
            self.send_error = None
            raise err
        delay = self.upload_delay if chat_id < 0 else self.send_delay
        if delay > 0:
            await asyncio.sleep(delay)
        self._counter += 1
        # If document is BufferedInputFile, mint a new file_id; if string, reuse
        file_id = getattr(document, "file_id", None) or f"AgACfake_file_id_{self._counter}"
        self.sent_documents.append({
            "chat_id": chat_id, "document": document,
            "caption": caption, "file_id": file_id,
        })
        return FakeMessage(file_id)


# ── Fixtures ────────────────────────────────────────────────────────


@pytest.fixture
def store():
    return {}


@pytest.fixture
def store_lock():
    return asyncio.Lock()


@pytest.fixture
def fake_bot():
    return FakeBot()


@pytest.fixture
def fake_session_factory(store, store_lock):
    def factory():
        return FakeSession(store, store_lock)
    return factory


@pytest.fixture
def qp_service(fake_bot, fake_session_factory):
    async def creds_provider():
        return "123CS0001", "password123"

    svc = QPaperService(
        bot=fake_bot,
        session_factory=fake_session_factory,
        creds_provider=creds_provider,
        storage_chat_id=-1001234567890,
    )
    return svc


# ── Tests ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cache_hit_instant_delivery(qp_service, store, fake_bot):
    """When a paper is already cached in DB, deliver() must forward the file_id
    instantly with ZERO NITRIS downloads and ZERO channel uploads."""
    store[1] = {
        "id": 1, "subject_code": "CS101", "academic_year": "2023-24/Autumn",
        "exam_type": "mid_sem", "portal_postback_target": "ctl00$target",
        "telegram_file_id": "AgACalready_cached_123",
        "status": QPStatus.PAPER_AVAILABLE.value,
        "file_kind": "pdf", "file_size_bytes": 12345,
        "acquired_by": None, "acquired_at": None, "error_message": None,
        "attempt_count": 1,
    }

    # Mock _nitris_download to assert it is NEVER called
    async def forbidden_nitris(*args):
        raise AssertionError("NITRIS was called on cache hit!")
    qp_service._nitris_download = forbidden_nitris

    res = await qp_service.deliver(cache_id=1, telegram_id=999)
    assert res.delivered is True
    assert res.file_kind == "pdf"
    assert len(fake_bot.sent_documents) == 1
    assert fake_bot.sent_documents[0]["chat_id"] == 999
    assert fake_bot.sent_documents[0]["document"] == "AgACalready_cached_123"


@pytest.mark.asyncio
async def test_first_acquisition_pdf(qp_service, store, fake_bot):
    """First request for a paper: downloads from NITRIS, uploads once to channel,
    caches file_id, and delivers to user."""
    store[2] = {
        "id": 2, "subject_code": "EC201", "academic_year": "2024-25/Spring",
        "exam_type": "end_sem", "portal_postback_target": "ctl00$ec201",
        "telegram_file_id": None,
        "status": QPStatus.RETRYABLE_FAILURE.value,
        "file_kind": None, "file_size_bytes": None,
        "acquired_by": None, "acquired_at": None, "error_message": None,
        "attempt_count": 0,
    }

    # Emulate NITRIS returning PDF bytes
    async def fake_nitris(sub, yr, ex, pb, requester_user_id=None):
        return b"%PDF-1.4 simulated pdf contents...", "pdf"
    qp_service._nitris_download = fake_nitris

    res = await qp_service.deliver(cache_id=2, telegram_id=777)
    assert res.delivered is True
    assert res.file_kind == "pdf"

    # Verify DB state
    assert store[2]["status"] == QPStatus.PAPER_AVAILABLE.value
    assert store[2]["telegram_file_id"].startswith("AgACfake_file_id_")
    assert store[2]["file_kind"] == "pdf"
    assert store[2]["file_size_bytes"] == len(b"%PDF-1.4 simulated pdf contents...")

    # Verify Telegram interactions: 1 upload to storage channel + 1 send to user
    assert len(fake_bot.sent_documents) == 2
    assert fake_bot.sent_documents[0]["chat_id"] == -1001234567890
    assert fake_bot.sent_documents[1]["chat_id"] == 777
    assert fake_bot.sent_documents[1]["document"] == store[2]["telegram_file_id"]


@pytest.mark.asyncio
async def test_first_acquisition_zip(qp_service, store, fake_bot):
    """Multi-paper / lab subjects return ZIP archives — verify ZIP sniffing & handling."""
    store[3] = {
        "id": 3, "subject_code": "CS191", "academic_year": "2023-24/Autumn",
        "exam_type": "mid_sem", "portal_postback_target": "ctl00$cs191",
        "telegram_file_id": None,
        "status": QPStatus.RETRYABLE_FAILURE.value,
        "file_kind": None, "file_size_bytes": None,
        "acquired_by": None, "acquired_at": None, "error_message": None,
        "attempt_count": 0,
    }

    zip_bytes = b"PK\x03\x04\x14\x00simulated zip contents..."
    async def fake_nitris(sub, yr, ex, pb, requester_user_id=None):
        return zip_bytes, _sniff_kind(zip_bytes)
    qp_service._nitris_download = fake_nitris

    res = await qp_service.deliver(cache_id=3, telegram_id=555)
    assert res.delivered is True
    assert res.file_kind == "zip"
    assert store[3]["file_kind"] == "zip"


@pytest.mark.asyncio
async def test_concurrent_same_paper_collapse(qp_service, store, fake_bot):
    """10 simultaneous user requests for the SAME uncached paper must collapse
    into EXACTLY 1 NITRIS download, and all 10 users must receive the paper."""
    store[4] = {
        "id": 4, "subject_code": "MA101", "academic_year": "2024-25/Autumn",
        "exam_type": "mid_sem", "portal_postback_target": "ctl00$ma101",
        "telegram_file_id": None,
        "status": QPStatus.RETRYABLE_FAILURE.value,
        "file_kind": None, "file_size_bytes": None,
        "acquired_by": None, "acquired_at": None, "error_message": None,
        "attempt_count": 0,
    }

    nitris_calls = 0
    async def counting_nitris(sub, yr, ex, pb, requester_user_id=None):
        nonlocal nitris_calls
        nitris_calls += 1
        await asyncio.sleep(0.1)  # simulate network delay
        return b"%PDF-1.4 math paper", "pdf"
    qp_service._nitris_download = counting_nitris

    # Fire 10 concurrent requests
    user_ids = [1000 + i for i in range(10)]
    results = await asyncio.gather(*(qp_service.deliver(4, uid) for uid in user_ids))

    # All 10 delivered successfully
    assert all(r.delivered for r in results)
    # EXACTLY 1 NITRIS call
    assert nitris_calls == 1
    # DB has single cached file_id
    assert store[4]["status"] == QPStatus.PAPER_AVAILABLE.value


@pytest.mark.asyncio
async def test_fallback_storage_when_chat_id_unset(fake_bot, fake_session_factory, store):
    """When QP_STORAGE_CHAT_ID is 0/unset, the upload falls back to the user's chat,
    saves the file_id, and marks delivered without a duplicate send."""
    async def creds():
        return "123CS0001", "pass"

    # storage_chat_id = 0
    svc = QPaperService(
        bot=fake_bot,
        session_factory=fake_session_factory,
        creds_provider=creds,
        storage_chat_id=0,
    )
    store[5] = {
        "id": 5, "subject_code": "PH101", "academic_year": "2023-24/Autumn",
        "exam_type": "mid_sem", "portal_postback_target": "ctl00$ph101",
        "telegram_file_id": None,
        "status": QPStatus.RETRYABLE_FAILURE.value,
        "file_kind": None, "file_size_bytes": None,
        "acquired_by": None, "acquired_at": None, "error_message": None,
        "attempt_count": 0,
    }

    async def fake_nitris(*args):
        return b"%PDF physics", "pdf"
    svc._nitris_download = fake_nitris

    res = await svc.deliver(5, telegram_id=888)
    assert res.delivered is True
    # In fallback mode, exactly 1 send occurs (to the user), not 2
    assert len(fake_bot.sent_documents) == 1
    assert fake_bot.sent_documents[0]["chat_id"] == 888
    # file_id is preserved for future requests
    assert store[5]["status"] == QPStatus.PAPER_AVAILABLE.value
    assert store[5]["telegram_file_id"] is not None


@pytest.mark.asyncio
async def test_stale_lock_recovery(qp_service, store, fake_bot):
    """If a worker process crashed 10 minutes ago leaving status='fetch_in_progress',
    the next user request must safely reclaim the lock and succeed."""
    ten_mins_ago = datetime.now(timezone.utc) - timedelta(seconds=600)
    store[6] = {
        "id": 6, "subject_code": "CY101", "academic_year": "2023-24/Spring",
        "exam_type": "end_sem", "portal_postback_target": "ctl00$cy101",
        "telegram_file_id": None,
        "status": QPStatus.FETCH_IN_PROGRESS.value,
        "acquired_by": "crashed_worker_dead_pid",
        "acquired_at": ten_mins_ago,
        "file_kind": None, "file_size_bytes": None,
        "error_message": None, "attempt_count": 1,
    }

    async def fake_nitris(*args):
        return b"%PDF chemistry", "pdf"
    qp_service._nitris_download = fake_nitris

    res = await qp_service.deliver(6, telegram_id=111)
    assert res.delivered is True
    assert store[6]["status"] == QPStatus.PAPER_AVAILABLE.value


@pytest.mark.asyncio
async def test_reaper_cleans_stuck_locks(qp_service, store):
    """Test that the stale-lock reaper reclaims expired locks into retryable_failure."""
    twenty_mins_ago = datetime.now(timezone.utc) - timedelta(seconds=1200)
    store[7] = {
        "id": 7, "subject_code": "ME101", "academic_year": "2023-24/Autumn",
        "exam_type": "mid_sem", "portal_postback_target": "ctl00$me101",
        "telegram_file_id": None,
        "status": QPStatus.FETCH_IN_PROGRESS.value,
        "acquired_by": "dead_worker",
        "acquired_at": twenty_mins_ago,
        "file_kind": None, "file_size_bytes": None,
        "error_message": None, "attempt_count": 1,
    }

    reclaimed = await qp_service._reap_stale_locks()
    assert reclaimed == 1
    assert store[7]["status"] == QPStatus.RETRYABLE_FAILURE.value
    assert "[stale-lock-reaped]" in store[7]["error_message"]


@pytest.mark.asyncio
async def test_permanent_failure_classification():
    """Verify permanent vs retryable error heuristic."""
    # 503 InvalidContextError is permanent for a postback target
    assert _is_permanent_error(InvalidContextError("NITRIS 503 Invalid Context")) is True
    # Portal explicit no paper message is permanent
    assert _is_permanent_error(AttendanceWorkflowError("Paper not available on portal")) is True
    # Transient connection timeout is retryable (not permanent)
    assert _is_permanent_error(TimeoutError("Connection timed out")) is False


@pytest.mark.asyncio
async def test_negative_cache_future_stamp_is_still_terminal(qp_service, store, fake_bot):
    """PERMANENT negatives: even a row carrying a legacy FUTURE-dated
    not_available_until stamp gets an instant no-paper answer with ZERO NITRIS
    downloads. Stamps no longer mean 're-check me later' — once NITRIS says no
    paper, that answer is final."""
    future_time = datetime.now(timezone.utc) + timedelta(hours=12)
    store[8] = {
        "id": 8, "subject_code": "CS201", "academic_year": "2024-25/Autumn",
        "exam_type": "mid_sem", "portal_postback_target": "ctl00$cs201",
        "telegram_file_id": None,
        "status": QPStatus.PAPER_NOT_AVAILABLE.value,
        "not_available_until": future_time,
        "file_kind": None, "file_size_bytes": None,
        "acquired_by": None, "acquired_at": None, "error_message": None,
        "attempt_count": 1,
    }

    async def forbidden_nitris(*args):
        raise AssertionError("NITRIS must never be queried for a negative-cached paper!")
    qp_service._nitris_download = forbidden_nitris

    res = await qp_service.deliver(cache_id=8, telegram_id=999)
    assert res.not_available is True
    assert res.delivered is False
    # Row completely untouched — still negative, still its original stamp,
    # no claim/acquisition ever started.
    assert store[8]["status"] == QPStatus.PAPER_NOT_AVAILABLE.value
    assert store[8]["attempt_count"] == 1
    assert len(fake_bot.sent_documents) == 0


@pytest.mark.asyncio
async def test_permanent_negative_cache_no_rescrape(qp_service, store, fake_bot):
    """When a paper has status='paper_not_available' and not_available_until is None (e.g. lab subjects),
    deliver() must return not_available=True with ZERO NITRIS downloads."""
    store[9] = {
        "id": 9, "subject_code": "CS291", "academic_year": "2024-25/Autumn",
        "exam_type": "mid_sem", "portal_postback_target": "",
        "telegram_file_id": None,
        "status": QPStatus.PAPER_NOT_AVAILABLE.value,
        "not_available_until": None,
        "file_kind": None, "file_size_bytes": None,
        "acquired_by": None, "acquired_at": None, "error_message": None,
        "attempt_count": 0,
    }

    async def forbidden_nitris(*args):
        raise AssertionError("NITRIS was called for permanent negative cache!")
    qp_service._nitris_download = forbidden_nitris

    res = await qp_service.deliver(cache_id=9, telegram_id=999)
    assert res.not_available is True
    assert res.delivered is False


@pytest.mark.asyncio
async def test_negative_cache_expired_stamp_never_rechecked(qp_service, store, fake_bot):
    """PERMANENT negatives: an EXPIRED not_available_until stamp must NEVER
    trigger a re-check. Once NITRIS says no paper exists, that answer is
    final — professors do not retroactively upload papers.
    Manual recovery from a wrong negative: /admin_reset_qp."""
    past_time = datetime.now(timezone.utc) - timedelta(hours=48)
    store[10] = {
        "id": 10, "subject_code": "EC301", "academic_year": "2024-25/Autumn",
        "exam_type": "end_sem", "portal_postback_target": "ctl00$ec301",
        "telegram_file_id": None,
        "status": QPStatus.PAPER_NOT_AVAILABLE.value,
        "not_available_until": past_time,
        "file_kind": None, "file_size_bytes": None,
        "acquired_by": None, "acquired_at": None, "error_message": None,
        "attempt_count": 1,
    }

    async def forbidden_nitris(*args):
        raise AssertionError("Expired negative stamp must NEVER be re-checked!")
    qp_service._nitris_download = forbidden_nitris

    res = await qp_service.deliver(cache_id=10, telegram_id=888)
    assert res.not_available is True
    assert res.delivered is False
    # No claim, no acquisition attempt — row byte-for-byte unchanged.
    assert store[10]["status"] == QPStatus.PAPER_NOT_AVAILABLE.value
    assert store[10]["attempt_count"] == 1
    assert len(fake_bot.sent_documents) == 0


@pytest.mark.asyncio
async def test_paper_not_available_error_creates_permanent_negative(qp_service, store, fake_bot):
    """When NITRIS download raises PaperNotAvailableError (postback returned
    form HTML), acquisition must set status='paper_not_available' PERMANENTLY:
    not_available_until cleared to NULL — the row is never re-checked."""
    store[11] = {
        "id": 11, "subject_code": "ME201", "academic_year": "2024-25/Autumn",
        "exam_type": "mid_sem", "portal_postback_target": "ctl00$me201",
        "telegram_file_id": None,
        "status": QPStatus.RETRYABLE_FAILURE.value,
        "not_available_until": None,
        "file_kind": None, "file_size_bytes": None,
        "acquired_by": None, "acquired_at": None, "error_message": None,
        "attempt_count": 0,
    }

    async def fake_nitris_unuploaded(*args):
        raise PaperNotAvailableError("Server returned form HTML instead of paper bytes. Paper has not been uploaded yet.")
    qp_service._nitris_download = fake_nitris_unuploaded

    res = await qp_service.deliver(cache_id=11, telegram_id=777)
    assert res.not_available is True
    assert res.delivered is False
    assert store[11]["status"] == QPStatus.PAPER_NOT_AVAILABLE.value
    # Permanent: NULL stamp — never a future TTL to expire.
    assert store[11]["not_available_until"] is None


# ── Own-creds-first policy (launch-hardening) ───────────────────────


@pytest.mark.asyncio
async def test_own_creds_tried_before_pool(qp_service, store, fake_bot, monkeypatch):
    """Cold acquisition must use the REQUESTER's own NITRIS credentials first;
    the shared pool is only a fallback. Guards against cross-tenant logins."""
    from contextlib import asynccontextmanager

    login_order: list[str] = []

    async def fake_provider():
        return [("POOLROLL", 42, b"pool-enc")]

    qp_service.creds_provider = fake_provider

    async def fake_own(user_id):
        assert user_id == 7
        return ("OWNROLL", 7, b"own-enc")

    qp_service._load_own_credentials = fake_own

    class FakeGateway:
        @asynccontextmanager
        async def acquire(self, is_login=False):
            yield

        async def login_through_gateway(self, client, username, password, *, user_id):
            login_order.append(username)

    class FakeNitrisClient:
        async def close(self):
            pass

        async def download_question_paper_bytes(self, academic_year, subject_query, event_target):
            return b"%PDF-1.4 paper"

    monkeypatch.setattr("app.nitris.gateway.nitris_gateway", FakeGateway())
    # P1 pool builds its client via session_pool.NitrisClient — patch there so
    # no real HTTP happens; the fake serves the paper bytes.
    monkeypatch.setattr("app.nitris.session_pool.NitrisClient", FakeNitrisClient)
    monkeypatch.setattr("app.services.qpaper_service.NitrisClient", FakeNitrisClient)
    monkeypatch.setattr(
        "app.db.crypto.decrypt_password",
        lambda enc: enc.decode() if isinstance(enc, bytes) else enc,
    )
    # P1 pool decrypts via its own imported symbol — patch there too.
    monkeypatch.setattr(
        "app.nitris.session_pool.decrypt_password",
        lambda enc: enc.decode() if isinstance(enc, bytes) else enc,
    )

    store[20] = {
        "id": 20, "subject_code": "CS2001", "academic_year": "2024-25/Autumn",
        "exam_type": "mid_sem", "portal_postback_target": "ctl00$cs2001",
        "telegram_file_id": None,
        "status": QPStatus.RETRYABLE_FAILURE.value,
        "not_available_until": None,
        "file_kind": None, "file_size_bytes": None,
        "acquired_by": None, "acquired_at": None, "error_message": None,
        "attempt_count": 0,
    }

    res = await qp_service.deliver(cache_id=20, telegram_id=555, requester_user_id=7)
    assert res.delivered is True
    # Own account used; pool candidate never needed.
    assert login_order == ["OWNROLL"]


@pytest.mark.asyncio
async def test_no_pool_fallback_when_own_login_fails(qp_service, store, fake_bot, monkeypatch):
    """H2: if the requester's own login fails (LoginError), acquisition must
    NOT fall back to other students' accounts. The user gets an error, no
    cross-account login is attempted, and the shared cache row is not poisoned
    into permanent_failure by one student's bad credentials."""
    from contextlib import asynccontextmanager

    from app.nitris.exceptions import LoginError

    login_order: list[str] = []

    async def fake_provider():
        return None  # H2: probe only

    qp_service.creds_provider = fake_provider

    async def fake_own(user_id):
        return ("OWNROLL", 7, b"own-enc")

    qp_service._load_own_credentials = fake_own

    quarantined: set[int] = set()

    class FakeGateway:
        def __init__(self):
            self._quarantined = quarantined

        @asynccontextmanager
        async def acquire(self, is_login=False):
            yield

        async def login_through_gateway(self, client, username, password, *, user_id):
            if username == "OWNROLL":
                self._quarantined.add(user_id)
                raise LoginError("Invalid credentials.")
            login_order.append(username)

    class FakeNitrisClient:
        async def close(self):
            pass

        async def download_question_paper_bytes(self, academic_year, subject_query, event_target):
            return b"%PDF-1.4 paper"

    monkeypatch.setattr("app.nitris.gateway.nitris_gateway", FakeGateway())
    monkeypatch.setattr("app.services.qpaper_service.NitrisClient", FakeNitrisClient)
    monkeypatch.setattr(
        "app.db.crypto.decrypt_password",
        lambda enc: enc.decode() if isinstance(enc, bytes) else enc,
    )
    # P1 pool decrypts via its own imported symbol — patch there too.
    monkeypatch.setattr(
        "app.nitris.session_pool.decrypt_password",
        lambda enc: enc.decode() if isinstance(enc, bytes) else enc,
    )

    # Track that on_login_failure fires for the RIGHT person (the requester).
    quarantine_calls: list[tuple[int, str]] = []

    async def fake_on_login_failure(user_id, err):
        quarantine_calls.append((user_id, err))

    monkeypatch.setattr(
        "app.nitris.auth_gate.on_login_failure", fake_on_login_failure
    )

    store[21] = {
        "id": 21, "subject_code": "CS2001", "academic_year": "2024-25/Autumn",
        "exam_type": "mid_sem", "portal_postback_target": "ctl00$cs2001b",
        "telegram_file_id": None,
        "status": QPStatus.RETRYABLE_FAILURE.value,
        "not_available_until": None,
        "file_kind": None, "file_size_bytes": None,
        "acquired_by": None, "acquired_at": None, "error_message": None,
        "attempt_count": 0,
    }

    res = await qp_service.deliver(cache_id=21, telegram_id=556, requester_user_id=7)

    # No delivery — and the pool account was NEVER contacted.
    assert res.delivered is False
    assert login_order == []
    # The failure was attributed to the REQUESTER only.
    assert quarantine_calls == [(7, "Invalid credentials.")]
    # Shared row was released for retry but NOT escalated to permanent.
    assert store[21]["status"] == QPStatus.RETRYABLE_FAILURE.value
    assert res.error

