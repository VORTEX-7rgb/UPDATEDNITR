"""Now & Next class resolution engine — pure function + rich HTML formatters.

All computations are wall-clock IST (Asia/Kolkata). India has no DST, so
`datetime.now(config.IST)` is the single source of truth across all servers.
"""
from __future__ import annotations

import html
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Optional, Sequence

from app.config import config, IST
from app.db.models import TimetableEntry


# ── Result data structures ───────────────────────────────────────────────────

@dataclass(frozen=True)
class ClassSlot:
    """Immutable view of a class slot for display."""
    subject_code: str
    room: str
    start_time: time
    end_time: time
    period_index: int
    weekday: int
    is_break: bool = False
    subject_name: str = ""
    course_type: str = ""

    @property
    def display_time(self) -> str:
        return f"{self.start_time.strftime('%H:%M')} – {self.end_time.strftime('%H:%M')}"

    @property
    def display_subject(self) -> str:
        if self.subject_name:
            return f"<b>{html.escape(self.subject_code)}</b> ({html.escape(self.subject_name)})"
        return f"<b>{html.escape(self.subject_code)}</b>"

    @property
    def display_room(self) -> str:
        if self.room:
            return f"<code>Room #{html.escape(self.room)}</code>"
        return "<i>No room assigned</i>"


@dataclass(frozen=True)
class NowNextResult:
    """Structured result of resolving current & next class at a given IST time."""
    current_class: Optional[ClassSlot]
    next_class: Optional[ClassSlot]
    is_lunch_break: bool
    is_before_first_class: bool
    is_day_done: bool
    is_weekend: bool
    minutes_until_next: Optional[int]
    next_class_day_offset: int  # 0=today, 1=tomorrow, >1=in N days
    evaluated_at_ist: datetime


WEEKDAY_LABELS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


# ── Resolution Engine ────────────────────────────────────────────────────────

