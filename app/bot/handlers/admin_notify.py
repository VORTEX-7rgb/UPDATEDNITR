"""Admin notification — fire-and-forget messages to bot admins.

Currently used to notify admins when a brand-new user registers. The
notification contains ONLY the roll number — no password, no Telegram ID,
no name. Per the user's spec: "only rollnumber nothing else".

Design rules
------------
1. NEVER raise. If any send fails (admin blocked the bot, bad ID, network
   error), log + swallow. The user's registration MUST NEVER fail because
   of an admin-notification problem.

2. Fire AFTER the user's success path completes. The user has already seen
   their "✅ Registration complete!" message + dashboard by the time this
   helper is called.

3. Send to EVERY admin ID in config.ADMIN_TELEGRAM_IDS (comma-separated
   env var). Empty set = no-op, no crash.

4. Zero PII in the message beyond the roll number. The password variable
   is never in scope here (caller passes only the roll string).
"""
from __future__ import annotations

import logging
from typing import Iterable

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramBadRequest

from app.config import config
from app.utils import esc

logger = logging.getLogger(__name__)


async def notify_admins_of_new_user(bot: Bot, roll_number: str) -> int:
    """Send a one-line new-user notification to every configured admin.

    The message body contains ONLY the roll number — no other PII.

    Args:
        bot: The aiogram Bot instance (caller passes message.bot from a handler).
        roll_number: The newly-registered user's NITRIS roll number.

    Returns:
        The number of admins successfully notified (0 if none configured,
        0 if all sends failed). NEVER raises.
    """
    admin_ids = list(config.ADMIN_TELEGRAM_IDS)
    if not admin_ids:
        # No admins configured — silent no-op. Not an error.
        logger.debug("notify_admins_of_new_user: ADMIN_TELEGRAM_IDS empty — skipping")
        return 0

    # Message body — header + roll only. No password, no Telegram ID, no name.
    # HTML-escape the roll defensively.
    text = (
        "🔔 <b>New user registered</b>\n\n"
        f"Roll: <code>{esc(roll_number)}</code>"
    )

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
        "notify_admins_of_new_user: notified %d/%d admins about new user roll=%s",
        success_count, len(admin_ids), roll_number,
    )
    return success_count


def get_admin_ids() -> Iterable[int]:
    """Test helper — return the configured admin IDs as a list."""
    return list(config.ADMIN_TELEGRAM_IDS)
