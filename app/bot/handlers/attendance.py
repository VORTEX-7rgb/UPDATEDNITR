"""Attendance screens powered by the Debar Engine (L-T-P budgets).

UX contract:
  * ONE bubble per interaction — edits whatever the user tapped.
  * List view: every subject gets a health emoji + plain-English budget line.
  * Subject detail: full math against NITRIS's official debar table.
  * Cached-first: instant render, background refresh, tiered latency personas.
"""
from __future__ import annotations

import asyncio
import logging

from aiogram import F, Router, types
from aiogram.enums import ParseMode
from aiogram.filters import Command, StateFilter
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select

from app.config import config
from app.db.database import get_db_session
from app.db.models import User
from app.services.attendance_health import (
    AttendanceSummary,
    SubjectHealth,
    summarize,
)
from app.ui import copy, theme
from app.ui.nav import ATT_REFRESH_CB, GLOSSARY_CB
from app.ui.surface import Surface, show
from app.utils import esc

logger = logging.getLogger(__name__)

router = Router(name="attendance_router")

SLOW_AFTER_SECONDS = config.ATTENDANCE_SLOW_AFTER_SECONDS
ATTLIST_CB = "ui|attlist"
ATTSUB_PREFIX = "ui|attsub|"
ATTDET_PREFIX = "ui|attdet|"
ATTDET_REFRESH_PREFIX = "ui|attdetrf|"
DETAILS_MODULE_PREFIX = "att_details:"

_DETAILS_STATUS_EMOJI = {
    "present": "🟢",
    "absent": "🔴",
    "leave": "🔵",
    "present_late": "🟠",
    "absent_late": "💗",
    "unknown": "⚪",
}

_DETAILS_LEGEND = (
    "🟢 Present · 🔴 Absent · 🔵 Leave · 🟠/💗 Late-reg"
)

_OVERALL_LABEL = {
    "safe": "SAFE",
    "warn": "WATCH",
    "risk": "AT RISK",
    "danger": "CRITICAL",
    "debarred": "DEBARRED SUBJECT",
    "unknown": "PARTIAL DATA",
    "no_classes": "NOT STARTED",
}


# ── Data ────────────────────────────────────────────────────────────────────

async def _load_summary(user_id: int) -> AttendanceSummary | None:
    from app.db.repositories.snapshot_repository import SnapshotRepository
    async with get_db_session() as session:
        repo = SnapshotRepository(session)
        snap = await repo.get_latest_snapshot(user_id, "attendance")
    if not snap or not getattr(snap, "snapshot_json", None):
        return None
    records = snap.snapshot_json.get("records") or []
    if not isinstance(records, list):
        return None
    return summarize(records)


def _summary_from_snapshot(snap) -> AttendanceSummary | None:
    """Render-ready summary from an already-loaded Snapshot row (or None)."""
    if not snap or not getattr(snap, "snapshot_json", None):
        return None
    records = snap.snapshot_json.get("records") or []
    if not isinstance(records, list):
        return None
    return summarize(records)


async def _load_user_and_summary(telegram_id: int):
    """PERF: ONE session/round trip for (User, latest attendance summary).

    Every attendance entry point previously paid two serial DB sessions
    (user lookup, then snapshot load). Merged — first paint gets ~1 RTT
    faster on every tap.
    """
    from app.db.repositories.snapshot_repository import SnapshotRepository
    async with get_db_session() as session:
        stmt = select(User.id).where(User.telegram_id == telegram_id)
        uid = (await session.execute(stmt)).scalar_one_or_none()
        snap = (
            await SnapshotRepository(session).get_latest_snapshot(uid, "attendance")
            if uid is not None else None
        )
    if uid is None:
        return None, None
    return uid, _summary_from_snapshot(snap)


def _records_from_result(data) -> list[dict]:
    try:
        d = data.to_dict()
        return d.get("records") or []
    except Exception:
        return []


# ── Pure renderers ──────────────────────────────────────────────────────────

