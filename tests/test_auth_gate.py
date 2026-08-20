"""Tests for the per-user credential quarantine gate (auth_gate + gateway guard).

Covers the hard invariant:
  - one confirmed LoginError → permanent quarantine (no 3-strike threshold)
  - quarantined user → automatic login refused at the gateway (client.login never called)
  - registration verification uses the SEPARATE explicit path (verify_credentials)
  - exactly-once Telegram notification on the valid→invalid transition
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from app.nitris.gateway import NitrisGateway
from app.nitris.exceptions import (
    CredentialsQuarantinedError, LoginError, NitrisError,
)
from app.nitris import auth_gate


class FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def first(self):
        return self._rows[0] if self._rows else None

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows


class FakeSession:
    """Emulates on_login_failure's UPDATE ... WHERE credentials_valid = TRUE RETURNING telegram_id.

    The `invalid` flag models the DB column: the first valid→invalid transition
    returns the telegram_id (one notification); any further attempt returns no
    row (already quarantined → no duplicate notification).
    """

    def __init__(self, state):
        self.state = state

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        pass

    def begin(self):
        return self

    async def execute(self, stmt, params=None):
        sql = str(stmt)
        if "UPDATE users" in sql and "credentials_valid = TRUE" in sql:
            if not self.state["invalid"]:
                self.state["invalid"] = True
                return FakeResult([(self.state["telegram_id"],)])
            return FakeResult([])
        return FakeResult([])


def _gw() -> NitrisGateway:
    return NitrisGateway(max_concurrent=5, min_login_interval=0.01)


def test_credentials_quarantined_error_is_nitris_error():
    assert issubclass(CredentialsQuarantinedError, NitrisError)


@pytest.mark.asyncio
async def test_gateway_refuses_quarantined_user():
    gw = _gw()
    client = MagicMock()
    client.login = AsyncMock()

    gw.quarantine(42)
    with pytest.raises(CredentialsQuarantinedError):
        await gw.login_through_gateway(client, "125AI0001", "pw", user_id=42)

    # The gate refused BEFORE any login attempt.
    assert client.login.call_count == 0

    gw.unquarantine(42)
    await gw.login_through_gateway(client, "125AI0001", "pw", user_id=42)
    assert client.login.call_count == 1


@pytest.mark.asyncio
async def test_gateway_auto_quarantines_on_login_error():
    gw = _gw()
    client = MagicMock()
    client.login = AsyncMock(side_effect=LoginError("bad password"))

    with pytest.raises(LoginError):
        await gw.login_through_gateway(client, "125AI0001", "pw", user_id=7)

    assert gw.is_quarantined(7)


@pytest.mark.asyncio
async def test_verify_credentials_is_separate_explicit_path():
    gw = _gw()
    client = MagicMock()
    client.login = AsyncMock()

    # verify_credentials has no user_id and bypasses the quarantine guard.
    await gw.verify_credentials(client, "125AI0001", "pw")
    assert client.login.call_count == 1


@pytest.mark.asyncio
async def test_load_user_credentials_refuses_quarantined():
    class User:
        def __init__(self, valid):
            self.credentials_valid = valid
            self.roll_number = "125AI0001"
            self.encrypted_password = "enc"
            self.telegram_id = 1
            self.id = 9

    class FakeGetSession:
        def __init__(self, user):
            self.user = user

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        async def get(self, model, uid):
            return self.user

    sf = lambda: FakeGetSession(User(valid=False))
    with pytest.raises(CredentialsQuarantinedError):
        await auth_gate.load_user_credentials(sf, 9)


@pytest.mark.asyncio
async def test_on_login_failure_quarantines_and_notifies_exactly_once():
    state = {"invalid": False, "telegram_id": 555}
    fake_sf = lambda: FakeSession(state)

    fake_bot = MagicMock()
    fake_bot.send_message = AsyncMock()
    auth_gate.init_auth_gate(fake_bot)

    gw = _gw()
    with patch.object(auth_gate, "nitris_gateway", gw):
        with patch("app.db.database.async_session_factory", fake_sf):
            await auth_gate.on_login_failure(9, "bad creds")

    assert gw.is_quarantined(9)
    assert fake_bot.send_message.call_count == 1

    # Second confirmed failure → already quarantined → NO duplicate notification.
    with patch.object(auth_gate, "nitris_gateway", gw):
        with patch("app.db.database.async_session_factory", fake_sf):
            await auth_gate.on_login_failure(9, "bad creds again")

    assert fake_bot.send_message.call_count == 1


@pytest.mark.asyncio
async def test_on_credentials_updated_unquarantines():
    state = {"invalid": False, "telegram_id": 1}
    fake_sf = lambda: FakeSession(state)

    gw = _gw()
    gw.quarantine(9)
    with patch.object(auth_gate, "nitris_gateway", gw):
        with patch("app.db.database.async_session_factory", fake_sf):
            await auth_gate.on_credentials_updated(9)

    assert not gw.is_quarantined(9)
