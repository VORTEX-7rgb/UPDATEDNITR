"""Question-paper service singleton + lifecycle.

Kept in a dedicated module (rather than inside the bot assembly) so the papers
handler can read the current singleton at call time without importing the whole
bot wiring (which would create a circular import).

H2 fix: the old ``creds_provider`` returned a POOL of other students'
encrypted credentials for cold QP acquisitions — the bot would log into
accounts belonging to users who never requested the paper, and could even
quarantine the wrong person when a pool candidate's login failed. Cold
acquisitions now use ONLY the requesting student's own credentials (see
``QPaperService._nitris_download``). This provider survives solely as a
health probe so a totally credential-less deployment fails fast with the
same clear message as before.
"""

import logging
from typing import Optional

from aiogram import Bot

from app.services.qpaper_service import QPaperService

logger = logging.getLogger(__name__)

qpaper_service: Optional[QPaperService] = None


async def init_qpaper_service(bot: Bot) -> None:
    """Initialize the singleton QPaperService on startup."""
    global qpaper_service
    from app.db.database import async_session_factory
    from sqlalchemy import text

    async def creds_provider():
        """Health probe: fail fast when NO user has valid credentials.

        Returns None on success — actual acquisition uses the requester's own
        credentials exclusively (H2 fix: no cross-account logins, ever).
        """
        async with async_session_factory() as s:
            count = (
                await s.execute(
                    text("SELECT COUNT(*) FROM users WHERE credentials_valid = TRUE")
                )
            ).scalar()

        if not count:
            raise RuntimeError(
                "No users with credentials_valid=TRUE — cannot acquire QP. "
                "Register at least one student with valid credentials before "
                "downloading papers."
            )

        logger.debug("creds_provider: %d user(s) with valid credentials", count)
        return None

    qpaper_service = QPaperService(
        bot=bot,
        session_factory=async_session_factory,
        creds_provider=creds_provider,
    )
    qpaper_service.start_reaper()


async def shutdown_qpaper_service() -> None:
    """Cleanly stop the QPaperService reaper on shutdown."""
    global qpaper_service
    if qpaper_service is not None:
        await qpaper_service.stop_reaper()
