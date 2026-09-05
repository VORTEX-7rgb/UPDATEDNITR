"""Warm-on-interaction middleware — cold logins die here.

LAYER 1 extension: whenever a KNOWN-REGISTERED user interacts with the bot
(any message or button tap), silently fire a LOW-priority session warm if
their pooled portal session has gone cold. By the time they navigate to a
module that needs the portal, login is already done — cold taps drop from
~2-4s (paced login + 4 portal round-trips) to ~1-1.5s of pure scrape.

ZERO-COST DESIGN
================
* The registry (`telegram_id -> user_id`) is populated ORGANICALLY by the
  handlers that already load the User row (start/dashboard/attendance). A
  user we have never seen this process lifetime costs NOTHING — no DB lookup
  per update, ever.
* All heavy lifting reuses session_warmer.request_session_warm: in-process
  warmth check (O(1)), 10-minute per-user throttle, LOW priority lane,
  never raises.
"""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

logger = logging.getLogger(__name__)

# telegram_id -> internal user.id. Insertion-capped (oldest evicted) — at 5k
# users this never fills, but a runaway scrape can't grow it unbounded.
_known_registered: dict[int, int] = {}
_CAP = 20_000


def note_registered_user(telegram_id: int, user_id: int) -> None:
    """Register a (telegram_id, user_id) pair from any handler that just
    loaded the User row. Cheap dict insert; call it wherever you already
    have both ids in hand."""
    if len(_known_registered) >= _CAP:
        # Evict the oldest entry (dicts preserve insertion order).
        _known_registered.pop(next(iter(_known_registered)), None)
    _known_registered[telegram_id] = user_id


def known_registered_count() -> int:
    """Diagnostics for /status."""
    return len(_known_registered)


class WarmSessionMiddleware(BaseMiddleware):
    """Outer middleware: fire-and-forget session warm for known users."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        try:
            # Skip session warming for purely static/cached features like holiday calendar
            cb = getattr(event, "callback_query", None) or (event if getattr(event, "data", None) else None)
            cb_data = getattr(cb, "data", "") or ""
            msg = getattr(event, "message", None) or (event if getattr(event, "text", None) else None)
            msg_text = getattr(msg, "text", "") or ""

            is_holiday_interaction = (
                cb_data in ("db_holidays", "holidays_refresh")
                or cb_data.startswith("holidays_")
                or msg_text.strip().lower().startswith("/holidays")
            )

            if not is_holiday_interaction:
                tg_user = data.get("event_from_user")
                user_id = _known_registered.get(getattr(tg_user, "id", None))
                if user_id is not None:
                    from app.services.session_warmer import request_session_warm
                    from app.utils import spawn_tracked
                    spawn_tracked(
                        request_session_warm(user_id),
                        name=f"sw-mw-{user_id}",
                    )
        except Exception as e:  # NEVER let warming disturb the update flow
            logger.debug("warm middleware skipped: %r", e)

        return await handler(event, data)
