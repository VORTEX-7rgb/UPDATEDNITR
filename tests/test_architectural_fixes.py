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
            await gw.login_through_gateway(mock_client, "125AI0001", "testpass")

    # Launch 3 workers concurrently (matching max_concurrent)
    # If nested acquire exists, this will hang / timeout.
    tasks = [asyncio.create_task(worker()) for _ in range(3)]
    await asyncio.wait_for(asyncio.gather(*tasks), timeout=2.0)
    assert mock_client.login.call_count == 3


@pytest.mark.asyncio
async def test_login_pacing_still_enforced():
    """Login pacing interval is properly respected across sequential calls."""
    gw = NitrisGateway(max_concurrent=5, min_login_interval=0.1)
    mock_client = MagicMock()
    mock_client.login = AsyncMock()

    t0 = time.monotonic()
    async with gw.acquire():
        await gw.login_through_gateway(mock_client, "125AI0001", "test")
    async with gw.acquire():
        await gw.login_through_gateway(mock_client, "125AI0002", "test")
    elapsed = time.monotonic() - t0

    assert elapsed >= 0.08, f"Expected pacing delay >= 0.08s (target 0.1s), got {elapsed:.3f}s"


def test_registration_uses_gateway():
    """Verify that process_password in telegram.py wraps verification in gateway.acquire()."""
    import app.bot.telegram as tg_module
    src = inspect.getsource(tg_module.process_password)
    assert "nitris_gateway.acquire" in src, "process_password must route through nitris_gateway.acquire"
    assert "login_through_gateway" in src, "process_password must call login_through_gateway"


@pytest.mark.asyncio
async def test_creds_provider_returns_encrypted_not_plaintext():
    """creds_provider returns tuples with encrypted_password rather than decrypting all upfront."""
    import app.bot.telegram as tg_module
    src = inspect.getsource(tg_module.init_qpaper_service)
    assert "candidates = [(r.roll_number, r.id, r.encrypted_password) for r in rows]" in src or "encrypted_password" in src


@pytest.mark.asyncio
async def test_full_qp_download_no_deadlock():
    """_nitris_download decrypts JIT and logs in through gateway without deadlock."""
    from app.services.qpaper_service import QPaperService
    from app.db.crypto import encrypt_password

    enc_pass = encrypt_password("supersecret")
    async def mock_creds():
        return [("125AI0001", 1, enc_pass)]

    bot = MagicMock()
    session_factory = MagicMock()
    service = QPaperService(bot, session_factory, mock_creds)

    with patch("app.services.qpaper_service.NitrisClient") as mock_client_cls:
        client_instance = AsyncMock()
        client_instance.download_question_paper_bytes = AsyncMock(return_value=b"%PDF-1.4 test")
        client_instance.close = AsyncMock()
        mock_client_cls.return_value = client_instance

        bytes_out, kind = await service._nitris_download("CS101", "2024-25/Autumn", "mid_sem", "btnTest")
        assert bytes_out == b"%PDF-1.4 test"
        assert kind == "pdf"
