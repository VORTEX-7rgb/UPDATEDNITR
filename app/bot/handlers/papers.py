"""Previous Year Question Papers handlers (search, metadata, download, batch)."""

import logging
import asyncio
import html

from aiogram import Router, types, F
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.enums import ParseMode
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.db.database import get_db_session
from app.db.repositories.user_repository import UserRepository
from app.db.repositories.snapshot_repository import SnapshotRepository
from app.services.examination_service import ExaminationService
from app.services.qpaper_service import QPResult
from app.utils import esc

from app.bot.fsm import QuestionPaperFlow
from app.bot import qpaper_registry
from app.ui.surface import show, Surface
from app.ui import copy as ui_copy
from app.ui import theme as ui_theme

logger = logging.getLogger(__name__)

router = Router(name="papers_router")

YEAR_MAP = {
    "2627A": "2026-27/Autumn",
    "2526S": "2025-26/Autumn",
    "2425A": "2024-25/Autumn",
    "2324A": "2023-24/Autumn",
    "2223A": "2022-23/Autumn"
}

REVERSE_YEAR_MAP = {v: k for k, v in YEAR_MAP.items()}


@router.message(Command("papers"), StateFilter(None))
async def cmd_papers(message: types.Message, state: FSMContext, explicit_telegram_id: int | None = None) -> None:
    """Entry point for Previous Year Question Papers flow. Resolves current subjects automatically."""
    telegram_id = explicit_telegram_id or message.from_user.id
    await state.clear()

    async with get_db_session() as session:
        user_repo = UserRepository(session)
        user = await user_repo.get_by_telegram_id(telegram_id)

        if not user:
            await message.answer("⚠️ You haven't registered yet! Please use /start to register.")
            return

        snapshot_repo = SnapshotRepository(session)
        snapshot = await snapshot_repo.get_latest_snapshot(user.id, "attendance")

    courses = []
    if snapshot and getattr(snapshot, "snapshot_json", None) and "records" in snapshot.snapshot_json:
        courses = snapshot.snapshot_json["records"]

    text = (
        "📝 <b>QUESTION PAPERS</b>\n"
        "<i>Your courses · pick one to hunt papers</i>\n\n"
        + ui_theme.quote("\n".join(
            f"<b>{esc(c.get('subject_code', '?'))}</b> — {esc((c.get('subject_name') or '')[:34])}"
            for c in courses
        ) or "No registered courses found.\nRun /attendance once and they'll appear here.")
        + "\n\n<i>Papers marked 🚀 deliver instantly from cache.</i>"
    )

    builder = InlineKeyboardBuilder()

    if courses:
        for idx, course in enumerate(courses, start=1):
            code = course.get("subject_code", "Unknown")
            name = course.get("subject_name", "Unknown")
            builder.row(types.InlineKeyboardButton(text=f"📚 {code} · {name[:22]}", callback_data=f"qp_sub_{code}"))
    else:
        text += "\n⚠️ <i>No subjects yet? Tap Refresh on your Attendance screen first.</i>"

    builder.row(types.InlineKeyboardButton(text="🔍 Search Other Subjects", callback_data="qp_search_prompt"))
    if courses:
        builder.row(types.InlineKeyboardButton(text="📥 Download All Current Papers", callback_data="qp_dlall_prompt"))
    builder.row(types.InlineKeyboardButton(text="🏠 Back to Dashboard", callback_data="inbox_back_dashboard"))

    await message.answer(text, reply_markup=builder.as_markup(), parse_mode=ParseMode.HTML)


@router.callback_query(F.data.startswith("qp_sub_"))
async def handle_subject_selected(callback: types.CallbackQuery, state: FSMContext) -> None:
    """Callback triggered when a student selects a subject. Renders year selector."""
    subject_code = callback.data[7:]

    try:
        await callback.answer()
    except Exception:
        pass

    text = (
        f"📅 <b>Select Academic Year</b>\n\n"
        f"Subject: <b>{esc(subject_code)}</b>\n\n"
        f"Please select the historical exam year you want to retrieve papers for:"
    )

    builder = InlineKeyboardBuilder()
    for code, label in YEAR_MAP.items():
        builder.row(types.InlineKeyboardButton(text=label, callback_data=f"qp_yr_{subject_code}_{code}"))

    builder.row(types.InlineKeyboardButton(text="◀️ Back to Subjects", callback_data="qp_back_subjects"))

    # PERF F1: not-modified-safe render (re-taps of the same year list are free).
    await show(callback.message, text, reply_markup=builder.as_markup())


