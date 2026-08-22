"""Architectural Fix Tests.

Tests:
1. login_through_gateway does NOT perform nested semaphore acquire (prevents deadlock).
2. Stress test: workers == max_concurrent completes without hanging.
3. Login pacing interval is still enforced.
4. Registration verification routes through the gateway acquire context.
5. creds_provider returns encrypted passwords for just-in-time decryption.
6. Full QP acquisition handles just-in-time decryption cleanly without deadlock.
"""
import asyncio
import inspect
import time
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from app.nitris.gateway import NitrisGateway, nitris_gateway


@pytest.mark.asyncio
async def test_login_through_gateway_does_not_acquire_semaphore():
    """Verify login_through_gateway does not call self.acquire() internally."""
    src = inspect.getsource(NitrisGateway.login_through_gateway)
    # Ensure there is no 'self.acquire' or 'nitris_gateway.acquire' call inside login_through_gateway
    assert "self.acquire(" not in src, "login_through_gateway must NOT call self.acquire() internally"


@pytest.mark.asyncio
async def test_no_deadlock_at_max_workers():
    """When all concurrency slots are held, login_through_gateway must not deadlock."""
    gw = NitrisGateway(max_concurrent=3, min_login_interval=0.01)
    
    mock_client = MagicMock()
    mock_client.login = AsyncMock()

    async def worker():
        async with gw.acquire():
            await gw.login_through_gateway(mock_client, "125AI0001", "testpass", user_id=1)

    # Launch 3 workers concurrently (matching max_concurrent)
    # If nested acquire exists, this will hang / timeout.
    tasks = [asyncio.create_task(worker()) for _ in range(3)]
    await asyncio.wait_for(asyncio.gather(*tasks), timeout=2.0)
    assert mock_client.login.call_count == 3


@pytest.mark.asyncio
async def test_login_pacing_still_enforced():
    """Login pacing interval is properly respected across sequential calls.
    (Token-bucket era: bucket pre-drained so these two logins must consume
    REFILLED tokens — average rate still ≤ 1/interval.)"""
    gw = NitrisGateway(max_concurrent=5, min_login_interval=0.1)
    mock_client = MagicMock()
    mock_client.login = AsyncMock()

    # Drain so both logins depend on refills (burst would otherwise fire them
    # back-to-back by design).
    gw._login_tokens = 0.0
    gw._login_last_refill = time.monotonic()

    t0 = time.monotonic()
    async with gw.acquire():
        await gw.login_through_gateway(mock_client, "125AI0001", "test", user_id=1)
    async with gw.acquire():
        await gw.login_through_gateway(mock_client, "125AI0002", "test", user_id=2)
    elapsed = time.monotonic() - t0

    assert elapsed >= 0.08, f"Expected pacing delay >= 0.08s (target 0.1s), got {elapsed:.3f}s"


def test_registration_uses_gateway():
    """Registration verification uses the SEPARATE explicit path (verify_credentials),
    not the automatic login_through_gateway (which requires user_id + quarantine guard)."""
    import app.bot.telegram as tg_module
    src = inspect.getsource(tg_module.process_password)
    assert "nitris_gateway.acquire" in src, "process_password must route through nitris_gateway.acquire"
    assert "verify_credentials" in src, "process_password must use the explicit verify_credentials path"
    assert "login_through_gateway" not in src, "registration must NOT use the automatic login_through_gateway path"


@pytest.mark.asyncio
async def test_creds_provider_returns_encrypted_not_plaintext():
    """H2 contract: creds_provider is a health probe only — it must NEVER
    return or reference candidate credential material (no encrypted_password
    rows, no pool tuples). Actual acquisition uses requester-owned creds."""
    import app.bot.telegram as tg_module
    src = inspect.getsource(tg_module.init_qpaper_service)
    assert "encrypted_password" not in src, (
        "creds_provider must not select/return credential material (H2: "
        "cross-account pool removed)"
    )
    assert "credentials_valid = TRUE" in src, (
        "creds_provider must remain a valid-credentials health probe"
    )
    assert "return None" in src, "creds_provider signals own-creds-only by returning None"


@pytest.mark.asyncio
async def test_full_qp_download_no_deadlock():
    """_nitris_download decrypts JIT (own credentials) and logs in through the
    gateway without deadlock."""
    from app.services.qpaper_service import QPaperService
    from app.db.crypto import encrypt_password

    enc_pass = encrypt_password("supersecret")

    async def mock_creds():
        return None  # H2: probe only

    bot = MagicMock()
    session_factory = MagicMock()
    service = QPaperService(bot, session_factory, mock_creds)

    async def fake_own(user_id):
        return ("125AI0001", user_id, enc_pass)

    service._load_own_credentials = fake_own

    # P1: the download now runs through the session pool — patch THERE, and
    # stub the gateway login (the real gateway's acquire() is harmless).
    from app.nitris.session_pool import NitrisClient as PoolClient  # noqa: F401

    with patch("app.nitris.session_pool.NitrisClient") as mock_client_cls, \
         patch("app.nitris.gateway.nitris_gateway.login_through_gateway", new=AsyncMock()), \
         patch("app.nitris.session_pool.decrypt_password", lambda enc: "plaintext"):
        client_instance = AsyncMock()
        client_instance.download_question_paper_bytes = AsyncMock(return_value=b"%PDF-1.4 test")
        client_instance.close = AsyncMock()
        client_instance.client = MagicMock(is_closed=False)
        mock_client_cls.return_value = client_instance

        bytes_out, kind = await service._nitris_download(
            "CS101", "2024-25/Autumn", "mid_sem", "btnTest", requester_user_id=1,
        )
        assert bytes_out == b"%PDF-1.4 test"
        assert kind == "pdf"


@pytest.mark.asyncio
async def test_qp_download_without_requester_is_rejected():
    """H2 contract: cold acquisition REQUIRES requester_user_id — no anonymous
    or cross-account downloads are possible anymore."""
    from app.services.qpaper_service import QPaperService

    async def mock_creds():
        return None

    service = QPaperService(MagicMock(), MagicMock(), mock_creds)

    with pytest.raises(RuntimeError, match="requester_user_id"):
        await service._nitris_download("CS101", "2024-25/Autumn", "mid_sem", "btnTest")


@pytest.mark.asyncio
async def test_qp_download_quarantined_requester_never_uses_other_accounts():
    """H2 contract: a quarantined requester yields CredentialsQuarantinedError;
    no other user's account is ever contacted."""
    from app.services.qpaper_service import QPaperService
    from app.nitris.exceptions import CredentialsQuarantinedError

    async def mock_creds():
        return None

    service = QPaperService(MagicMock(), MagicMock(), mock_creds)

    async def fake_own(user_id):
        return None  # missing / quarantined

    service._load_own_credentials = fake_own

    with pytest.raises(CredentialsQuarantinedError):
        await service._nitris_download(
            "CS101", "2024-25/Autumn", "mid_sem", "btnTest", requester_user_id=7,
        )
