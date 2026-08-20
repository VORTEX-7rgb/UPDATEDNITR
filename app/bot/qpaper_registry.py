"""Question-paper service singleton + lifecycle.

Kept in a dedicated module (rather than inside the bot assembly) so the papers
handler can read the current singleton at call time without importing the whole
bot wiring (which would create a circular import).
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
    from app.db.crypto import decrypt_password  # noqa: F401  (kept for parity)
    from app.db.models import User, SyncState
    from sqlalchemy import select, or_, func

    async def creds_provider():
        """Return a list of (roll, password, user_id) candidates for QP acquisition."""
        async with async_session_factory() as s:
            stmt = (
                select(User.id, User.roll_number, User.encrypted_password)
                .outerjoin(SyncState, User.id == SyncState.user_id)
                .where(User.credentials_valid == True)
                .where(
                    or_(
                        User.qp_cooldown_until.is_(None),
                        User.qp_cooldown_until < func.now()
                    )
                )
                .order_by(SyncState.last_success.desc().nulls_last(), User.id.desc())
                .limit(5)
            )
            rows = (await s.execute(stmt)).all()

            if not rows:
                logger.warning(
                    "No healthy QP credential candidates with sync history — "
                    "falling back to any user with credentials_valid=TRUE"
                )
                stmt = (
                    select(User.id, User.roll_number, User.encrypted_password)
                    .where(User.credentials_valid == True)
                    .order_by(User.id.desc())
                    .limit(5)
                )
                rows = (await s.execute(stmt)).all()

            if not rows:
                raise RuntimeError(
                    "No users with credentials_valid=TRUE — cannot acquire QP. "
                    "Register at least one student with valid credentials before "
                    "downloading papers."
                )

            candidates = [(r.roll_number, r.id, r.encrypted_password) for r in rows]

            logger.info(
                "creds_provider: returning %d candidate(s) for QP acquisition "
                "(passwords NOT decrypted — will be decrypted one-at-a-time inside gateway)",
                len(candidates),
            )
            return candidates

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
