"""Per-user NITRIS credential quarantine — single authority for the auth gate.

HARD INVARIANT
==============
One confirmed ``LoginError`` for user X immediately sets
``users.credentials_valid = FALSE`` for X, immediately notifies X on Telegram,
and NO automatic login attempt may occur for X until X explicitly re-registers
(via /forgot / Update Credentials).

Every automatic login path must:
  1. (pre-check) obtain credentials via :func:`load_user_credentials`, which
     raises :class:`CredentialsQuarantinedError` for a quarantined user, and
  2. call ``nitris_gateway.login_through_gateway(..., user_id=...)`` which
     refuses quarantined users in O(1) as a defense-in-depth backstop.

The ONLY exception is registration/re-registration, which uses
``nitris_gateway.verify_credentials(...)`` — an explicit, user-initiated path
with no user_id and no quarantine check.

Notification is fire-and-forget from here (outside any DB transaction / gateway
lock) so it can never block or corrupt the login flow.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import User
from app.nitris.exceptions import CredentialsQuarantinedError
from app.nitris.gateway import nitris_gateway, CREDENTIAL_COOLDOWN_SECONDS

logger = logging.getLogger(__name__)

# Set on startup by init_auth_gate(bot). Used to deliver the quarantine notice.
_bot = None

QUARANTINE_NOTICE = (
    "❌ <b>NITRIS credentials invalid</b>\n\n"
    "Your NITRIS Roll Number / password was rejected by the portal.\n\n"
    "To protect your account, NitrClaw has stopped all further automatic "
    "login attempts.\n\n"
    "Please use 🔄 <b>Update Credentials</b> or <b>/forgot</b> to register "
    "again with your current NITRIS credentials."
)


def init_auth_gate(bot) -> None:
    """Register the bot instance used for quarantine notifications. Call once on startup."""
    global _bot
    _bot = bot


@dataclass
class UserCreds:
    """Credentials + identity loaded through the auth gate."""
    user_id: int
    roll_number: str
    encrypted_password: str
    telegram_id: int


async def load_user_credentials(
    session_factory: async_sessionmaker[AsyncSession], user_id: int
) -> UserCreds:
    """Load a user's credentials, refusing quarantined users.

    Raises:
        CredentialsQuarantinedError: if the user does not exist or has
            ``credentials_valid = FALSE``. Callers must translate this into a
            friendly "/forgot" message and NEVER attempt a login.
    """
    async with session_factory() as session:
        user = await session.get(User, user_id)
        if user is None:
            raise CredentialsQuarantinedError(f"User {user_id} not found")
        if not user.credentials_valid:
            raise CredentialsQuarantinedError(
                f"Credentials quarantined for user {user_id}"
            )
        return UserCreds(
            user_id=user.id,
            roll_number=user.roll_number,
            encrypted_password=user.encrypted_password,
            telegram_id=user.telegram_id,
        )


async def on_login_failure(user_id: int, error_msg: str) -> None:
    """One confirmed LoginError → permanent quarantine + immediate notification.

    Uses an atomic ``UPDATE ... WHERE credentials_valid = TRUE RETURNING telegram_id``
    so that:
      * the FIRST failure flips valid → invalid (no 3-strike threshold), and
      * concurrent failures (e.g. inbox + attendance syncing simultaneously)
        produce exactly ONE notification (rowcount dedupe).
    """
    telegram_id: Optional[int] = None
    try:
        from app.db.database import async_session_factory
        async with async_session_factory() as session:
            async with session.begin():
                result = await session.execute(
                    text("""
                        UPDATE users
                        SET credentials_valid = FALSE,
                            credentials_invalid_at = NOW(),
                            qp_fail_count = qp_fail_count + 1,
                            qp_cooldown_until = NOW() + make_interval(secs => :cooldown),
                            updated_at = NOW()
                        WHERE id = :user_id
                          AND credentials_valid = TRUE
                        RETURNING telegram_id
                    """),
                    {"user_id": user_id, "cooldown": CREDENTIAL_COOLDOWN_SECONDS},
                )
                row = result.first()
                if row is None:
                    # Already quarantined — do not send a duplicate notification.
                    return
                telegram_id = row[0]
    except Exception as e:
        logger.error("on_login_failure DB update failed for user_id=%d: %r", user_id, e)
        return

    # Defense-in-depth: seed the gateway's in-memory guard.
    nitris_gateway.quarantine(user_id)
    logger.warning("Credentials quarantined for user_id=%d: %s", user_id, (error_msg or "")[:120])

    if telegram_id and _bot is not None:
        try:
            await _bot.send_message(chat_id=telegram_id, text=QUARANTINE_NOTICE)
        except Exception as e:
            logger.warning(
                "Failed to notify quarantined user %d (telegram_id=%s): %r",
                user_id, telegram_id, e,
            )


async def on_credentials_updated(user_id: int) -> None:
    """Re-enable logins after a successful explicit re-registration.

    Bumps ``credentials_version`` (one fresh attempt per credential version),
    resets the failure counters, and clears the gateway in-memory guard.
    """
    try:
        from app.db.database import async_session_factory
        async with async_session_factory() as session:
            async with session.begin():
                await session.execute(
                    text("""
                        UPDATE users
                        SET credentials_valid = TRUE,
                            credentials_version = credentials_version + 1,
                            credentials_invalid_at = NULL,
                            qp_fail_count = 0,
                            qp_cooldown_until = NULL,
                            updated_at = NOW()
                        WHERE id = :user_id
                    """),
                    {"user_id": user_id},
                )
    except Exception as e:
        logger.error("on_credentials_updated DB update failed for user_id=%d: %r", user_id, e)
        return

    nitris_gateway.unquarantine(user_id)
    logger.info("Credentials re-enabled for user_id=%d", user_id)


async def init_quarantine(session_factory: async_sessionmaker[AsyncSession]) -> None:
    """Seed the gateway's in-memory guard from the DB on startup.

    Restores protection across restarts without requiring a per-request DB read
    inside the gateway lock.
    """
    try:
        async with session_factory() as session:
            result = await session.execute(
                text("SELECT id FROM users WHERE credentials_valid = FALSE")
            )
            ids = [row[0] for row in result.fetchall()]
        for uid in ids:
            nitris_gateway.quarantine(uid)
        logger.info("Seeded gateway quarantine guard with %d user(s)", len(ids))
    except Exception as e:
        logger.error("init_quarantine failed: %r", e)
