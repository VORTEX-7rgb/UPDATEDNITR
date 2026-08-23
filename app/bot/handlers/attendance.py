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
from sqlalchemy.orm import selectinload

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


def _kb_detail():
    b = InlineKeyboardBuilder()
    b.row(theme.btn("← All Subjects", ATTLIST_CB))
    b.row(theme.btn("📖 What do these mean?", GLOSSARY_CB))
    b.row(theme.home_button())
    return b.as_markup()


# ── Core refresh flow (single bubble lifecycle) ────────────────────────────

async def _run_flow(surf: Surface, user_id: int, cached: AttendanceSummary | None) -> None:
    has_cache = bool(cached and cached.subjects)
    await surf.edit(
        _list_text(cached, copy.UPDATING if has_cache else "⏳ <i>Fetching from NITRIS…</i>"),
        _kb_busy(),
    )
    base = _list_text(cached) if has_cache else ""
    if base:
        surf.poke_later(SLOW_AFTER_SECONDS, f"{base}\n\n{copy.slow_note('checking')}")

    from app.nitris.job_queue import nitris_job_queue, Priority
    from app.nitris.gateway import NitrisCircuitOpenError

    try:
        future = await nitris_job_queue.enqueue(
            job_type="attendance_refresh",
            user_id=user_id,
            priority=Priority.HIGH,
            dedup_key=f"attendance_refresh:user:{user_id}",
            payload={},
        )
        try:
            result = await asyncio.wait_for(future, timeout=120.0)
            if result.get("success") and result.get("data"):
                fresh = summarize(_records_from_result(result["data"]))
                await surf.final(_list_text(fresh, copy.UPDATED_JUST_NOW), _kb_viewing(fresh))
            else:
                await surf.final(
                    _list_text(cached, "⚠️ Couldn't update right now — showing last known."),
                    _kb_viewing(cached),
                )
        except asyncio.TimeoutError:
            await surf.final(_list_text(cached, copy.STILL_RUNNING), _kb_viewing(cached))

    except NitrisCircuitOpenError:
        await surf.final(_list_text(cached, copy.CIRCUIT_DOWN), _kb_viewing(cached))
    except RuntimeError as e:
        # Queue-full rejection (hard depth cap) — tell the user instead of dying.
        logger.warning("Attendance enqueue rejected: %r", e)
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

    await _run_flow(surf, user.id, cached)


@router.message(Command("attendance"), StateFilter(None))
async def cmd_attendance(message: types.Message):
    telegram_id = message.from_user.id

    async with get_db_session() as session:
        stmt = select(User).options(selectinload(User.sync_state)).where(
            User.telegram_id == telegram_id
        )
        res = await session.execute(stmt)
        user = res.scalar_one_or_none()

    if not user:
        await message.answer("⚠️ You haven't registered yet! Please use /start to register.")
        return

    from app.nitris.rate_limiter import operation_cooldown, COOLDOWN_ATTENDANCE_REFRESH
    allowed, wait = await operation_cooldown.check(
        user.id, "attendance_refresh", cooldown_seconds=COOLDOWN_ATTENDANCE_REFRESH
    )
    if not allowed:
        cached = await _load_summary(user.id)
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

    cached = await _load_summary(user.id)
    first = await message.answer(
        _list_text(cached, copy.UPDATING if cached and cached.subjects else None),
        reply_markup=_kb_busy(),
    )
    surf = Surface(first)
    await _run_flow(surf, user.id, cached)


# ── Cached navigation (Back targets — zero portal traffic) ─────────────────

@router.callback_query(F.data == ATTLIST_CB)
async def cb_attendance_list(callback: types.CallbackQuery):
    try:
        await callback.answer()
    except Exception:
        pass

    async with get_db_session() as session:
        stmt = select(User).where(User.telegram_id == callback.from_user.id)
        user = (await session.execute(stmt)).scalar_one_or_none()
    if not user:
        await show(callback.message, "⚠️ Not registered. Send /start.")
        return

    summary = await _load_summary(user.id)
    await show(callback.message, _list_text(summary), _kb_viewing(summary))


@router.callback_query(F.data.startswith(ATTSUB_PREFIX))
async def cb_attendance_subject(callback: types.CallbackQuery):
    try:
        await callback.answer()
    except Exception:
        pass

    code = callback.data[len(ATTSUB_PREFIX):]

    async with get_db_session() as session:
        stmt = select(User).where(User.telegram_id == callback.from_user.id)
        user = (await session.execute(stmt)).scalar_one_or_none()
    if not user:
        await show(callback.message, "⚠️ Not registered. Send /start.")
        return

    summary = await _load_summary(user.id)
    target = next(
        (s for s in (summary.subjects if summary else []) if s.code.upper() == code.upper()),
        None,
    )
    if target is None:
        await show(callback.message, _list_text(summary), _kb_viewing(summary))
        return

    await show(callback.message, _detail_text(target), _kb_detail())


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