def _subject_line(h: SubjectHealth) -> str:
    name = esc((h.name or "").strip()[:30])
    tag = f" <i>[{h.ltp}]</i>" if h.ltp else ""
    if h.level == "no_classes":
        return (f"{h.emoji} <b>{esc(h.code)}</b> {name}{tag}\n"
                f"      <i>classes haven't started yet</i>")
    if h.level == "unknown":
        return (f"{h.emoji} <b>{esc(h.code)}</b> {name}\n"
                f"      {h.tc} held · {h.ua} skipped · <i>pattern not tracked yet</i>")
    if h.level == "debarred":
        second = "💀 <b>DEBAR ZONE — talk to your professor NOW</b>"
    else:
        left = h.ua_left if h.ua_left is not None else 0
        second = f"{h.tc} held · {h.ua} skipped · <b>{left} skip(s) to the line</b>"
    return f"{h.emoji} <b>{esc(h.code)}</b> {name}{tag}\n      {second}"


def _list_text(summary: AttendanceSummary | None, status_line: str | None = None) -> str:
    if summary is None or not summary.subjects:
        body = theme.quote(
            "No attendance on file yet.\nTap <b>Refresh</b> to pull it from NITRIS."
        )
        text = f"{theme.ICON_ATT} <b>ATTENDANCE</b>\n{body}"
        return f"{text}\n\n{status_line}" if status_line else text

    overall = _OVERALL_LABEL.get(summary.level, "?")
    body = f"Overall: <b>{summary.emoji} {overall}</b>\n\n"
    body += "\n".join(_subject_line(s) for s in summary.subjects)

    r = summary.riskiest
    if r is not None and r.level in ("warn", "risk", "danger", "debarred"):
        body += f"\n\n⚠️ Tightest budget: <b>{esc(r.code)}</b> — watch this one."

    text = f"{theme.ICON_ATT} <b>ATTENDANCE</b>\n{theme.quote(body)}"
    return f"{text}\n\n{status_line}" if status_line else text


def _detail_text(s: SubjectHealth) -> str:
    title = f"{theme.ICON_ATT} <b>{esc(s.code)}</b> · {esc(s.name)}"
    meta = f"<i>Pattern {s.ltp or '?'}" + (f" · {esc(s.faculty)}" if s.faculty else "") + "</i>"

    if s.level == "no_classes":
        body = theme.quote("Classes haven't started yet.\nNothing to track — enjoy it.")
        return f"{title}\n{meta}\n{body}"

    if s.level == "unknown":
        body = theme.quote(
            f"Classes held: <b>{s.tc}</b>\n"
            f"Skips: <b>{s.ua}</b> · Approved leave: <b>{s.le}</b> · "
            f"Missed total: <b>{s.oa}</b>\n\n"
            "<i>NITRIS hasn't published a debar pattern for this L-T-P yet — "
            "raw numbers shown honestly until it does.</i>"
        )
        return f"{title}\n{meta}\n{body}"

    rule = s.rule
    lines = [
        f"Classes held: <b>{s.tc}</b>",
        f"Skips (UA): <b>{s.ua}</b> — debar line at <b>{rule.ua_limit}</b>",
        f"Approved leave (LE): <b>{s.le}</b>",
        f"Missed total (OA): <b>{s.oa}</b> — hard cap <b>{rule.oa_cap}</b>",
    ]
    if s.level == "debarred":
        verdict = "💀 <b>DEBARRED ZONE.</b> Crossed NITRIS's limit — speak to your professor today."
    elif s.level == "danger":
        verdict = f"🔴 <b>LAST SKIPS.</b> Only {max(s.ua_left or 0, 0)} more before the debar line."
    elif s.level == "risk":
        verdict = "🟠 Grade-penalty territory. Attendance recovery mode: ON."
    elif s.level == "warn":
        verdict = "🟡 Half your skip budget is gone. Go easy."
    else:
        verdict = f"🟢 Safe. {s.ua_left} skip(s) before the debar line."

    body = theme.quote("\n".join(lines))
    return f"{title}\n{meta}\n{body}\n\n{verdict}"


def _day_token(d: dict) -> str:
    status = d.get("status") or "unknown"
    day = d.get("day") or 0
    emoji = _DETAILS_STATUS_EMOJI.get(status, "⚪")
    if status in ("absent", "absent_late"):
        return f"{emoji}<b>{day}</b>"
    return f"{emoji}{day}"


