"""Timetable sync cooldown = 4 hours (was 60s).

Timetables change ~never mid-semester (the scheduler TTL is already 7d), so
the manual "⚡ Sync from NITRIS" cooldown was raised from 60s to 14400s.
Guards pin the default, the humanized wait message, and the wiring.
"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("ENCRYPTION_KEY", "ZmDfcTF7_60GrrY167zsiPd67pEvs0aGOv2oasOM1Pg=")
os.environ.setdefault("BOT_TOKEN", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost:5432/test")

from app.config import config
from app.bot.handlers.timetable import _fmt_wait, COOLDOWN_TIMETABLE_SYNC

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_default_cooldown_is_four_hours():
    """Code default must be 4h (14400s). The deployed .env does not pin this
    key, so the default is what production actually runs."""
    assert COOLDOWN_TIMETABLE_SYNC == config.COOLDOWN_TIMETABLE_SYNC
    assert config.COOLDOWN_TIMETABLE_SYNC == 4 * 3600


def test_env_example_documents_the_new_value():
    src = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    assert "COOLDOWN_TIMETABLE_SYNC=14400" in src


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (1, "1s"),
        (45, "45s"),
        (89, "89s"),          # sub-90 stays seconds
        (90, "1m"),           # ≥90 floors to whole minutes
        (3599, "59m"),
        (3600, "1h 00m"),
        (7205, "2h 00m"),
        (14399, "3h 59m"),
        (14400, "4h 00m"),
    ],
)
def test_fmt_wait_humanizes(seconds, expected):
    assert _fmt_wait(seconds) == expected


def test_handler_uses_the_formatter_not_raw_seconds():
    """The locked-out message must render '3h 59m', never '14399s'."""
    src = (REPO_ROOT / "app/bot/handlers/timetable.py").read_text(encoding="utf-8")
    assert "_fmt_wait(wait)" in src
    assert "{wait}s" not in src
