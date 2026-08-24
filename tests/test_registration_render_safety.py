"""Guard: registration completion must never attach a ReplyKeyboardMarkup to
editMessageText.

Telegram's editMessageText only accepts InlineKeyboardMarkup. Passing the
persistent 🏠 reply-keyboard bar raised a pydantic ValidationError
("1 validation error for EditMessageText") that the generic except handler
mislabeled as "A database error occurred during registration" — on EVERY
registration (incident 2026-08-24, VM logs 06:21:22 UTC).

These tests pin the fix at the AST level so the bug can never silently return.
"""
import ast
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("ENCRYPTION_KEY", "ZmDfcTF7_60GrrY167zsiPd67pEvs0aGOv2oasOM1Pg=")
os.environ.setdefault("BOT_TOKEN", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost:5432/test")

REGISTRATION_PATH = (
    Path(__file__).resolve().parents[1] / "app" / "bot" / "handlers" / "registration.py"
)


def _registration_tree() -> ast.AST:
    return ast.parse(REGISTRATION_PATH.read_text(encoding="utf-8"))


def _except_handler_types(tree: ast.AST) -> list[str]:
    """Flattened string renderings of every except-clause type in the module."""
    rendered: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler) and node.type is not None:
            rendered.append(ast.unparse(node.type))
    return rendered


def test_no_edit_text_call_carries_the_reply_start_keyboard():
    """Every status_msg.edit_text(...) in registration.py must avoid passing
    build_start_reply_keyboard() as reply_markup. (message.answer MAY use it —
    new messages accept ReplyKeyboardMarkup; edits do not.)"""
    tree = _registration_tree()
    source = REGISTRATION_PATH.read_text(encoding="utf-8")

    # The helper must still exist and be used for the /start welcome message.
    assert "build_start_reply_keyboard" in source

    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "edit_text"):
            continue
        for kw in node.keywords:
            if kw.arg == "reply_markup":
                rendered = ast.unparse(kw.value)
                if "build_start_reply_keyboard" in rendered:
                    offenders.append(rendered)

    assert not offenders, (
        "editMessageText only accepts InlineKeyboardMarkup — attaching the "
        f"reply-keyboard Start bar to an edit breaks every registration: {offenders}"
    )


def test_telegram_render_errors_are_caught_separately_from_db_errors():
    """The registration completion handler must have a dedicated
    (TelegramAPIError, ValidationError) arm BEFORE the generic Exception arm,
    so render failures are never mislabeled as database errors."""
    rendered = _except_handler_types(_registration_tree())
    telegram_aware = any("TelegramAPIError" in r for r in rendered)
    validation_aware = any("ValidationError" in r for r in rendered)

    assert telegram_aware, (
        "registration.py must catch aiogram TelegramAPIError explicitly — "
        "render failures mean the registration COMMITTED and must not be "
        "reported as a database error"
    )
    assert validation_aware, (
        "registration.py must catch pydantic ValidationError explicitly — "
        "aiogram payload validation failures (e.g. bad reply_markup on an "
        "edit) must not surface as 'database error'"
    )


def test_generic_db_error_message_still_present_as_final_fallback():
    """The generic Exception arm keeps the honest DB-error message as the
    last-resort fallback for genuine database failures."""
    source = REGISTRATION_PATH.read_text(encoding="utf-8")
    assert "A database error occurred during registration" in source