def _verdict_line(s: SubjectHealth | None) -> str:
    """Format one-line debar verdict banner for the top of the date-wise screen."""
    if s is None:
        return ""
    if s.level == "no_classes":
        return "⚪ <i>Classes haven't started yet.</i>"
    if s.level == "debarred":
        return "💀 <b>DEBARRED ZONE — talk to your professor NOW</b>"
    if s.level == "danger":
        return f"🔴 <b>LAST SKIPS.</b> Only {max(s.ua_left or 0, 0)} more before debar line."
    if s.level == "risk":
        return f"🟠 <b>Grade-penalty territory.</b> ({s.ua_left} skip(s) left)"
    if s.level == "warn":
        return f"🟡 <b>Half budget gone.</b> {s.ua_left} skip(s) left."
    if s.level == "unknown":
        return "❔ <i>Raw attendance (pattern not published yet).</i>"
    return f"🟢 Safe. <b>{s.ua_left} skip(s)</b> before debar line."


def _details_text(
    data: dict | None,
    code: str,
    status_line: str | None = None,
    subject: SubjectHealth | None = None,
) -> str:
    safe_code = esc(code)
    subject_label = esc(
        (data.get("subject_label") if data else None)
        or (subject.name if subject else "")
        or code
    )
    session_label = esc((data.get("session_label") if data else None) or "")
    totals = (data.get("totals") if data else None) or {}

    header_parts = [f"{theme.ICON_ATT} <b>{safe_code}</b> · {subject_label}"]
    if session_label:
        header_parts.append(f"<i>{session_label}</i>")

    v_line = _verdict_line(subject)
    if v_line:
        header_parts.append(v_line)

    # If no month matrix yet (first-time loading preview)
    if not data or not data.get("months"):
        if subject is not None and subject.tc > 0:
            tc = subject.tc
            ua = subject.ua
            le = subject.le
            oa = subject.oa
            pres = tc - oa
            pct = round((pres / tc * 100), 1) if tc else 0.0
            body_lines = [
                f"Classes: <b>{tc}</b> · Present: <b>{pres}</b> · Absent: <b>{ua}</b>"
                + (f" · Leave: <b>{le}</b>" if le else ""),
                f"Attendance: <b>{pct}%</b>",
                "",
                "⏳ <i>Loading date-by-date calendar from NITRIS…</i>",
                "",
                f"<i>{_DETAILS_LEGEND}</i>",
            ]
            text = "\n".join(header_parts) + "\n" + theme.quote("\n".join(body_lines))
            return f"{text}\n\n{status_line}" if status_line else text

        body = theme.quote(
            f"No date-wise records on file for <b>{safe_code}</b> yet.\n"
            "⏳ <i>Fetching from NITRIS…</i>"
        )
        text = "\n".join(header_parts) + "\n" + body
        return f"{text}\n\n{status_line}" if status_line else text

    # Full month matrix available
    total_classes = totals.get("total", subject.tc if subject else 0)
    present = totals.get("present", (subject.tc - subject.oa) if subject else 0)
    absent = totals.get("absent", subject.ua if subject else 0)
    leave = totals.get("leave", subject.le if subject else 0)
    pct = round((present / total_classes * 100), 1) if total_classes else 0.0

    month_blocks: list[str] = []
    for m in data.get("months") or []:
        m_name = esc(m.get("name") or "")
        m_count = m.get("count", 0)
        m_sub = (m.get("submission") or "").strip()
        badge = " · <i>Pending</i>" if m_sub.lower() == "pending" else ""

        days = m.get("days") or []
        if not days:
            month_blocks.append(f"<b>{m_name}</b> ({m_count}){badge}\n  <i>No classes recorded</i>")
            continue

        lines: list[str] = []
        row: list[str] = []
        for d in days:
            row.append(_day_token(d))
            if len(row) == 8:
                lines.append(" ".join(row))
                row = []
        if row:
            lines.append(" ".join(row))

        month_blocks.append(
            f"<b>{m_name}</b> ({m_count}){badge}\n  " + "\n  ".join(lines)
        )

    body_lines = [
        "\n\n".join(month_blocks),
        "",
        f"Classes: <b>{total_classes}</b> · Present: <b>{present}</b> · "
        f"Absent: <b>{absent}</b>" + (f" · Leave: <b>{leave}</b>" if leave else ""),
        f"Attendance: <b>{pct}%</b>",
        "",
        f"<i>{_DETAILS_LEGEND}</i>",
    ]

    text = (
        "\n".join(header_parts) + "\n" +
        theme.quote("\n".join(body_lines))
    )
    return f"{text}\n\n{status_line}" if status_line else text


# ── Keyboards ───────────────────────────────────────────────────────────────

