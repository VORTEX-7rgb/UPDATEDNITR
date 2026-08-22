"""Regression tests for PERF P1 — per-user NITRIS session pool.

Contract:
  * MISS  → exactly ONE gateway login, then work runs.
  * HIT   → ZERO additional logins; same client instance reused.
  * LoginError / SessionExpiredError / CredentialsQuarantinedError inside
    work → pooled entry dropped + closed (next run re-authenticates).
  * Transient errors keep the session.
  * Concurrent jobs for the SAME user are serialized (per-user lock).
  * Sliding TTL expiry forces a fresh login.
"""
import asyncio
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.nitris import session_pool as sp


class FakeNitrisClient:
    def __init__(self):
        self.closed = False
        self.client = SimpleNamespace(is_closed=False)

    async def close(self):
        self.closed = True
        self.client.is_closed = True


@pytest.fixture(autouse=True)
def clean_pool_and_mocks(monkeypatch):
    sp._pool.clear()
    logins = {"count": 0}

    @asynccontextmanager
    async def fake_acquire():
        yield

    async def fake_login(client, roll, password, *, user_id):
        logins["count"] += 1
        client.login_calls = logins["count"]

    monkeypatch.setattr(sp.NitrisClient, "__new__", lambda cls: FakeNitrisClient())
    monkeypatch.setattr(
        "app.nitris.gateway.nitris_gateway.acquire", fake_acquire
    )
    monkeypatch.setattr(
        "app.nitris.gateway.nitris_gateway.login_through_gateway", fake_login
    )
    monkeypatch.setattr(sp, "decrypt_password", lambda enc: "plaintext")
    yield logins
    sp._pool.clear()


ENC = b"enc"


def _work_returning(value):
    async def _w(cl, pw):
        return value
    return _w


async def _run(work):
    return await sp.with_pooled_session(
        user_id=7, roll_number="725MN1011", encrypted_password=ENC, work=work,
    )


@pytest.mark.asyncio
async def test_p1_miss_logs_once_then_hits_are_free(clean_pool_and_mocks):
    logins = clean_pool_and_mocks

    seen = []

    async def track(cl, pw):
        seen.append(id(cl))
        return "ok"

    await _run(track)
    assert logins["count"] == 1

    await _run(track)
    assert logins["count"] == 1, "second run must NOT re-login"
    # Same client object reused across runs.
    assert len(set(seen)) == 1


@pytest.mark.asyncio
async def test_p1_auth_fault_drops_session_transient_keeps_it(clean_pool_and_mocks):
    from app.nitris.exceptions import LoginError

    logins = clean_pool_and_mocks

    async def boom(cl, pw):
        raise LoginError("Invalid credentials.")

    with pytest.raises(LoginError):
        await _run(boom)
    assert logins["count"] == 1
    assert 7 not in sp._pool, "auth fault must drop the pooled entry"

    # Next run authenticates fresh.
    await _run(_work_returning("ok"))
    assert logins["count"] == 2

    # A TRANSIENT failure keeps the session alive.
    async def transient(cl, pw):
        raise TimeoutError("portal hiccup")

    with pytest.raises(TimeoutError):
        await _run(transient)
    assert 7 in sp._pool
    await _run(_work_returning("ok2"))
    assert logins["count"] == 2, "transient error must not force re-login"


@pytest.mark.asyncio
async def test_p1_same_user_jobs_serialize_on_one_client(clean_pool_and_mocks):
    order = []

    async def slow_work(cl, pw):
        await asyncio.sleep(0.05)
        order.append("end")

    async def quick_work(cl, pw):
        order.append("start")

    t_slow = asyncio.create_task(_run(slow_work))
    await asyncio.sleep(0.01)                     # let slow work take the lock
    await _run(quick_work)                        # must WAIT for the lock
    await t_slow
    assert order == ["end", "start"], "same-user jobs overlapped!"


@pytest.mark.asyncio
async def test_p1_sliding_ttl_expiry_forces_fresh_login(clean_pool_and_mocks):
    logins = clean_pool_and_mocks

    await _run(_work_returning("warm"))
    assert logins["count"] == 1

    # Age the entry past its TTL.
    sp._pool[7].expires = time.monotonic() - 1

    await _run(_work_returning("fresh"))
    assert logins["count"] == 2, "expired session must re-authenticate"


@pytest.mark.asyncio
async def test_p1_drop_all_closes_clients():
    await _run(_work_returning("x"))
    entry = sp._pool[7]
    n = await sp.drop_all_sessions()
    assert n == 1
    assert entry.client.closed is True
    assert 7 not in sp._pool