def resolve_now_and_next(
    entries: Sequence[TimetableEntry],
    now_ist: Optional[datetime] = None,
) -> NowNextResult:
    """Pure resolution function: compares timetable entries against an IST datetime.

    Args:
        entries: All TimetableEntry rows for the user.
        now_ist: Optional datetime to evaluate at. If None, evaluates at current
            IST time (`datetime.now(config.IST)`). If tz-naive, assumed to be IST.
            If tz-aware non-IST, automatically converted to IST.

    Returns:
        NowNextResult with current/next class slots and status flags.
    """
    if now_ist is None:
        now_ist = datetime.now(IST)
    elif now_ist.tzinfo is None:
        now_ist = now_ist.replace(tzinfo=IST)
    else:
        now_ist = now_ist.astimezone(IST)

    if not entries:
        return NowNextResult(
            current_class=None,
            next_class=None,
            is_lunch_break=False,
            is_before_first_class=False,
            is_day_done=False,
            is_weekend=now_ist.weekday() in (5, 6),
            minutes_until_next=None,
            next_class_day_offset=0,
            evaluated_at_ist=now_ist,
        )

    now_t = now_ist.time()
    today_weekday = now_ist.weekday()

    # Convert models to ClassSlot dataclasses grouped by weekday
    by_day: dict[int, list[ClassSlot]] = {d: [] for d in range(7)}
    for e in entries:
        slot = ClassSlot(
            subject_code=e.subject_code,
            room=e.room or "",
            start_time=e.start_time,
            end_time=e.end_time,
            period_index=e.period_index,
            weekday=e.weekday,
            is_break=e.is_break,
            subject_name=e.subject_name or "",
            course_type=e.course_type or "",
        )
        by_day[e.weekday].append(slot)

    # Sort each day's slots by start_time
    for d in by_day:
        by_day[d].sort(key=lambda s: s.start_time)

    # ── 1. Determine current active slot (if any) ────────────────────────────
    current_class: Optional[ClassSlot] = None
    is_lunch_break = False

    today_slots = by_day[today_weekday]
    for s in today_slots:
        # Grace period for class end
        end_with_grace = (
            datetime.combine(now_ist.date(), s.end_time, tzinfo=IST)
            + timedelta(minutes=config.TIMETABLE_CLASS_END_GRACE_MIN)
        ).time()

        if s.start_time <= now_t <= end_with_grace:
            if s.is_break or "LUNCH" in s.subject_code.upper():
                is_lunch_break = True
            else:
                current_class = s
            break

    # ── 2. Determine next upcoming class ────────────────────────────────────
    next_class: Optional[ClassSlot] = None
    next_day_offset = 0
    minutes_until_next: Optional[int] = None

    # Search from today (offset=0) up to lookahead days ahead
    max_days = config.TIMETABLE_LOOKAHEAD_DAYS
    for offset in range(max_days):
        check_weekday = (today_weekday + offset) % 7
        candidate_slots = by_day[check_weekday]

        for s in candidate_slots:
            if s.is_break or "LUNCH" in s.subject_code.upper():
                continue  # Skip break rows when picking next class

            if offset == 0:
                # Today: must start in the future (or if currently in class, must be after current)
                if s.start_time > now_t:
                    next_class = s
                    next_day_offset = 0
                    target_dt = datetime.combine(
                        now_ist.date(), s.start_time, tzinfo=IST
                    )
                    minutes_until_next = max(0, int((target_dt - now_ist).total_seconds() // 60))
                    break
            else:
                # Future day: pick the first class of that day
                next_class = s
                next_day_offset = offset
                target_dt = datetime.combine(
                    now_ist.date() + timedelta(days=offset), s.start_time, tzinfo=IST
                )
                minutes_until_next = max(0, int((target_dt - now_ist).total_seconds() // 60))
                break

        if next_class is not None:
            break

    # Status flags
    has_today_classes = any(not s.is_break for s in today_slots)
    is_weekend = today_weekday in (5, 6)
    is_before_first = False
    is_day_done = False

    if has_today_classes and next_class is not None and next_day_offset == 0:
        first_class_today = next(s for s in today_slots if not s.is_break)
        if now_t < first_class_today.start_time:
            is_before_first = True
    elif has_today_classes and next_day_offset > 0:
        is_day_done = True

    return NowNextResult(
        current_class=current_class,
        next_class=next_class,
        is_lunch_break=is_lunch_break,
        is_before_first_class=is_before_first,
        is_day_done=is_day_done,
        is_weekend=is_weekend,
        minutes_until_next=minutes_until_next,
        next_class_day_offset=next_day_offset,
        evaluated_at_ist=now_ist,
    )


# ── Telegram HTML Formatters ─────────────────────────────────────────────────

def _format_duration(minutes: int) -> str:
    """Format minutes into human-readable duration (e.g. '45m', '2h 15m', '1d 4h')."""
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    rem_mins = minutes % 60
    if hours < 24:
        if rem_mins == 0:
            return f"{hours}h"
        return f"{hours}h {rem_mins}m"
    days = hours // 24
    rem_hours = hours % 24
    if rem_hours == 0:
        return f"{days}d"
    return f"{days}d {rem_hours}h"


def format_now_next_message(
    result: NowNextResult,
    last_synced_at: Optional[datetime] = None,
) -> str:
    """Format a rich HTML Telegram response for the 'Now & Next' command/button."""
    time_str = result.evaluated_at_ist.strftime("%A, %I:%M %p IST")
    lines = [f"⏰ <b>Class Status</b> — <i>{time_str}</i>\n"]

    # 1. Current Status Block
    if result.current_class:
        c = result.current_class
        # Calculate time remaining in current class
        end_dt = datetime.combine(
            result.evaluated_at_ist.date(), c.end_time, tzinfo=IST
        )
        remaining_mins = max(0, int((end_dt - result.evaluated_at_ist).total_seconds() // 60))
        rem_str = f" (ends in {_format_duration(remaining_mins)})" if remaining_mins > 0 else ""

        lines.append("🟢 <b>CURRENT CLASS:</b>")
        lines.append(f"  • {c.display_subject}")
        lines.append(f"  • 🕒 <code>{c.display_time}</code>{rem_str}")
        lines.append(f"  • 📍 {c.display_room}\n")

    elif result.is_lunch_break:
        lines.append("🍽️ <b>LUNCH BREAK</b> (12:00 – 13:15)\n")

    elif result.is_before_first_class:
        lines.append("🌅 <b>No active class yet today.</b>\n")

    elif result.is_day_done:
        lines.append("🌙 <b>All classes finished for today!</b>\n")

    elif result.is_weekend:
        lines.append("🏖️ <b>Enjoy your weekend!</b> No classes scheduled.\n")

    else:
        lines.append("☕ <b>No active class right now.</b> (Free period / break)\n")

    # 2. Next Class Block
    if result.next_class:
        n = result.next_class
        day_label = WEEKDAY_LABELS[n.weekday]
        if result.next_class_day_offset == 0:
            day_prefix = "Today"
        elif result.next_class_day_offset == 1:
            day_prefix = f"Tomorrow ({day_label})"
        else:
            day_prefix = f"{day_label} (+{result.next_class_day_offset}d)"

        in_str = f"in {_format_duration(result.minutes_until_next)}" if result.minutes_until_next is not None else ""

        lines.append(f"🔜 <b>NEXT CLASS ({day_prefix}):</b>")
        lines.append(f"  • {n.display_subject}")
        lines.append(f"  • 🕒 <code>{n.display_time}</code> — <b>{in_str}</b>")
        lines.append(f"  • 📍 {n.display_room}")
    else:
        lines.append("ℹ️ <i>No upcoming classes found in the next 7 days.</i>")

    if last_synced_at:
        synced_ist = last_synced_at.astimezone(IST) if last_synced_at.tzinfo else last_synced_at.replace(tzinfo=IST)
        lines.append(f"\n🔄 <i>Last synced: {synced_ist.strftime('%d %b %Y, %I:%M %p IST')}</i>")

    return "\n".join(lines)


def format_day_schedule(
    entries: Sequence[TimetableEntry],
    weekday: int,
) -> str:
    """Format one day's full timetable schedule into an HTML card."""
    day_name = WEEKDAY_LABELS[weekday]
    day_entries = [e for e in entries if e.weekday == weekday]
    day_entries.sort(key=lambda e: e.start_time)

    lines = [f"📅 <b>{day_name.upper()} TIMETABLE</b>\n"]

    if not day_entries:
        lines.append("<i>No classes scheduled for this day.</i>")
        return "\n".join(lines)

    for e in day_entries:
        time_str = f"{e.start_time.strftime('%H:%M')}–{e.end_time.strftime('%H:%M')}"
        if e.is_break or "LUNCH" in e.subject_code.upper():
            lines.append(f"🍽️ <code>{time_str}</code>  <b>LUNCH BREAK</b>")
        else:
            room_str = f" [#{e.room}]" if e.room else ""
            subj_title = f" - {e.subject_name}" if e.subject_name else ""
            lines.append(
                f"• <code>{time_str}</code>  <b>{html.escape(e.subject_code)}</b>{html.escape(subj_title)}{html.escape(room_str)}"
            )

    return "\n".join(lines)