@router.callback_query(F.data == "qp_back_subjects")
async def handle_qp_back_subjects(callback: types.CallbackQuery, state: FSMContext) -> None:
    try:
        await callback.answer()
    except Exception:
        pass

    try:
        await callback.message.delete()
    except Exception:
        pass

    await cmd_papers(callback.message, state, explicit_telegram_id=callback.from_user.id)


@router.callback_query(F.data.startswith("qp_yr_"))
async def handle_year_selected(callback: types.CallbackQuery, state: FSMContext) -> None:
    """Triggered when user picks an academic year for a subject. Single-flight dedup enabled."""
    telegram_id = callback.from_user.id
    data = callback.data[6:]
    subject_code, year_code = data.rsplit("_", 1)
    from app.utils import current_academic_year
    full_year_str = YEAR_MAP.get(year_code) or current_academic_year()

    try:
        await callback.answer("⏳ Locating question papers...")
    except Exception:
        pass

    status_msg = await callback.message.answer("⏳ Querying question paper database cache...")
    from app.ui.surface import Surface
    from app.ui import copy as ui_copy, theme as ui_theme
    surf = Surface(status_msg)

    async with get_db_session() as session:
        user_repo = UserRepository(session)
        user = await user_repo.get_by_telegram_id(telegram_id)

        if not user:
            await show(status_msg, "❌ You are not registered. Use /start to register.")
            return

        exam_service = ExaminationService(session)
        mid_cache = await exam_service.get_cached_paper(subject_code, full_year_str, "mid_sem")
        end_cache = await exam_service.get_cached_paper(subject_code, full_year_str, "end_sem")

    _kb_back_year = lambda: ui_theme.footer_kb(back_cb=f"qp_sub_{subject_code}", back_text="← Back")

    # Negative cache is PERMANENT by design: if cached rows exist for this
    # subject/year, trust them and never re-query NITRIS (professors do not
    # retroactively upload papers). Manual recovery: /admin_reset_qp.
    if not mid_cache and not end_cache:
        from app.nitris.job_queue import nitris_job_queue, Priority
        from app.nitris.gateway import NitrisCircuitOpenError
        from app.services.examination_service import _clean_code

        clean_subj = _clean_code(subject_code)
        dedup_key = f"qp_metadata:{clean_subj}:{full_year_str}"

        await surf.edit(
            f"📝 <b>{esc(subject_code)}</b> · {esc(full_year_str)}\n"
            + ui_theme.quote("⚡ Checking NITRIS for papers…\n\n<i>Sharing the request with other "
                             "students so nobody hammers the portal.</i>")
            ,
            ui_theme.footer_kb(back_cb=f"qp_sub_{subject_code}", back_text="← Back"),
        )
        surf.poke_later(4.0, ui_copy.slow_note("checking NITRIS"))

        try:
            future = await nitris_job_queue.enqueue(
                job_type="qp_metadata_fetch",
                user_id=user.id,
                priority=Priority.MEDIUM,
                dedup_key=dedup_key,
                payload={
                    "academic_year": full_year_str,
                    "subject_code": subject_code,
                    "roll_number": user.roll_number,
                },
                timeout=90.0,
            )

            try:
                result = await asyncio.wait_for(future, timeout=90.0)
            except asyncio.TimeoutError:
                await surf.final(
                    "⏳ <b>NITRIS is slow — your request is queued safely.</b>\n\n"
                    "<i>Try again in a moment; the catalog will be ready shortly.</i>",
                    _kb_back_year(),
                )
                return

            if not result.get("success"):
                error = result.get("error", "Unknown error")
                await surf.final(
                    f"❌ <b>Portal query failed</b>\n\n"
                    f"Couldn't reach NITRIS to check for papers.\n"
                    f"Error: <code>{html.escape(str(error)[:200])}</code>\n\n"
                    f"<i>Please try again in a moment.</i>",
                    _kb_back_year(),
                )
                return

            parsed_records = result.get("parsed_records", [])

        except NitrisCircuitOpenError:
            await surf.final(ui_copy.CIRCUIT_DOWN, _kb_back_year())
            return
        except RuntimeError as e:
            logger.warning("QP metadata enqueue rejected: %r", e)
            await surf.final(ui_copy.QUEUE_BUSY, _kb_back_year())
            return

        try:
            async with get_db_session() as session:
                exam_service = ExaminationService(session)
                await exam_service.persist_subject_metadata(
                    parsed_records=parsed_records,
                    academic_year=full_year_str,
                    subject_code=subject_code,
                )
                await session.commit()
                mid_cache = await exam_service.get_cached_paper(subject_code, full_year_str, "mid_sem")
                end_cache = await exam_service.get_cached_paper(subject_code, full_year_str, "end_sem")
        except Exception as e:
            logger.error("Failed persisting paper metadata: %r", e)
            await surf.final(
                f"❌ <b>Failed to cache paper metadata</b>\n\n"
                f"Error: <code>{html.escape(str(e)[:200])}</code>",
                _kb_back_year(),
            )
            return

    has_available = (
        (mid_cache and mid_cache.status != "paper_not_available") or
        (end_cache and end_cache.status != "paper_not_available")
    )
    if not has_available:
        await surf.final(
            f"📝 <b>Papers · {esc(subject_code)}</b> — {esc(full_year_str)}\n\n"
            + ui_theme.quote(
                "Nothing here yet.\nNITRIS hasn't uploaded any papers for this "
                "subject and year — normal for lab / 1-credit subjects."
            ),
            _kb_back_year(),
        )
        return

    text = (
        f"📝 <b>Papers · {esc(subject_code)}</b>\n"
        f"<i>{esc(full_year_str)}</i>\n\n"
        + ui_theme.quote(
            "Papers marked 🚀 deliver instantly from Claw cache.\n"
            "<i>Others take ~20s straight from NITRIS.</i>"
        )
    )

    builder = InlineKeyboardBuilder()
    if mid_cache and mid_cache.status != "paper_not_available":
        mid_label = "📝 Mid Sem"
        if mid_cache.status == "paper_available" and mid_cache.telegram_file_id:
            mid_label += " 🚀"
        builder.row(types.InlineKeyboardButton(text=mid_label, callback_data=f"qp_dl_{mid_cache.id}"))
    if end_cache and end_cache.status != "paper_not_available":
        end_label = "📝 End Sem"
        if end_cache.status == "paper_available" and end_cache.telegram_file_id:
            end_label += " 🚀"
        builder.row(types.InlineKeyboardButton(text=end_label, callback_data=f"qp_dl_{end_cache.id}"))
    builder.row(
        types.InlineKeyboardButton(text="◀️ Select Year", callback_data=f"qp_sub_{subject_code}"),
        types.InlineKeyboardButton(text="🏠 Dashboard", callback_data="inbox_back_dashboard"),
    )

    await surf.final(text, builder.as_markup())


