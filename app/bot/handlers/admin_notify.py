"""Admin notification — fire-and-forget messages to bot admins.

Currently used to notify admins when a brand-new user registers. The
notification contains the student's NAME (scraped from their own NITRIS
profile during registration) and the roll number — no password, no
Telegram ID.

Design rules
------------
1. NEVER raise. If any send fails (admin blocked the bot, bad ID, network
   error), log + swallow. The user's registration MUST NEVER fail because
   of an admin-notification problem.

2. Fire immediately after the user row is COMMITTED — before onboarding /
   schedule side-effects — so a later hiccup can never suppress it.

3. Send to EVERY admin ID in config.ADMIN_TELEGRAM_IDS (comma-separated
   env var). Empty set = no-op, no crash.

4. The name comes from the portal profile the USER THEMSELVES synced
   (student_info on the attendance snapshot); the password is never in
   scope here.
"""
from __future__ import annotations

import logging
from typing import Iterable, Optional

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramBadRequest

from app.config import config
from app.utils import esc

logger = logging.getLogger(__name__)


def extract_student_name(student_info: Optional[str]) -> Optional[str]:
    """'ARADHY SINGH CHAUHAN {725MN1011}' -> 'Aradhy Singh Chauhan'.

    Returns None when nothing usable is present (missing / blank input).
    """
    if not student_info:
        return None
    base = str(student_info).split("{")[0].strip()
    return base.title() or None


async def notify_admins_of_new_user(
    bot: Bot,
    roll_number: str,
    student_name: Optional[str] = None,
) -> int:
    """Send a new-user notification to every configured admin.

    Message contains the student's NAME (when available from the synced
    portal profile) and the roll number — never the password or Telegram ID.

    Args:
        bot: The aiogram Bot instance (caller passes message.bot from a handler).
        roll_number: The newly-registered user's NITRIS roll number.
        student_name: Raw 'FULL NAME {roll}' string scraped at registration;
            parsed + title-cased here. Optional — omitted from the message
            when unavailable.

    Returns:
        The number of admins successfully notified (0 if none configured,
        0 if all sends failed). NEVER raises.
    """
    admin_ids = list(config.ADMIN_TELEGRAM_IDS)
    if not admin_ids:
        # No admins configured — silent no-op. Not an error.
        logger.debug("notify_admins_of_new_user: ADMIN_TELEGRAM_IDS empty — skipping")
        return 0

    name = extract_student_name(student_name)
    lines = ["🔔 <b>New user registered</b>", ""]
    if name:
        lines.append(f"👤 Name: <b>{esc(name)}</b>")
    lines.append(f"🎓 Roll: <code>{esc(roll_number)}</code>")
    text = "\n".join(lines)

    success_count = 0
    for admin_id in admin_ids:
        try:
            await bot.send_message(
                chat_id=admin_id,
                text=text,
                parse_mode="HTML",
            )
            success_count += 1
        except TelegramForbiddenError as e:
            # Admin blocked the bot OR the admin ID is a chat the bot can't message.
            logger.warning(
                "notify_admins_of_new_user: cannot message admin %d — "
                "user has blocked the bot, or invalid admin ID. Error: %r",
                admin_id, e,
            )
        except TelegramBadRequest as e:
            logger.warning(
                "notify_admins_of_new_user: Telegram rejected send to admin %d: %r",
                admin_id, e,
            )
        except Exception as e:
            # Catch-all — registration MUST NEVER fail because of notification.
            logger.warning(
                "notify_admins_of_new_user: unexpected error sending to admin %d: %r",
                admin_id, e,
            )

    logger.info(
        "notify_admins_of_new_user: notified %d/%d admins about new user roll=%s name=%s",
        success_count, len(admin_ids), roll_number, name,
    )
    return success_count


def get_admin_ids() -> Iterable[int]:
    """Test helper — return the configured admin IDs as a list."""
    return list(config.ADMIN_TELEGRAM_IDS)
