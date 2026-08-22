"""Regression tests for the H1 / H2 / H3 hardening fixes.

H1 — Portal outages must NEVER quarantine users:
     * infrastructure failures during login raise LoginUnavailableError,
       NOT LoginError;
     * the gateway quarantines ONLY on confirmed credential rejection
       (LoginError) and routes LoginUnavailableError into the circuit breaker;
     * no handler's LoginUnavailableError arm calls on_login_failure.

H3 — Negative paper caches are PERMANENT (self-heal removed by design):
     * once NITRIS has no paper for a subject/year, that is remembered
       forever — zero re-check traffic against the portal (professors never
       retroactively upload papers);
     * no TTL stamping, no heal-on-touch, no stale-refetch gate;
     * manual recovery from a wrong negative: /admin_reset_qp.
"""
import inspect
import re

import pytest

from app.nitris.exceptions import (
    LoginError,
    LoginUnavailableError,
    NitrisError,
)


# ── H1: client.login classification ─────────────────────────────────────────


def _make_client_with_mocks(monkeypatch):
    """NitrisClient whose httpx layer is replaced by AsyncMocks."""
    from unittest.mock import AsyncMock

    from app.nitris.client import NitrisClient

    c = NitrisClient()
    c.client.get = AsyncMock()
    c.client.post = AsyncMock()
    return c


def _resp(json_payload=None, status=200):
    from unittest.mock import MagicMock

    r = MagicMock()
    r.status_code = status
    r.raise_for_status = lambda: (None if status == 200 else (_ for _ in ()).throw(
        __import__("httpx").HTTPStatusError("err", request=None, response=None)
    ))
    if json_payload is not None:
        r.json = lambda: json_payload
    else:
        r.json = MagicMock(side_effect=ValueError("not json"))
    return r


@pytest.mark.asyncio
async def test_h1_transport_failure_raises_login_unavailable_and_retries(monkeypatch):
    """Network failure at the session-init step → LoginUnavailableError after
    the full retry budget (previously: instant permanent-quarantine LoginError)."""
    import httpx

    c = _make_client_with_mocks(monkeypatch)
    c.client.get.side_effect = httpx.ConnectError("portal unreachable")

    # Collapse backoff sleeps so the test stays fast.
    async def _nosleep(_):
        pass

    monkeypatch.setattr("asyncio.sleep", _nosleep)

    with pytest.raises(LoginUnavailableError):
        await c.login("125AI0001", "pw")

    # All 3 attempts were made before giving up.
    assert c.client.get.await_count == 3


@pytest.mark.asyncio
async def test_h1_transform_failure_raises_login_unavailable(monkeypatch):
    """A misbehaving password-transform endpoint is a portal fault, not bad
    credentials."""
    import httpx

    c = _make_client_with_mocks(monkeypatch)

    async def _nosleep(_):
        pass

    monkeypatch.setattr("asyncio.sleep", _nosleep)

    c.client.get.return_value = _resp({})          # step 0 OK
    c.client.post.side_effect = httpx.ReadTimeout("transform endpoint hung")

    with pytest.raises(LoginUnavailableError):
        await c.login("125AI0001", "pw")
    assert c.client.post.await_count == 3  # retried, never classified as bad creds


@pytest.mark.asyncio
async def test_h1_explicit_rejection_still_raises_login_error(monkeypatch):
    """The portal RESPONDING with a rejection remains the one true
    bad-credentials signal → LoginError, fast-fail (no retries)."""
    c = _make_client_with_mocks(monkeypatch)

    c.client.get.return_value = _resp({})                       # step 0 OK
    c.client.post.return_value = _resp({"d": "INVALID PASSWORD"})  # step 2 rejects

    with pytest.raises(LoginError):
        await c.login("125AI0001", "wrong-pw")

    # Exactly one full attempt (transform POST + auth POST) — confirmed
    # rejections must not be retried.
    assert c.client.post.await_count == 2


@pytest.mark.asyncio
async def test_h1_empty_transform_is_portal_fault_not_bad_credentials(monkeypatch):
    """Empty transformed password = server oddity → LoginUnavailableError."""
    c = _make_client_with_mocks(monkeypatch)

    async def _nosleep(_):
        pass

    monkeypatch.setattr("asyncio.sleep", _nosleep)

    c.client.get.return_value = _resp({})
    c.client.post.return_value = _resp({"d": ""})

    with pytest.raises(LoginUnavailableError):
        await c.login("125AI0001", "pw")


# ── H1: gateway routing ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_h1_gateway_does_not_quarantine_on_login_unavailable():
    """login_through_gateway must treat LoginUnavailableError as a portal
    fault: propagate it WITHOUT adding the user to the quarantine guard."""
    from unittest.mock import AsyncMock, MagicMock

    from app.nitris.gateway import NitrisGateway

    gw = NitrisGateway(max_concurrent=4, min_login_interval=0.0)

    client = MagicMock()
    client.login = AsyncMock(side_effect=LoginUnavailableError("portal down"))

    with pytest.raises(LoginUnavailableError):
        async with gw.acquire():
            await gw.login_through_gateway(client, "roll", "pw", user_id=42)

    assert 42 not in gw._quarantined


@pytest.mark.asyncio
async def test_h1_gateway_still_quarantines_on_confirmed_login_error():
    """Confirmed rejection (LoginError) still quarantines — unchanged contract."""
    from unittest.mock import AsyncMock, MagicMock

    from app.nitris.gateway import NitrisGateway

    gw = NitrisGateway(max_concurrent=4, min_login_interval=0.0)

    client = MagicMock()
    client.login = AsyncMock(side_effect=LoginError("Invalid credentials."))

    with pytest.raises(LoginError):
        async with gw.acquire():
            await gw.login_through_gateway(client, "roll", "pw", user_id=42)

    assert 42 in gw._quarantined