def _qp_nav_markup() -> types.InlineKeyboardMarkup:
    """Post-delivery navigation (user contract: buttons after EVERY paper).
    Reuses existing routed callbacks — zero new wiring."""
    b = InlineKeyboardBuilder()
    b.row(types.InlineKeyboardButton(
        text="📚 Back to Papers", callback_data="qp_back_subjects",
    ))
    b.row(types.InlineKeyboardButton(
        text="🏠 Dashboard", callback_data="inbox_back_dashboard",
    ))
    return b.as_markup()


@router.callback_query(F.data.startswith("qp_dl_"))
async def handle_paper_download(callback: types.CallbackQuery, state: FSMContext) -> None:
    if qpaper_registry.qpaper_service is None:
        try:
            await callback.answer("❌ Service not initialized. Restart bot.", show_alert=True)
        except Exception:
            pass
        return

    telegram_id = callback.from_user.id
    try:
        cache_id = int(callback.data.split("_")[-1])
    except (IndexError, ValueError):
        try:
            await callback.answer("This paper link has expired.", show_alert=False)
        except Exception:
            pass
        return

    # PERF F3 — ACK FIRST: the spinner dies immediately, BEFORE any DB work.
    # Neutral text since cached-vs-cold isn't known yet; the later specific
    # answers were second answers on the same query (Telegram ignores those).
    try:
        await callback.answer("⚡ Opening paper…")
    except Exception:
        pass

    # Resolve the requesting student so cold acquisitions run under THEIR OWN
    # NITRIS account (own-creds-first policy; pool is fallback only).
    requester_user_id = None
    async with get_db_session() as session:
        user_repo = UserRepository(session)
        user = await user_repo.get_by_telegram_id(telegram_id)
        if user:
            requester_user_id = user.id

    # Check if paper is already available in cache for instant delivery
    snap = await qpaper_registry.qpaper_service._read_cache(cache_id)
    is_cached = snap and snap[0] == "paper_available" and snap[1]

    if is_cached:
        result: QPResult = await qpaper_registry.qpaper_service.deliver(
            cache_id, telegram_id, requester_user_id=requester_user_id,
            nav_markup=_qp_nav_markup(),
        )
        if result.delivered:
            # Receipt bubble with navigation — the PDF alone used to leave the
            # student stranded with no way back to the menu.
            await callback.message.answer(
                ui_copy.QP_DROPPED,
                reply_markup=_qp_nav_markup(),
                parse_mode=ParseMode.HTML,
            )
        else:
            surf = Surface(await callback.message.answer("⚠️ Processing paper..."))
            await _present_qp_result(surf, result)
        return

    # ONE bubble: acquisition progress -> slow-poke persona -> receipt/error.
    surf = Surface(await callback.message.answer(
        f"📝 <b>Acquiring paper from NITRIS…</b>\n"
        + ui_theme.quote("<i>Big files take a moment. This bubble will update itself.</i>")
    ))
    surf.poke_later(6.0, ui_copy.slow_note("acquiring"))
    result: QPResult = await qpaper_registry.qpaper_service.deliver(
        cache_id, telegram_id, requester_user_id=requester_user_id,
        nav_markup=_qp_nav_markup(),
    )
    await _present_qp_result(surf, result)