def _kb_viewing(summary: AttendanceSummary | None = None):
    """List keyboard: tappable subject codes (3/row) + actions + nav."""
    b = InlineKeyboardBuilder()
    if summary and summary.subjects:
        tracked = [s for s in summary.subjects if s.level != "no_classes"]
        row: list[types.InlineKeyboardButton] = []
        for s in tracked:
            row.append(types.InlineKeyboardButton(
                text=s.code, callback_data=f"{ATTSUB_PREFIX}{s.code}"
            ))
            if len(row) == 3:
                b.row(*row)
                row = []
        if row:
            b.row(*row)
    b.row(theme.btn("🔄 Refresh", ATT_REFRESH_CB))
    b.row(theme.btn("📖 What do these mean?", GLOSSARY_CB))
    b.row(theme.home_button())
    return b.as_markup()


def _kb_busy():
    b = InlineKeyboardBuilder()
    b.row(theme.home_button())
    return b.as_markup()


def _kb_detail(code: str = ""):
    return _kb_dates(code)


def _kb_dates(code: str):
    b = InlineKeyboardBuilder()
    b.row(theme.btn("🔄 Refresh", f"{ATTDET_REFRESH_PREFIX}{code}"))
    b.row(theme.btn("← All Subjects", ATTLIST_CB))
    b.row(theme.btn("📖 What do these mean?", GLOSSARY_CB))
    b.row(theme.home_button())
    return b.as_markup()


# ── Core refresh flow (single bubble lifecycle) ────────────────────────────

async def _run_flow(
    surf: Surface, user_id: int, cached: AttendanceSummary | None,
    chat_id: int | None = None, message_id: int | None = None,
) -> None:
    """LAYER 2 (timetable pattern): enqueue and RETURN immediately.

    The job renders this same bubble itself on success AND failure — no
    inline await, no 120s hang, no poke. The interactive worker frees in
    milliseconds, so bursts of taps can never pile up handlers.
    """
    has_cache = bool(cached and cached.subjects)
    await surf.edit(
        _list_text(cached, copy.UPDATING if has_cache else "⏳ <i>Fetching from NITRIS…</i>"),
        _kb_busy(),
    )

    from app.nitris.job_queue import nitris_job_queue, Priority
    from app.nitris.gateway import NitrisCircuitOpenError

    try:
        await nitris_job_queue.enqueue(
            job_type="attendance_refresh",
            user_id=user_id,
            priority=Priority.HIGH,
            dedup_key=f"attendance_refresh:user:{user_id}",
            payload={
                "callback_chat_id": chat_id,
                "callback_message_id": message_id,
                "interaction_token": getattr(surf, "owner_token", None),
            },
        )
    except NitrisCircuitOpenError:
        # PERF (fairness): the tap never produced a sync — give the user their
        # cooldown back so a retry after the portal recovers isn't blocked.
        from app.nitris.rate_limiter import operation_cooldown
        await operation_cooldown.clear(user_id, "attendance_refresh")
        await surf.final(_list_text(cached, copy.CIRCUIT_DOWN), _kb_viewing(cached))
    except RuntimeError as e:
        # Queue-full rejection — answer the user instead of dying silently.
        from app.nitris.rate_limiter import operation_cooldown
        await operation_cooldown.clear(user_id, "attendance_refresh")
        logger.warning("Attendance refresh enqueue rejected: %r", e)
        await surf.final(_list_text(cached, copy.QUEUE_BUSY), _kb_viewing(cached))


# ── Entry points ────────────────────────────────────────────────────────────

async def fetch_attendance_for_callback(callback: types.CallbackQuery, user: User):
    from app.nitris.rate_limiter import operation_cooldown, COOLDOWN_ATTENDANCE_REFRESH

    # CACHE-FIRST, ALWAYS (user contract): even when the anti-spam cooldown is
    # active, this tap must immediately render the LATEST CACHED attendance
    # plus an inline countdown — never a dead bubble, never a dependent alert.
    allowed, wait = await operation_cooldown.check(
        user.id, "attendance_refresh", cooldown_seconds=COOLDOWN_ATTENDANCE_REFRESH
    )
    cached = await _load_summary(user.id)
    surf = Surface(callback.message)  # EDIT WHAT YOU TAPPED

    if not allowed:
        cooldown_note = f"⏳ Synced just now. Next live refresh in {wait}s."
        await surf.edit(
            _list_text(cached, cooldown_note),
            _kb_viewing(cached),
        )
        return

    await _run_flow(surf, user.id, cached,
                    chat_id=callback.message.chat.id,
                    message_id=callback.message.message_id)


