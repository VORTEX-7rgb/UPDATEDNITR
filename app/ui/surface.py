"""Telegram message lifecycle — THE single place bubbles are born or edited.

Golden rule of Phase A:  EDIT WHAT YOU TAPPED.
A button press edits the message whose button was pressed. A fresh send happens
only as a fallback (message deleted / can't be edited / command entry points).

Surface class additionally provides progressive latency UX (their §6/§7):
cached content immediately, optional "slow NITRIS" pokes after 2s+, final
render that atomically invalidates any pending poke (stale-write protection).
"""
from __future__ import annotations

import asyncio
import logging

from aiogram import types
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest

logger = logging.getLogger(__name__)


async def show(
    message: types.Message,
    text: str,
    reply_markup: types.InlineKeyboardMarkup | None = None,
    parse_mode: str = ParseMode.HTML,
) -> types.Message | None:
    """Edit the given bot message; fall back to a fresh send when impossible.

    Swallows the benign "message is not modified" error (double-taps on Refresh)
    so handlers never crash on idempotent renders.
    """
    # Inaccessible messages (very old callback targets) have no edit_text.
    if not hasattr(message, "edit_text"):
        return await _send_fresh(message, text, reply_markup, parse_mode)

    try:
        return await message.edit_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
    except TelegramBadRequest as e:
        lowered = str(e).lower()
        if "message is not modified" in lowered:
            return message  # idempotent re-render — perfectly fine
        logger.debug("Edit fell back to send (%s)", e)
    except Exception as e:  # deleted message, network hiccup, etc.
        logger.debug("Edit failed, falling back to send: %r", e)

    return await _send_fresh(message, text, reply_markup, parse_mode)


async def _send_fresh(message, text, reply_markup, parse_mode):
    """Last resort: put the screen into a brand-new bubble in the same chat."""
    try:
        answer = getattr(message, "answer", None)
        if callable(answer):
            return await answer(text, reply_markup=reply_markup, parse_mode=parse_mode)
        bot = getattr(message, "bot", None)
        chat_id = getattr(getattr(message, "chat", None), "id", None)
        if bot is not None and chat_id is not None:
            return await bot.send_message(
                chat_id=chat_id, text=text, reply_markup=reply_markup, parse_mode=parse_mode
            )
    except Exception as e:
        logger.warning("Fresh send also failed: %r", e)
    return None


class Surface:
    """Owns exactly ONE bubble through a whole interaction.

    Usage pattern inside handlers:

        surf = Surface(await show(callback.message, initial_text, kb))
        surf.poke_later(2.5, copy.SLOW_NITRIS)
        result = await asyncio.wait_for(future, timeout=120)
        ...
        await surf.final(final_text, final_kb)

    Any pending poke is invalidated by `final()` / a newer `edit()`, so a slow
    "NITRIS is slow…" note can never overwrite a finished render.
    """

    def __init__(self, message: types.Message):
        self.message = message
        self._gen = 0
        self._pokes: set[asyncio.Task] = set()

    async def edit(self, text: str, reply_markup=None) -> types.Message | None:
        self._gen += 1
        msg = await show(self.message, text, reply_markup)
        if msg is not None:
            self.message = msg
        return msg

    def poke_later(self, delay: float, text: str, reply_markup=None) -> asyncio.Task:
        """After `delay`, replace the bubble with `text` UNLESS a newer render
        already happened. Bounded to one pending poke per call."""
        gen = self._gen

        async def _poke() -> None:
            try:
                await asyncio.sleep(delay)
                if gen == self._gen:
                    await self.edit(text, reply_markup)
            except asyncio.CancelledError:
                return

        task = asyncio.create_task(_poke())
        self._pokes.add(task)
        task.add_done_callback(self._pokes.discard)
        return task

    async def final(self, text: str, reply_markup=None) -> types.Message | None:
        """Terminal render — cancels every pending poke first."""
        self._gen += 1
        for t in list(self._pokes):
            t.cancel()
        self._pokes.clear()
        return await self.edit(text, reply_markup)