@router.callback_query(F.data == "qp_dlall_prompt")
async def handle_qp_download_all_prompt(callback: types.CallbackQuery, state: FSMContext) -> None:
    try:
        await callback.answer()
    except Exception:
        pass

    text = (
        f"📅 <b>Select Academic Year for Batch Download</b>\n\n"
        f"Please select the historical exam year you want to retrieve papers for all your current courses:"
    )

    builder = InlineKeyboardBuilder()
    for code, label in YEAR_MAP.items():
        builder.row(types.InlineKeyboardButton(text=label, callback_data=f"qp_dlall_yr_{code}"))

    builder.row(types.InlineKeyboardButton(text="◀️ Back to Subjects", callback_data="qp_back_subjects"))
    # PERF F1: not-modified-safe render.
    await show(callback.message, text, reply_markup=builder.as_markup())


@router.callback_query(F.data.startswith("qp_dlall_yr_"))
async def handle_qp_download_all_year(callback: types.CallbackQuery, state: FSMContext) -> None:
    """Year tapped → render the Mid/End/Both chooser (does NOT execute yet).

    Previously this launched the full batch immediately; now the student
    picks which exam type they want. Old message bubbles keep working — their
    year buttons land here and get the chooser gracefully.
    """
    if qpaper_registry.qpaper_service is None:
        try:
            await callback.answer("❌ Service not initialized. Restart bot.", show_alert=True)
        except Exception:
            pass
        return

    try:
        await callback.answer()
    except Exception:
        pass

    year_code = callback.data.split("_")[-1]
    selected_year = YEAR_MAP.get(year_code)
    if not selected_year:
        await show(callback.message, "❌ Invalid academic year selected.")
        return

    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(text="📝 Mid Sem only", callback_data=f"qp_dlall_go_{year_code}_m"))
    builder.row(types.InlineKeyboardButton(text="📗 End Sem only", callback_data=f"qp_dlall_go_{year_code}_e"))
    builder.row(types.InlineKeyboardButton(text="📚 Both (everything)", callback_data=f"qp_dlall_go_{year_code}_b"))
    builder.row(types.InlineKeyboardButton(text="◀️ Back to Years", callback_data="qp_dlall_prompt"))

    text = (
        f"📅 <b>{esc(selected_year)}</b> — what do you want?\n\n"
        f"Pick which exam's papers to download for ALL your current subjects."
    )
    # PERF F1: not-modified-safe render.
    await show(callback.message, text, reply_markup=builder.as_markup())


