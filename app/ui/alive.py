"""Alive Dashboard + Claw Briefing composers.

Presentation-only. Reads (never writes business state) inside the CALLER'S
session, plus one tiny self-contained transaction to stamp
sync_states.last_seen_at after each render.

Text assembly only — the keyboard is supplied by callers and stays untouched.
(The PNG photo-dashboard card was removed by design decision: text-only
dashboards. No PIL dependency remains anywhere in the app.)
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import func, select, text as sql_text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import IST
from app.db.models import Event, EventType, User
from app.db.repositories.timetable_repository import TimetableRepository
from app.services.attendance_health import AttendanceSummary, summarize
from app.ui import theme
from app.utils import esc

import logging

logger = logging.getLogger(__name__)

BRIEF_AFTER_HOURS = 6

_OVERALL_LABEL = {
    "safe": "SAFE", "warn": "WATCH", "risk": "AT RISK", "danger": "CRITICAL",
    "debarred": "DEBARRED SUBJECT", "unknown": "PARTIAL DATA", "no_classes": "NOT STARTED",
}


# ── Pure pieces (unit-tested) ───────────────────────────────────────────────

def compose_today_block(
    *,
    now_next=None,                 # NowNextResult | None
    summary: AttendanceSummary | None,
    unread_count: int,
    timetable_synced: bool,
    overall_pct: int | None = None,
) -> str:
    lines: list[str] = []

    # Class line
    if not timetable_synced:
        lines.append("📅 Timetable not synced yet")
    elif now_next is not None:
        if now_next.current_class is not None:
            c = now_next.current_class
            lines.append(f"🟢 Now: <b>{esc(c.subject_code)}</b>"
                         + (f" · {esc(c.room)}" if c.room else ""))
        elif now_next.next_class is not None:
            n = now_next.next_class
            mins = now_next.minutes_until_next
            eta = f" · in {mins // 60}h {mins % 60:02d}m" if mins else ""
            lines.append(f"📅 Next: <b>{esc(n.subject_code)}</b>"
                         + (f" · {esc(n.room)}" if n.room else "")
                         + f" · {n.start_time.strftime('%H:%M')}{eta}")
        elif now_next.is_weekend:
            lines.append("😌 Weekend mode — no classes")
        elif now_next.is_day_done:
            lines.append("🌙 Done for today")
        else:
            lines.append("☕ No class right now")

    # Attendance health
    if summary is not None and summary.subjects:
        overall = _OVERALL_LABEL.get(summary.level, "?")
        lines.append(f"{summary.emoji} Attendance: <b>{overall}</b>")
        r = summary.riskiest
        if r is not None and r.level in ("warn", "risk", "danger", "debarred"):
            if r.level == "debarred":
                lines.append(f"💀 {esc(r.code)}: DEBAR ZONE")
            else:
                left = r.ua_left if r.ua_left is not None else 0
                lines.append(f"⚠️ {esc(r.code)}: {max(left, 0)} skip(s) left")
    else:
        lines.append("📊 Attendance: tap Refresh to load")

    # Inbox
    if unread_count > 0:
        lines.append(f"📬 {unread_count} unread notice{'s' if unread_count != 1 else ''}")
    else:
        lines.append("📬 Inbox clear")

    if overall_pct is not None:
        lines.append(f"<code>{theme.progress_bar(overall_pct)}</code> {overall_pct}% overall")

    return theme.quote("\n".join(lines))


def compose_briefing(counts: dict[str, int], absence_lines: list[str]) -> str | None:
    """'While you were gone' block. None when there is nothing worth saying."""
    icons = {
        EventType.NEW_MESSAGE_RECEIVED.value: ("📬", "new notice(s)"),
        EventType.MESSAGE_UPDATED.value: ("🔄", "notice(s) updated"),
        EventType.ATTENDANCE_UPDATED.value: ("📊", "attendance update(s)"),
        EventType.NEW_ABSENCE_DETECTED.value: ("🚨", ""),
        EventType.NEW_SUBJECT_ADDED.value: ("📚", "new subject(s)"),
    }
    out: list[str] = []
    for ev_val, n in counts.items():
        if n <= 0 or ev_val not in icons:
            continue
        icon, label = icons[ev_val]
        if ev_val == EventType.NEW_ABSENCE_DETECTED.value:
            continue  # rendered as detailed absence_lines below
        out.append(f"{icon} {n} {label}")

    out.extend(absence_lines)

    if not out:
        return None
    q = theme.quote("\n".join(out))
    if len(out) > 3:
        # Long briefings collapse behind a tap — premium-app behavior.
        q = q.replace("<blockquote>", "<blockquote expandable>", 1)
    return ("🦀 <b>WELCOME BACK</b>\n<i>While you were gone:</i>\n" + q)


def extract_first_name(student_info: str | None) -> str | None:
    """'ARADHY SINGH CHAUHAN {725MN1011}' -> 'Aradhy'. None when unknown."""
    if not student_info:
        return None
    base = str(student_info).split("{")[0].strip()
    if not base:
        return None
    return base.split()[0].title()


def overall_attended_pct(summary: AttendanceSummary | None) -> int | None:
    """Σ(tc-ua)/Σtc across started subjects. None when nothing has begun."""
    if summary is None or not summary.subjects:
        return None
    held = sum(s.tc for s in summary.subjects if s.tc > 0)
    skipped = sum(s.ua for s in summary.subjects if s.tc > 0)
    if held <= 0:
        return None
    return max(0, min(100, round((held - skipped) / held * 100)))


# ── DB glue ─────────────────────────────────────────────────────────────────

def _status_line(ss) -> str:
    if ss and ss.failure_count and ss.last_error:
        return f"🔴 Sync issue: {esc(str(ss.last_error)[:60])}"
    if ss and ss.last_success:
        return (f"🟢 Synced {ss.last_success.astimezone(IST).strftime('%d %b %H:%M')} IST")
    return "🟢 Ready"


async def render_dashboard(session: AsyncSession, user: User, unread_count: int) -> str:
    """Build the full dashboard text. Caller supplies its open session and
    renders with get_dashboard_keyboard() exactly as before."""
    now = datetime.now(IST)

    # Safe sync-state load: NEVER rely on lazy relationship access — some call
    # sites don't selectinload, and async lazy-load explodes (MissingGreenlet).
    if "sync_state" in user.__dict__:
        ss = user.sync_state
    else:
        from app.db.models import SyncState
        res = await session.execute(
            select(SyncState).where(SyncState.user_id == user.id)
        )
        ss = res.scalar_one_or_none()

    tt_repo = TimetableRepository(session)
    entries = await tt_repo.get_user_timetable(user.id)
    tt_last = await tt_repo.get_last_synced_at(user.id)

    from app.db.repositories.snapshot_repository import SnapshotRepository
    snap = await SnapshotRepository(session).get_latest_snapshot(user.id, "attendance")
    records = (snap.snapshot_json.get("records") or []) \
        if snap and getattr(snap, "snapshot_json", None) else []
    summary = summarize(records) if records else None

    now_next = None
    if entries:
        from app.services.now_next_service import resolve_now_and_next
        now_next = resolve_now_and_next(entries, now)

    # ── Briefing (reads events since last_seen; then stamps last_seen) ──
    last_seen = ss.last_seen_at if ss else None
    briefing_text: str | None = None
    if last_seen is not None:
        age_h = (now - last_seen).total_seconds() / 3600.0
        if age_h >= BRIEF_AFTER_HOURS:
            rows = (await session.execute(
                select(Event.event_type, func.count().label("n"))
                .where(Event.user_id == user.id, Event.created_at > last_seen)
                .group_by(Event.event_type)
            )).all()
            counts = {r[0]: int(r[1]) for r in rows}

            absence_lines: list[str] = []
            if counts.get(EventType.NEW_ABSENCE_DETECTED.value):
                seen: dict[str, Optional[SubjectHealthLike]] = {}
                for rec in records:
                    seen[str(rec.get("subject_code"))] = rec
                ev_rows = (await session.execute(
                    select(Event.payload_json).where(
                        Event.user_id == user.id,
                        Event.event_type == EventType.NEW_ABSENCE_DETECTED.value,
                        Event.created_at > last_seen,
                    ).order_by(Event.created_at.desc()).limit(20)
                )).scalars().all()
                emitted: set[str] = set()
                for payload in ev_rows:
                    code = str((payload or {}).get("subject_code") or "")
                    if not code or code in emitted:
                        continue
                    emitted.add(code)
                    rec = seen.get(code)
                    if rec:
                        h_line = _absence_health_line(rec)
                        if h_line:
                            absence_lines.append(h_line)

            briefing_text = compose_briefing(counts, absence_lines)

    # Stamp last_seen (self-contained txn — never affects caller's session).
    try:
        from app.db.database import async_session_factory
        async with async_session_factory() as s2:
            async with s2.begin():
                await s2.execute(sql_text("""
                    INSERT INTO sync_states (user_id, failure_count, last_seen_at)
                    VALUES (:uid, 0, NOW())
                    ON CONFLICT (user_id)
                    DO UPDATE SET last_seen_at = NOW()
                """), {"uid": user.id})
    except Exception as e:  # stamping must never break rendering
        import logging
        logging.getLogger(__name__).warning("last_seen stamp failed: %r", e)

    # ── Assemble ────────────────────────────────────────────────────────
    parts: list[str] = [f"{theme.BRAND}"]
    if briefing_text:
        parts.append(briefing_text)

    pct = overall_attended_pct(summary)
    today = compose_today_block(
        now_next=now_next,
        summary=summary,
        unread_count=unread_count,
        timetable_synced=bool(entries),
        overall_pct=pct,
    )
    parts.append(today)

    footer_bits = [f"👤 <code>{esc(user.roll_number)}</code>"]
    st = _status_line(ss)
    if st:
        footer_bits.append(st)
    if tt_last:
        footer_bits.append(
            "📅 " + (tt_last.astimezone(IST) if tt_last.tzinfo else tt_last.replace(tzinfo=IST))
                 .strftime("synced %d %b")
        )
    parts.append("<i>" + " · ".join(footer_bits) + "</i>")

    return "\n\n".join(parts)


# typing alias to avoid an import cycle in annotations
SubjectHealthLike = dict


def _absence_health_line(record: dict) -> Optional[str]:
    """'🚨 CS2011: 3 skip(s) left' for a subject that logged absences."""
    from app.services.attendance_health import subject_health
    h = subject_health(record)
    if h.level == "no_classes":
        return None
    code = esc(h.code)
    if h.level == "debarred":
        return f"💀 {code}: DEBAR ZONE — see professor"
    if h.rule is None:
        return f"🚨 {code}: new absence logged"
    left = max(h.ua_left or 0, 0)
    return f"🚨 {code}: {left} skip(s) left ({h.ua}/{h.rule.ua_limit})"