@router.message(Command("attendance"), StateFilter(None))
async def cmd_attendance(message: types.Message):
    telegram_id = message.from_user.id

    # PERF: user + latest snapshot in ONE round trip (was two serial sessions).
    user_id, cached = await _load_user_and_summary(telegram_id)
    if user_id is None:
        await message.answer("⚠️ You haven't registered yet! Please use /start to register.")
        return

    # Warm-on-interaction registry seed (see app/bot/middlewares.py).
    from app.bot.middlewares import note_registered_user
    note_registered_user(telegram_id, user_id)

    from app.nitris.rate_limiter import operation_cooldown, COOLDOWN_ATTENDANCE_REFRESH
    allowed, wait = await operation_cooldown.check(
        user_id, "attendance_refresh", cooldown_seconds=COOLDOWN_ATTENDANCE_REFRESH
    )
    if not allowed:
        if cached:
            await message.answer(
                _list_text(cached, f"⏳ Synced just now. Next live refresh in {wait}s."),
                reply_markup=_kb_viewing(cached),
                parse_mode=ParseMode.HTML,
            )
        else:
            await message.answer(
                f"⏳ You just refreshed attendance. Please wait {wait}s before trying again.",
                parse_mode=ParseMode.HTML,
            )
        return

    first = await message.answer(
        _list_text(cached, copy.UPDATING if cached and cached.subjects else None),
        reply_markup=_kb_busy(),
    )
    surf = Surface(first)
    await _run_flow(surf, user_id, cached,
                    chat_id=first.chat.id,
                    message_id=first.message_id)


# ── Cached navigation (Back targets — zero portal traffic) ─────────────────

@router.callback_query(F.data == ATTLIST_CB)
async def cb_attendance_list(callback: types.CallbackQuery):
    try:
        await callback.answer()
    except Exception:
        pass

    # PERF: one round trip (was user lookup + snapshot load).
    user_id, summary = await _load_user_and_summary(callback.from_user.id)
    if user_id is None:
        await show(callback.message, "⚠️ Not registered. Send /start.")
        return

    await show(callback.message, _list_text(summary), _kb_viewing(summary))


@router.callback_query(F.data.startswith(ATTSUB_PREFIX))
async def cb_attendance_subject(callback: types.CallbackQuery):
    """Subject tapped from list -> go DIRECTLY to date-wise calendar."""
    try:
        await callback.answer()
    except Exception:
        pass

    # Support both ATTSUB_PREFIX (ui|attsub|) and ATTDET_PREFIX (ui|attdet|)
    raw = callback.data or ""
    if raw.startswith(ATTSUB_PREFIX):
        code = raw[len(ATTSUB_PREFIX):].strip().upper()
    elif raw.startswith(ATTDET_PREFIX):
        code = raw[len(ATTDET_PREFIX):].strip().upper()
    else:
        code = raw.strip().upper()

    user_id, summary = await _load_user_and_summary(callback.from_user.id)
    if user_id is None:
        await show(callback.message, "⚠️ Not registered. Send /start.")
        return

    target = next(
        (s for s in (summary.subjects if summary else []) if s.code.upper() == code),
        None,
    )
    if target is None:
        await show(callback.message, _list_text(summary), _kb_viewing(summary))
        return

    cached_details = await _load_details(user_id, code)
    if cached_details and cached_details.get("months"):
        # INSTANT (< 20ms from DB) — ZERO PORTAL LATENCY!
        await show(
            callback.message,
            _details_text(cached_details, code, subject=target),
            _kb_dates(code),
        )
        return

    # First time ever: auto-trigger fetch in background, show instant preview!
    surf = Surface(callback.message)
    await _run_details_flow(
        surf, user_id, code, cached_details,
        chat_id=callback.message.chat.id,
        message_id=callback.message.message_id,
        subject=target,
    )


# ── Date-wise matrix navigation (cached-first + live refresh) ───────────────

def _details_module(code: str) -> str:
    return f"{DETAILS_MODULE_PREFIX}{code.upper()}"


async def _load_details(user_id: int, code: str) -> dict | None:
    from app.db.repositories.snapshot_repository import SnapshotRepository
    async with get_db_session() as session:
        repo = SnapshotRepository(session)
        snap = await repo.get_latest_snapshot(user_id, _details_module(code))
    if not snap or not getattr(snap, "snapshot_json", None):
        return None
    return snap.snapshot_json