# Exam-type filter suffixes for the batch executor.
_EXAM_FILTER_MAP = {"m": "mid_sem", "e": "end_sem", "b": None}  # None = both
_EXAM_FILTER_LABEL = {"m": "Mid Sem", "e": "End Sem", "b": "All papers"}


@router.callback_query(F.data.startswith("qp_dlall_go_"))
async def handle_qp_download_all_go(callback: types.CallbackQuery, state: FSMContext) -> None:
    if qpaper_registry.qpaper_service is None:
        try:
            await callback.answer("❌ Service not initialized. Restart bot.", show_alert=True)
        except Exception:
            pass
        return

    raw = callback.data.removeprefix("qp_dlall_go_")
    try:
        year_code, suffix = raw.rsplit("_", 1)
    except ValueError:
        try:
            await callback.answer("This button has expired.", show_alert=False)
        except Exception:
            pass
        return

    target_exam = _EXAM_FILTER_MAP.get(suffix)
    type_label = _EXAM_FILTER_LABEL.get(suffix)
    if target_exam is None and suffix != "b":
        try:
            await callback.answer("This button has expired.", show_alert=False)
        except Exception:
            pass
        return

    selected_year = YEAR_MAP.get(year_code)

    # PERF F3 — ACK FIRST: spinner dies before any DB work.
    try:
        await callback.answer("⚡ Starting batch…")
    except Exception:
        pass

    if not selected_year:
        await show(callback.message, "❌ Invalid academic year selected.")
        return

    telegram_id = callback.from_user.id
    status_msg = await callback.message.answer("⏳ Resolving current semester courses...")

    async with get_db_session() as session:
        user_repo = UserRepository(session)
        user = await user_repo.get_by_telegram_id(telegram_id)
        if not user:
            await status_msg.edit_text("❌ You are not registered. Use /start to register.")
            return
        snapshot_repo = SnapshotRepository(session)
        snapshot = await snapshot_repo.get_latest_snapshot(user.id, "attendance")
        if not snapshot or not getattr(snapshot, "snapshot_json", None) or "records" not in snapshot.snapshot_json:
            await status_msg.edit_text(
                "❌ No registered subjects found in your latest attendance snapshot. "
                "Run /attendance first."
            )
            return
        courses = list(snapshot.snapshot_json["records"])
        user_id = user.id

    total_courses = len(courses)
    await status_msg.edit_text(
        f"⏳ Checking paper catalog for {total_courses} subjects..."
    )

    cache_ids_to_deliver: list[int] = []
    uncached_courses: list[dict] = []

    async with get_db_session() as session:
        exam_service = ExaminationService(session)
        types_to_check = ("mid_sem", "end_sem") if target_exam is None else (target_exam,)
        for course in courses:
            sub_code = course.get("subject_code") or ""
            if not sub_code:
                continue
            found_any = False
            for exam_t in types_to_check:
                cache_row = await exam_service.get_cached_paper(sub_code, selected_year, exam_t)
                if cache_row and cache_row.status != "paper_not_available":
                    cache_ids_to_deliver.append(cache_row.id)
                    found_any = True
            # Subject is "uncached" only when the SELECTED type(s) have no
            # usable row — not when merely the other exam type is missing.
            if not found_any:
                uncached_courses.append(course)

    if uncached_courses:
        await status_msg.edit_text(
            f"⏳ Syncing catalogs for {len(uncached_courses)} uncached subjects from NITRIS..."
        )
        all_parsed: list[tuple[str, list]] = []

        from app.nitris.job_queue import nitris_job_queue, Priority
        from app.nitris.gateway import NitrisCircuitOpenError
        from app.services.examination_service import _clean_code

        for course in uncached_courses:
            sub_code = course.get("subject_code") or ""
            if not sub_code:
                continue
            try:
                clean_subj = _clean_code(sub_code)
                dedup_key = f"qp_metadata:{clean_subj}:{selected_year}"

                future = await nitris_job_queue.enqueue(
                    job_type="qp_metadata_fetch",
                    user_id=user.id,
                    priority=Priority.MEDIUM,
                    dedup_key=dedup_key,
                    payload={
                        "academic_year": selected_year,
                        "subject_code": sub_code,
                        "roll_number": user.roll_number,
                    },
                    timeout=90.0,
                )

                try:
                    result = await asyncio.wait_for(future, timeout=90.0)
                    if result.get("success"):
                        records = result.get("parsed_records", [])
                        all_parsed.append((sub_code, records))
                    else:
                        logger.warning(
                            "Batch metadata fetch failed for %s %s: %s",
                            sub_code, selected_year, result.get("error", "unknown"),
                        )
                except asyncio.TimeoutError:
                    logger.warning("Metadata fetch timed out for %s %s", sub_code, selected_year)

            except NitrisCircuitOpenError:
                logger.warning("Circuit open during batch metadata fetch — stopping")
                break
            except Exception as e:
                logger.warning("Batch metadata fetch failed for %s %s: %r", sub_code, selected_year, e)

        if all_parsed:
            async with get_db_session() as session:
                exam_service = ExaminationService(session)
                for sub_code, records in all_parsed:
                    persisted = await exam_service.persist_subject_metadata(
                        parsed_records=records,
                        academic_year=selected_year,
                        subject_code=sub_code,
                    )
                    for rec in persisted:
                        if target_exam is not None and rec.exam_type != target_exam:
                            continue
                        if rec.status != "paper_not_available" and rec.id not in cache_ids_to_deliver:
                            cache_ids_to_deliver.append(rec.id)
                await session.commit()

    if not cache_ids_to_deliver:
        await status_msg.edit_text(
            "ℹ️ <b>No papers available</b> for any of your current subjects "
            f"in <b>{esc(selected_year)}</b> ({esc(type_label)}).",
            reply_markup=_qp_nav_markup(),
            parse_mode=ParseMode.HTML,
        )
        return

    total = len(cache_ids_to_deliver)
    await status_msg.edit_text(f"⏳ Delivering {total} papers — cache hits are instant...")

    succeeded = 0
    not_available = 0
    failed = 0
    errors: list[str] = []

    nav = _qp_nav_markup()
    for i, cache_id in enumerate(cache_ids_to_deliver, start=1):
        result: QPResult = await qpaper_registry.qpaper_service.deliver(
            cache_id, telegram_id, requester_user_id=user_id,
            nav_markup=nav,
        )
        if result.delivered:
            succeeded += 1
        elif result.not_available:
            not_available += 1
        else:
            failed += 1
            if result.error:
                errors.append(f"Paper #{cache_id}: {result.error[:80]}")
        if i % 3 == 0 or i == total:
            try:
                await status_msg.edit_text(
                    f"⏳ Delivering papers — {i}/{total} done "
                    f"({succeeded}✓ {not_available}ℹ️ {failed}✗)"
                )
            except Exception:
                pass
        await asyncio.sleep(0.05)

    summary = (
        f"📋 <b>Batch download complete</b>\n\n"
        f"📅 Year: <b>{esc(selected_year)}</b>\n"
        f"📝 Type: <b>{esc(type_label)}</b>\n"
        f"✅ Delivered: <b>{succeeded}</b>\n"
        f"ℹ️ No paper available: <b>{not_available}</b>\n"
        f"❌ Failed: <b>{failed}</b>\n"
    )
    if errors:
        summary += "\n<b>Errors:</b>\n" + "\n".join(f"• {html.escape(e)}" for e in errors[:5])
        if len(errors) > 5:
            summary += f"\n... and {len(errors) - 5} more"
    await status_msg.edit_text(
        summary,
        reply_markup=_qp_nav_markup(),
        parse_mode=ParseMode.HTML,
    )

    # ── Trailing navigation bubble (user contract) ─────────────────────
    # The summary above edits the ORIGINAL status bubble, which sits ABOVE
    # the whole PDF stream. This bubble lands BELOW the last document so the
    # student always ends the delivery with Back-to-Papers/Dashboard buttons
    # in thumb reach.
    try:
        await callback.message.answer(
            f"📚 <b>{succeeded} paper(s) delivered</b> — {esc(selected_year)}\n"
            f"<i>Use the buttons below to keep browsing.</i>",
            reply_markup=_qp_nav_markup(),
            parse_mode=ParseMode.HTML,
        )
    except Exception as nav_err:
        logger.warning("Post-batch navigation bubble failed: %r", nav_err)


