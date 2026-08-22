"""Visual language: separators, blockquote cards, emoji law, standard footers.

Button law (never violated):
    row 1..n : primary / contextual actions
    last row : [← Back] (optional)  [🏠 Home]
"""
from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

# ── Brand + icon law ────────────────────────────────────────────────────────
BRAND = "🦀 NITRCLAW"
ICON_ATT = "📊"
ICON_TT = "📅"
ICON_INBOX = "📬"
ICON_PAPERS = "📝"
ICON_HOME = "🏠"

HR = "━━━━━━━━━━━━━━━━━━"


def progress_bar(pct: int, width: int = 10) -> str:
    """▰▰▰▰▱▱▱ filled/hollow budget bar for text surfaces."""
    p = max(0, min(100, int(pct)))
    filled = round(p / 100 * width)
    return "▰" * filled + "▱" * (width - filled)

# Generic Home target — reuses the existing dashboard-rendering callback so no
# new routing is introduced in this phase.
HOME_CB = "inbox_back_dashboard"


def quote(inner: str) -> str:
    """Wrap content in Telegram's native card look (<blockquote>).

    Degrades gracefully on old clients to a quoted/plain block. Flip
    USE_BLOCKQUOTE to False if the live render check ever fails.
    """
    if USE_BLOCKQUOTE:
        return f"<blockquote>{inner}</blockquote>"
    return inner


USE_BLOCKQUOTE = True


def btn(text: str, callback_data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=callback_data)


def home_button() -> InlineKeyboardButton:
    return btn(f"{ICON_HOME} Home", HOME_CB)


def footer_kb(back_cb: str | None = None, back_text: str = "← Back") -> InlineKeyboardMarkup:
    """Standard last-row navigation. Back optional, Home mandatory."""
    b = InlineKeyboardBuilder()
    row = []
    if back_cb:
        row.append(btn(back_text, back_cb))
    row.append(home_button())
    b.row(*row)
    return b.as_markup()


def add_footer(builder: InlineKeyboardBuilder, back_cb: str | None = None,
               back_text: str = "← Back") -> InlineKeyboardBuilder:
    """Append the standard nav row(s) onto an existing keyboard builder."""
    row = []
    if back_cb:
        row.append(btn(back_text, back_cb))
    row.append(home_button())
    builder.row(*row)
    return builder


def refresh_home_kb(refresh_cb: str, refresh_text: str = "🔄 Refresh") -> InlineKeyboardMarkup:
    """Common screen footer: primary refresh action + Home."""
    b = InlineKeyboardBuilder()
    b.row(btn(refresh_text, refresh_cb))
    b.row(home_button())
    return b.as_markup()
