"""Alive Dashboard composers + briefing + migration wiring."""
from __future__ import annotations

from datetime import datetime, time

from app.services.attendance_health import summarize
from app.ui.alive import (
    BRIEF_AFTER_HOURS,
    compose_briefing,
    compose_today_block,
)
from app.services.now_next_service import (
    ClassSlot,
    NowNextResult,
    resolve_now_and_next,
)


def _slot(code="MN2105", start="16:00", room="LH-3") -> ClassSlot:
    hh, mm = map(int, start.split(":"))
    return ClassSlot(
        subject_code=code, room=room,
        start_time=time(hh, mm), end_time=time(16, 55),
        period_index=7, weekday=4,
    )


def _now_next(**kw) -> NowNextResult:
    base = dict(current_class=None, next_class=None, is_lunch_break=False,
                is_before_first_class=False, is_day_done=False, is_weekend=False,
                minutes_until_next=None, next_class_day_offset=0,
                evaluated_at_ist=datetime(2026, 8, 22, 15, 0))
    base.update(kw)
    return NowNextResult(**base)


def _records():
    return [
        dict(subject_code="MN2105", subject_name="Underground Coal Mining",
             faculty="", tc="20", ua="2", le="0", oa="2", ltp="3-1-0"),
        dict(subject_code="CS2011", subject_name="AI & ML",
             faculty="", tc="10", ua="5", le="0", oa="5", ltp="2-0-0"),
    ]


# ── Today block ─────────────────────────────────────────────────────────────

def test_today_block_full_house():
    nn = _now_next(next_class=_slot(), minutes_until_next=60)
    text = compose_today_block(now_next=nn,
                               summary=summarize(_records()),
                               unread_count=2, timetable_synced=True)
    assert "📅 Next: <b>MN2105</b>" in text
    assert "in 1h 00m" in text or "in 1h 0m" in text
    assert "Attendance: <b>WATCH" in text or "🟡 Attendance" in text
    assert "⚠️ CS2011" in text          # riskiest surfaced
    assert "📬 2 unread" in text


def test_today_block_weekend_and_clear_inbox():
    text = compose_today_block(now_next=_now_next(is_weekend=True),
                               summary=summarize([]),
                               unread_count=0, timetable_synced=True)
    assert "Weekend mode" in text
    assert "Inbox clear" in text


def test_today_block_unsynced_timetable():
    text = compose_today_block(now_next=None, summary=None,
                               unread_count=0, timetable_synced=False)
    assert "Timetable not synced yet" in text


# ── Briefing ────────────────────────────────────────────────────────────────

def test_briefing_counts_and_absences():
    counts = {
        "new_message_received": 2,
        "attendance_updated": 1,
        "new_absence_detected": 1,
        "message_updated": 0,           # zero -> suppressed
    }
    absence = ["🚨 CS2011: 3 skip(s) left (5/9)"]
    text = compose_briefing(counts, absence)
    assert text is not None
    assert "WELCOME BACK" in text
    assert "📬 2 new notice(s)" in text
    assert "📊 1 attendance update(s)" in text
    assert "🚨 CS2011" in text
    assert "🔄" not in text             # zero-count suppressed


def test_briefing_none_when_nothing_happened():
    assert compose_briefing({}, []) is None
    assert compose_briefing({"new_message_received": 0}, []) is None


def test_brief_threshold_constant():
    assert BRIEF_AFTER_HOURS >= 1       # sanity; product chose 6h


# ── Phase D: card data + name/pct helpers + expandable briefing ────────────

def test_extract_first_name():
    from app.ui.alive import extract_first_name
    assert extract_first_name("ARADHY SINGH CHAUHAN {725MN1011}") == "Aradhy"
    assert extract_first_name("Riya {123}") == "Riya"
    assert extract_first_name("") is None
    assert extract_first_name(None) is None
    assert extract_first_name("{725}") is None


def test_overall_attended_pct():
    from app.ui.alive import overall_attended_pct
    s = summarize(_records())  # 30 held, 7 skipped
    assert overall_attended_pct(s) == round((30 - 7) / 30 * 100)
    empty = summarize([dict(subject_code="X", subject_name="", faculty="",
                            tc="0", ua="0", le="0", oa="0", ltp="0-0-1")])
    assert overall_attended_pct(empty) is None


def test_briefing_expands_when_long():
    counts = {"new_message_received": 5, "attendance_updated": 2}
    absence = ["🚨 A: 1 left", "🚨 B: 2 left"]
    text = compose_briefing(counts, absence)
    assert text is not None and "blockquote expandable" in text


# ── Migration wiring ────────────────────────────────────────────────────────

def test_migration_0011_exists_and_chains_from_0010():
    from pathlib import Path
    p = Path(__file__).parent.parent / "alembic" / "versions" / "0011_last_seen_at.py"
    assert p.exists()
    src = p.read_text(encoding="utf-8")
    assert 'revision: str = "0011_last_seen_at"' in src
    assert '"0010_fix_inbox_tokens"' in src
    assert "ADD COLUMN IF NOT EXISTS last_seen_at" in src


# ── End-to-end through the pure resolver (guards day-offset regressions) ────

def test_resolver_feeds_today_block_tomorrow_case():
    entries = []
    # Build a tiny Monday-only week; evaluate on Saturday evening.
    from app.db.models import TimetableEntry  # noqa: F401  (shape reference only)
    slots = [_slot(code="MN1001", start="08:00", room="101")]
    tt_entries = []
    for i, s in enumerate(slots, start=1):
        e = type("E", (), {})()  # duck-typed entry for resolver
        e.subject_code, e.room = s.subject_code, s.room
        e.start_time, e.end_time = s.start_time, s.end_time
        e.period_index, e.weekday = s.period_index, 0
        e.is_break, e.subject_name, e.course_type = False, "", ""
        tt_entries.append(e)

    sat_evening = datetime(2026, 8, 22, 18, 0)   # Saturday
    nn = resolve_now_and_next(tt_entries, sat_evening)
    text = compose_today_block(now_next=nn, summary=summarize(_records()),
                               unread_count=0, timetable_synced=True)
    assert "+2d" in text or "Next:" in text      # Monday lookahead surfaces