async def _present_qp_result(surf, result: QPResult) -> None:
    """Terminal render of a download attempt onto the SAME bubble."""
    # User contract: EVERY terminal state carries Back-to-Papers + Dashboard
    # so nobody is stranded after a paper (or a failure).
    kb = _qp_nav_markup()
    try:
        if result.delivered:
            # File has landed in the chat; this bubble becomes the receipt.
            await surf.final(ui_copy.QP_DROPPED, kb)
            return

        if result.not_available:
            await surf.final(
                "ℹ️ <b>No paper on NITRIS</b>\n\n"
                + ui_theme.quote(
                    "NITRIS has no paper uploaded for this exam — "
                    "usually a lab / 1-credit subject.\n"
                    "<i>Nothing to download, so Claw won't keep asking the "
                    "portal for it.</i>"
                ),
                kb,
            )
            return

        if result.in_progress:
            await surf.final(
                "⏳ <b>Acquisition in progress</b>\n\n"
                + ui_theme.quote(
                    "Another student is fetching this exact paper right now.\n"
                    "<i>Tap again in ~30s — it'll be instant once cached.</i>"
                ),
                kb,
            )
            return

        if result.permanent:
            await surf.final(
                f"❌ <b>Paper unavailable</b>\n\n"
                + ui_theme.quote(
                    "Couldn't acquire after multiple attempts.\n"
                    f"Reason: <code>{html.escape((result.error or 'unknown')[:300])}</code>\n\n"
                    "<i>Contact support if this persists.</i>"
                ),
                kb,
            )
            return

        await surf.final(
            f"⚠️ <b>Temporary error fetching paper</b>\n\n"
            + ui_theme.quote(
                "Failed just now — worth one more tap.\n"
                f"Error: <code>{html.escape((result.error or 'unknown')[:300])}</code>"
            ),
            kb,
        )
    except Exception as e:
        logger.error("Failed to present QP result to user: %r", e)
        try:
            await surf.final("🦀 Something broke on our side. Your data is safe.", ui_theme.footer_kb())
        except Exception:
            pass


