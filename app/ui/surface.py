"""Telegram message lifecycle — THE single place bubbles are born or edited.

Golden rule of Phase A:  EDIT WHAT YOU TAPPED.
A button press edits the message whose button was pressed. A fresh send happens
only as a fallback (message deleted / can't be edited / command entry points).

Surface class additionally provides progressive latency UX (their §6/§7):
cached content immediately, optional "slow NITRIS" pokes after 2s+, final
render that atomically invalidates any pending poke (stale-write protection).
Also includes cross-handler navigation race protection so slow background
renders never overwrite newer screens navigated to by the user.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from aiogram import types
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest

logger = logging.getLogger(__name__)

# Bubble ownership registry: (chat_id, message_id) -> latest active interaction sequence
_bubble_owners: dict[tuple[int, int], int] = {}
_global_interaction_seq = 0


def _get_bubble_key(message: types.Message) -> tuple[int, int] | None:
    chat_id = getattr(getattr(message, "chat", None), "id", None)
    message_id = getattr(message, "message_id", None) or id(message)
    if chat_id is not None:
        return (chat_id, message_id)
    return None


def claim_bubble(message: types.Message) -> int:
    """Register a new active interaction owner on this message bubble.
    Any prior slow in-flight flow on the same bubble will be invalidated."""
    global _global_interaction_seq
    key = _get_bubble_key(message)
    _global_interaction_seq += 1
    if key is not None:
        _bubble_owners[key] = _global_interaction_seq
        if len(_bubble_owners) > 2000:
            for k in list(_bubble_owners.keys())[:1000]:
                _bubble_owners.pop(k, None)
    return _global_interaction_seq


def is_bubble_owner(message: types.Message, token: int) -> bool:
    """Return True if this interaction token still owns the bubble."""
    key = _get_bubble_key(message)
    if key is None:
        return True
    return _bubble_owners.get(key, token) == token


def check_bubble_owner(chat_id: int | None, message_id: int | None, token: int | None) -> bool:
    """Return True if the interaction token still owns the bubble, or if token is None."""
    if chat_id is None or message_id is None or token is None:
        return True
    return _bubble_owners.get((chat_id, message_id), token) == token


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
    claim_bubble(message)

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
    "NITRIS is slow…" note can never overwrite a finished render. Cross-handler
    ownership tokens prevent a slow background scrape from overwriting a newer
    screen navigated to by the student.
    """

    def __init__(self, message: types.Message):
        self.message = message
        self._gen = 0
        self._owner_token = claim_bubble(message)
        self._pokes: set[asyncio.Task] = set()

    @property
    def owner_token(self) -> int:
        """The active interaction sequence token for this surface."""
        return self._owner_token

    async def edit(self, text: str, reply_markup=None) -> types.Message | None:
        if not is_bubble_owner(self.message, self._owner_token):
            logger.debug("Surface edit dropped: user navigated to newer interaction")
            return None
        self._gen += 1
        msg = await self._raw_show(self.message, text, reply_markup)
        if msg is not None:
            self.message = msg
        return msg

    async def _raw_show(self, message: types.Message, text: str, reply_markup=None) -> types.Message | None:
        """Internal render without re-claiming bubble ownership."""
        if not hasattr(message, "edit_text"):
            return await _send_fresh(message, text, reply_markup, ParseMode.HTML)
        try:
            return await message.edit_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
        except TelegramBadRequest as e:
            if "message is not modified" in str(e).lower():
                return message
            logger.debug("Edit fell back to send (%s)", e)
        except Exception as e:
            logger.debug("Edit failed, falling back to send: %r", e)
        return await _send_fresh(message, text, reply_markup, ParseMode.HTML)

    def poke_later(self, delay: float, text: str, reply_markup=None) -> asyncio.Task:
        """After `delay`, replace the bubble with `text` UNLESS a newer render
        already happened. Bounded to one pending poke per call."""
        gen = self._gen
        token = self._owner_token

        async def _poke() -> None:
            try:
                await asyncio.sleep(delay)
                if gen == self._gen and is_bubble_owner(self.message, token):
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
        if not is_bubble_owner(self.message, self._owner_token):
            logger.debug("Surface final dropped: user navigated away to newer interaction")
            return None
        return await self.edit(text, reply_markup)