@pytest.mark.asyncio
async def test_h1_login_unavailable_feeds_the_circuit_breaker():
    """Sustained login-phase outages must open the circuit (the pre-H1 blind
    spot): LoginUnavailableError counts toward the breaker like other
    portal faults."""
    from unittest.mock import AsyncMock, MagicMock

    from app.nitris.gateway import CircuitState, NitrisGateway

    gw = NitrisGateway(
        max_concurrent=4,
        min_login_interval=0.0,
        circuit_error_threshold=3,
        circuit_recovery_seconds=60,
    )

    client = MagicMock()
    client.login = AsyncMock(side_effect=LoginUnavailableError("portal down"))

    for _ in range(3):
        with pytest.raises(LoginUnavailableError):
            async with gw.acquire():
                await gw.login_through_gateway(client, "roll", "pw", user_id=1)

    assert gw.circuit_state == CircuitState.OPEN
    # ...and while OPEN, requests fail fast WITHOUT attempting a login.
    logins_before = client.login.call_count
    from app.nitris.gateway import NitrisCircuitOpenError

    with pytest.raises(NitrisCircuitOpenError):
        async with gw.acquire():
            await gw.login_through_gateway(client, "roll", "pw", user_id=1)
    assert client.login.call_count == logins_before


def test_h1_no_handler_quarantines_inside_login_unavailable_arms():
    """Source-level guardrail: every LoginUnavailableError except-arm across
    the job/scheduler handlers must be free of on_login_failure calls."""
    import app.nitris.job_handlers as jh
    import app.services.scheduler_service as ss

    sources = []
    for mod in (jh, ss):
        for name, fn in vars(mod).items():
            if callable(fn) and getattr(fn, "__module__", "") == mod.__name__ \
                    and name.startswith("handle_"):
                try:
                    sources.append(inspect.getsource(fn))
                except (OSError, TypeError):
                    pass

    # The two scheduler handlers are closures inside init_scheduler().
    init_src = inspect.getsource(ss.init_scheduler)
    sources.append(init_src)

    for src in sources:
        for arm in re.findall(
            r"except LoginUnavailableError.*?(?=\n\s*except |\Z)", src, re.S
        ):
            assert "on_login_failure" not in arm, (
                "LoginUnavailableError arm must never quarantine:\n" + arm
            )


def test_h1_login_unavailable_is_not_a_client_fault_for_the_circuit():
    """Classification sanity: _record_error must treat LoginUnavailableError
    as a PORTAL fault (counts toward consecutive errors)."""
    from app.nitris.gateway import NitrisGateway

    gw = NitrisGateway(max_concurrent=2, min_login_interval=0.0)
    assert isinstance(LoginUnavailableError("x"), NitrisError)
    assert not isinstance(LoginUnavailableError("x"), LoginError)


# ── H3: negative caches are PERMANENT (self-heal feature removed by design) ──
#
# Decision: professors never retroactively upload papers, so once NITRIS has
# no paper for a subject/year, that fact is remembered FOREVER and the portal
# is never re-queried for it. Manual recovery: /admin_reset_qp.


def test_h3_persist_negatives_are_permanent_no_ttl_stamping():
    """Source-level guardrail: persist_subject_metadata must create negative
    rows with NO expiry stamping whatsoever — no TTL helpers, no
    not_available_until assignment on creation. (The two remaining
    ``not_available_until = None`` writes are the pre-existing revival paths
    that CLEAR the negative when the portal starts showing targets.)"""
    from app.services.examination_service import ExaminationService

    src = inspect.getsource(ExaminationService.persist_subject_metadata)
    assert "_negative_ttl_from_now" not in src, (
        "TTL stamping helper must be gone — negatives are permanent"
    )
    assert "_heal_negative_ttl" not in src, (
        "heal-on-touch must be gone — negatives are permanent"
    )
    # Still creates exactly three negative rows (both-missing case + mid + end).
    # ("status=" with no space matches the 3 creation kwargs, not the 2
    # pre-existing revival comparisons "existing.status == ...")
    assert src.count("status=QPStatus.PAPER_NOT_AVAILABLE.value") == 3


def test_h3_heal_helpers_removed_from_module():
    """The TTL/heal helpers must no longer exist anywhere in the module."""
    import app.services.examination_service as es

    assert not hasattr(es, "_negative_ttl_from_now")
    assert not hasattr(es, "_heal_negative_ttl")


def test_h3_year_flow_trusts_existing_negatives():
    """Source-level guardrail: handle_year_selected must use the plain
    both-missing gate — cached negative rows are trusted forever, never
    re-queried based on expiry."""
    from app.bot.handlers.papers import handle_year_selected

    src = inspect.getsource(handle_year_selected)
    assert "if not mid_cache and not end_cache:" in src, (
        "year flow must re-fetch metadata ONLY when no cache rows exist"
    )
    assert "_negative_stale" not in src, (
        "stale-TTL refetch gate must be gone — negatives are permanent"
    )


def test_h3_deliver_treats_negatives_as_terminal():
    """Source-level guardrail: deliver() answers paper_not_available instantly
    with zero NITRIS calls and contains NO TTL-expiry reset path."""
    from app.services.qpaper_service import QPaperService

    src = inspect.getsource(QPaperService.deliver)
    assert "NEGATIVE_CACHE_TTL_SECONDS" not in src
    assert "SET status = :retryable" not in src, (
        "the expired-TTL reset-to-retryable block must be gone"
    )