@router.callback_query(F.data == "qp_search_prompt")
async def handle_qp_search_prompt(callback: types.CallbackQuery, state: FSMContext) -> None:
    try:
        await callback.answer()
    except Exception:
        pass

    await state.set_state(QuestionPaperFlow.waiting_for_search_query)

    text = (
        f"🔍 <b>Search Question Papers</b>\n\n"
        f"Please enter a subject code (e.g. <b>BM1002</b>) or a course name keyword (e.g. <b>Chemistry</b>):"
    )

    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(text="🚫 Cancel Search", callback_data="qp_back_subjects"))

    # PERF F1: not-modified-safe render (re-opening search prompt is free).
    await show(callback.message, text, reply_markup=builder.as_markup())


@router.message(QuestionPaperFlow.waiting_for_search_query, F.text)
async def process_qp_search_query(message: types.Message, state: FSMContext) -> None:
    """Processes search queries via the job queue."""
    query = message.text.strip()
    telegram_id = message.from_user.id

    if len(query) < 2:
        await message.answer("❌ Search query is too short. Please enter at least 2 characters:")
        return

    status_msg = await message.answer(f"🔍 Searching for <b>\"{esc(query)}\"</b> on NITRIS portal...")
    surf = Surface(status_msg)
    surf.poke_later(4.0, ui_copy.slow_note("searching NITRIS"))

    if query.startswith("/"):
        await state.clear()
        try:
            await status_msg.delete()
        except Exception:
            pass
        return

    async with get_db_session() as session:
        user_repo = UserRepository(session)
        user = await user_repo.get_by_telegram_id(telegram_id)

        if not user:
            await status_msg.edit_text("❌ You are not registered. Use /start to register.")
            await state.clear()
            return

    from app.nitris.job_queue import nitris_job_queue, Priority
    from app.nitris.gateway import NitrisCircuitOpenError
    from app.nitris.rate_limiter import operation_cooldown, COOLDOWN_PAPERS_SEARCH

    allowed, wait = await operation_cooldown.check(
        user.id, "qp_search", key=query,
        cooldown_seconds=COOLDOWN_PAPERS_SEARCH,
    )
    if not allowed:
        await status_msg.edit_text(
            f"⏳ You just searched for this. Please wait {wait}s before trying again.",
            parse_mode=ParseMode.HTML,
        )
        await state.clear()
        return

    try:
        future = await nitris_job_queue.enqueue(
            job_type="qp_search",
            user_id=user.id,
            priority=Priority.MEDIUM,
            dedup_key=f"qp_search:{query.lower()}",
            payload={"query": query},
        )

        try:
            result = await asyncio.wait_for(future, timeout=90.0)
        except asyncio.TimeoutError:
            await surf.final(
                "⏳ <b>Search is taking longer than expected.</b>\n\n"
                "NITRIS may be slow. Please try again in a moment.",
                ui_theme.footer_kb(),
            )
            await state.clear()
            return

        if not result.get("success"):
            error = result.get("error", "Unknown error")
            await surf.final(
                f"❌ Portal query failed: {html.escape(str(error)[:200])}",
                ui_theme.footer_kb(),
            )
            await state.clear()
            return

        records = result.get("records", [])
        parsed_records = records

    except NitrisCircuitOpenError:
        await surf.final(ui_copy.CIRCUIT_DOWN, ui_theme.footer_kb())
        await state.clear()
        return
    except RuntimeError as e:
        logger.warning("QP search enqueue rejected: %r", e)
        await surf.final(ui_copy.QUEUE_BUSY, ui_theme.footer_kb())
        await state.clear()
        return

    if not parsed_records:
        await surf.final(
            f"🔍 <b>No matches</b>\n\n"
            + ui_theme.quote(
                f'Nothing found for "<b>{esc(query)}</b>".\n'
                "Double-check the subject code or spelling and try again."
            ),
            ui_theme.footer_kb(back_cb="qp_search_prompt", back_text="🔍 Search Again"),
        )
        await state.clear()
        return

    unique_subjects = {}
    for r in parsed_records:
        unique_subjects[r.subject_code] = r.subject_name

    await state.clear()

    if len(unique_subjects) == 1:
        subject_code = list(unique_subjects.keys())[0]

        builder = InlineKeyboardBuilder()
        for code, label in YEAR_MAP.items():
            builder.row(types.InlineKeyboardButton(text=label, callback_data=f"qp_yr_{subject_code}_{code}"))
        builder.row(types.InlineKeyboardButton(text="◀️ Search Menu", callback_data="qp_search_prompt"))
        builder.row(ui_theme.home_button())

        await surf.final(
            f"📅 <b>{esc(subject_code)}</b> · {esc(unique_subjects[subject_code])}\n\n"
            "Pick the exam year:",
            builder.as_markup(),
        )
        return

    text = f"🔍 <b>Matches for \"{esc(query)}\"</b>\n\nPick a subject:\n\n"
    builder = InlineKeyboardBuilder()

    for idx, (code, name) in enumerate(unique_subjects.items(), start=1):
        text += f"<b>{idx}.</b> <code>{esc(code)}</code> | <i>{esc(name)}</i>\n"
        builder.row(types.InlineKeyboardButton(text=f"📚 {code} - {name[:25]}...", callback_data=f"qp_sub_{code}"))

    builder.row(types.InlineKeyboardButton(text="🔍 Search Again", callback_data="qp_search_prompt"))
    builder.row(types.InlineKeyboardButton(text="🏠 Back to Menu", callback_data="qp_back_subjects"))
    builder.row(ui_theme.home_button())

    await surf.final(text, builder.as_markup())


@router.message(QuestionPaperFlow.waiting_for_search_query, ~F.text)
async def qp_search_needs_text(message: types.Message) -> None:
    await message.answer(
        "⚠️ Please send your search as a <b>text message</b>.\n\nSend /cancel to abort.",
        parse_mode=ParseMode.HTML,
    )