async def _run_details_flow(
    surf: Surface,
    user_id: int,
    code: str,
    cached: dict | None,
    chat_id: int | None = None,
    message_id: int | None = None,
    subject: SubjectHealth | None = None,
) -> None:
    """LAYER 2 (timetable pattern): enqueue details fetch and return immediately."""
    has_cache = bool(cached and cached.get("months"))
    await surf.edit(
        _details_text(
            cached, code,
            copy.UPDATING if has_cache else "⏳ <i>Fetching date-wise matrix from NITRIS…</i>",
            subject=subject,
        ),
        _kb_dates(code),
    )

    from app.nitris.job_queue import nitris_job_queue, Priority
    from app.nitris.gateway import NitrisCircuitOpenError

    try:
        await nitris_job_queue.enqueue(
            job_type="attendance_details_fetch",
            user_id=user_id,
            priority=Priority.HIGH,
            dedup_key=f"attendance_details:user:{user_id}:{code.upper()}",
            payload={
                "subject_code": code,
                "callback_chat_id": chat_id,
                "callback_message_id": message_id,
                "interaction_token": getattr(surf, "owner_token", None),
            },
        )
    except NitrisCircuitOpenError:
        from app.nitris.rate_limiter import operation_cooldown
        await operation_cooldown.clear(user_id, f"att_details_refresh:{code.upper()}")
        await surf.final(
            _details_text(cached, code, copy.CIRCUIT_DOWN, subject=subject),
            _kb_dates(code),
        )
    except RuntimeError as e:
        from app.nitris.rate_limiter import operation_cooldown
        await operation_cooldown.clear(user_id, f"att_details_refresh:{code.upper()}")
        logger.warning("Attendance details enqueue rejected: %r", e)
        await surf.final(
            _details_text(cached, code, copy.QUEUE_BUSY, subject=subject),
            _kb_dates(code),
        )


@router.callback_query(F.data.startswith(ATTDET_PREFIX))
async def cb_attendance_dates(callback: types.CallbackQuery):
    """Alias to cb_attendance_subject for backward compatibility."""
    return await cb_attendance_subject(callback)


@router.callback_query(F.data.startswith(ATTDET_REFRESH_PREFIX))
async def cb_attendance_dates_refresh(callback: types.CallbackQuery):
    """Force fresh scrape of date-wise attendance matrix from NITRIS."""
    try:
        await callback.answer()
    except Exception:
        pass

    code = callback.data[len(ATTDET_REFRESH_PREFIX):].strip().upper()
    user_id, summary = await _load_user_and_summary(callback.from_user.id)
    if user_id is None:
        await show(callback.message, "⚠️ Not registered. Send /start.")
        return

    target = next(
        (s for s in (summary.subjects if summary else []) if s.code.upper() == code),
        None,
    )
    if target is None:
        await show(callback.message, _list_text(summary), _kb_viewing(summary))
        return

    from app.nitris.rate_limiter import operation_cooldown, COOLDOWN_ATTENDANCE_REFRESH

    action_key = f"att_details_refresh:{code}"
    allowed, wait = await operation_cooldown.check(
        user_id, action_key, cooldown_seconds=COOLDOWN_ATTENDANCE_REFRESH
    )
    cached_details = await _load_details(user_id, code)
    surf = Surface(callback.message)

    if not allowed:
        cooldown_note = f"⏳ Synced just now. Next live refresh in {wait}s."
        await surf.edit(
            _details_text(cached_details, code, cooldown_note, subject=target),
            _kb_dates(code),
        )
        return

    await _run_details_flow(
        surf, user_id, code, cached_details,
        chat_id=callback.message.chat.id,
        message_id=callback.message.message_id,
        subject=target,
    )


# ── Glossary ────────────────────────────────────────────────────────────────

@router.callback_query(F.data == GLOSSARY_CB)
async def cb_glossary(callback: types.CallbackQuery):
    try:
        await callback.answer()
    except Exception:
        pass
    text = (
        f"{theme.BRAND}\n\n"
        f"{copy.GLOSSARY_TITLE}\n\n"
        f"{copy.GLOSSARY_BODY}\n\n"
        f"<i>{copy.GLOSSARY_NOTE}</i>"
    )
    await show(callback.message, text,
               theme.footer_kb(back_cb=ATTLIST_CB, back_text="← Back to Attendance"))

