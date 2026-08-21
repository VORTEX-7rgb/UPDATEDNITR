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

logger = logging.getLogger(__name__)

router = Router(name="papers_router")

YEAR_MAP = {
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
        "📚 <b>Previous Year Question Papers</b>\n\n"
        "Here are your registered courses for the current semester. "
        "Select one below to find historical exam papers, or search for other subjects:\n\n"
    )

    builder = InlineKeyboardBuilder()

    if courses:
        for idx, course in enumerate(courses, start=1):
            code = course.get("subject_code", "Unknown")
            name = course.get("subject_name", "Unknown")
            text += f"<b>{idx}.</b> <code>{esc(code)}</code> | <i>{esc(name)}</i>\n"
            builder.row(types.InlineKeyboardButton(text=f"📚 {code} - {name[:25]}...", callback_data=f"qp_sub_{code}"))
        text += "\n"
    else:
        text += "<i>No registered courses found in your attendance snapshot. Use /attendance to update them!</i>\n\n"

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

    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode=ParseMode.HTML)


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

    async with get_db_session() as session:
        user_repo = UserRepository(session)
        user = await user_repo.get_by_telegram_id(telegram_id)

        if not user:
            await status_msg.edit_text("❌ You are not registered. Use /start to register.")
            return

        exam_service = ExaminationService(session)
        mid_cache = await exam_service.get_cached_paper(subject_code, full_year_str, "mid_sem")
        end_cache = await exam_service.get_cached_paper(subject_code, full_year_str, "end_sem")

    if not mid_cache and not end_cache:
        from app.nitris.job_queue import nitris_job_queue, Priority
        from app.nitris.gateway import NitrisCircuitOpenError
        from app.services.examination_service import _clean_code

        clean_subj = _clean_code(subject_code)
        dedup_key = f"qp_metadata:{clean_subj}:{full_year_str}"

        await status_msg.edit_text(
            "⏳ <b>Fetching paper metadata from NITRIS...</b>\n\n"
            "<i>If other students are requesting the same paper, this request "
            "is being shared with them to avoid hammering the portal.</i>",
            parse_mode=ParseMode.HTML,
        )

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
                await status_msg.edit_text(
                    "⏳ <b>Metadata fetch is taking longer than expected.</b>\n\n"
                    "NITRIS may be slow. Please try again in a moment — your request "
                    "is queued and will complete shortly.",
                    parse_mode=ParseMode.HTML,
                )
                return

            if not result.get("success"):
                error = result.get("error", "Unknown error")
                await status_msg.edit_text(
                    f"❌ <b>Portal query failed</b>\n\n"
                    f"Couldn't reach NITRIS to check for papers.\n"
                    f"Error: <code>{html.escape(str(error)[:200])}</code>\n\n"
                    f"Please try again in a moment.",
                    parse_mode=ParseMode.HTML,
                )
                return

            parsed_records = result.get("parsed_records", [])

        except NitrisCircuitOpenError:
            await status_msg.edit_text(
                "⚠️ <b>NITRIS is temporarily unavailable.</b>\n\n"
                "The system is protecting the portal from overload. "
                "Please try again in ~60 seconds.",
                parse_mode=ParseMode.HTML,
            )
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
            await status_msg.edit_text(
                f"❌ <b>Failed to cache paper metadata</b>\n\n"
                f"Error: <code>{html.escape(str(e)[:200])}</code>",
                parse_mode=ParseMode.HTML,
            )
            return

    has_available = (
        (mid_cache and mid_cache.status != "paper_not_available") or
        (end_cache and end_cache.status != "paper_not_available")
    )
    if not has_available:
        await status_msg.edit_text(
            f"ℹ️ <b>No paper available</b>\n\n"
            f"📖 Subject: <b>{esc(subject_code)}</b>\n"
            f"📅 Year: <b>{esc(full_year_str)}</b>\n\n"
            f"NITRIS portal confirmed no question papers are uploaded for this "
            f"subject and year. This is normal for lab / 1-credit subjects.",
            parse_mode=ParseMode.HTML,
        )
        return

    try:
        await status_msg.delete()
    except Exception:
        pass

    text = (
        f"📝 <b>Download Question Papers</b>\n\n"
        f"📖 Subject: <b>{esc(subject_code)}</b>\n"
        f"📅 Session: <b>{esc(full_year_str)}</b>\n\n"
        f"Tap a paper to download. Already-cached papers deliver instantly."
    )

    builder = InlineKeyboardBuilder()
    if mid_cache and mid_cache.status != "paper_not_available":
        mid_label = "📝 Download Mid Sem"
        if mid_cache.status == "paper_available" and mid_cache.telegram_file_id:
            mid_label += " 🚀"
        builder.row(types.InlineKeyboardButton(text=mid_label, callback_data=f"qp_dl_{mid_cache.id}"))
    if end_cache and end_cache.status != "paper_not_available":
        end_label = "📝 Download End Sem"
        if end_cache.status == "paper_available" and end_cache.telegram_file_id:
            end_label += " 🚀"
        builder.row(types.InlineKeyboardButton(text=end_label, callback_data=f"qp_dl_{end_cache.id}"))
    builder.row(
        types.InlineKeyboardButton(text="◀️ Select Year", callback_data=f"qp_sub_{subject_code}"),
        types.InlineKeyboardButton(text="🏠 Dashboard", callback_data="inbox_back_dashboard"),
    )

    await callback.message.answer(text, reply_markup=builder.as_markup(), parse_mode=ParseMode.HTML)


@router.callback_query(F.data.startswith("qp_dl_"))
async def handle_paper_download(callback: types.CallbackQuery, state: FSMContext) -> None:
    if qpaper_registry.qpaper_service is None:
        try:
            await callback.answer("❌ Service not initialized. Restart bot.", show_alert=True)
        except Exception:
            pass
        return

    telegram_id = callback.from_user.id
    cache_id = int(callback.data.split("_")[-1])

    # Check if paper is already available in cache for instant delivery
    snap = await qpaper_registry.qpaper_service._read_cache(cache_id)
    is_cached = snap and snap[0] == "paper_available" and snap[1]

    if is_cached:
        try:
            await callback.answer("🚀 Delivering cached paper...")
        except Exception:
            pass
        result: QPResult = await qpaper_registry.qpaper_service.deliver(cache_id, telegram_id)
        if not result.delivered:
            status_msg = await callback.message.answer("⚠️ Processing paper...")
            await _present_qp_result(status_msg, result)
        return

    try:
        await callback.answer("⏳ Fetching from portal...")
    except Exception:
        pass

    status_msg = await callback.message.answer("⏳ Acquiring paper from NITRIS portal...")
    result: QPResult = await qpaper_registry.qpaper_service.deliver(cache_id, telegram_id)
    await _present_qp_result(status_msg, result)


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
    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode=ParseMode.HTML)


@router.callback_query(F.data.startswith("qp_dlall_yr_"))
async def handle_qp_download_all_year(callback: types.CallbackQuery, state: FSMContext) -> None:
    if qpaper_registry.qpaper_service is None:
        try:
            await callback.answer("❌ Service not initialized. Restart bot.", show_alert=True)
        except Exception:
            pass
        return

    telegram_id = callback.from_user.id
    year_code = callback.data.split("_")[-1]
    selected_year = YEAR_MAP.get(year_code)

    try:
        await callback.answer()
    except Exception:
        pass

    if not selected_year:
        await callback.message.answer("❌ Invalid academic year selected.")
        return

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
        for course in courses:
            sub_code = course.get("subject_code") or ""
            if not sub_code:
                continue
            mid_cache = await exam_service.get_cached_paper(sub_code, selected_year, "mid_sem")
            end_cache = await exam_service.get_cached_paper(sub_code, selected_year, "end_sem")
            if mid_cache and mid_cache.status != "paper_not_available":
                cache_ids_to_deliver.append(mid_cache.id)
            if end_cache and end_cache.status != "paper_not_available":
                cache_ids_to_deliver.append(end_cache.id)
            if not mid_cache and not end_cache:
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
                        if rec.status != "paper_not_available" and rec.id not in cache_ids_to_deliver:
                            cache_ids_to_deliver.append(rec.id)
                await session.commit()

    if not cache_ids_to_deliver:
        await status_msg.edit_text(
            "ℹ️ <b>No papers available</b> for any of your current subjects "
            f"in <b>{esc(selected_year)}</b>.",
            parse_mode=ParseMode.HTML,
        )
        return

    total = len(cache_ids_to_deliver)
    await status_msg.edit_text(f"⏳ Delivering {total} papers — cache hits are instant...")

    succeeded = 0
    not_available = 0
    failed = 0
    errors: list[str] = []

    for i, cache_id in enumerate(cache_ids_to_deliver, start=1):
        result: QPResult = await qpaper_registry.qpaper_service.deliver(cache_id, telegram_id)
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
        f"✅ Delivered: <b>{succeeded}</b>\n"
        f"ℹ️ No paper available: <b>{not_available}</b>\n"
        f"❌ Failed: <b>{failed}</b>\n"
    )
    if errors:
        summary += "\n<b>Errors:</b>\n" + "\n".join(f"• {html.escape(e)}" for e in errors[:5])
        if len(errors) > 5:
            summary += f"\n... and {len(errors) - 5} more"
    await status_msg.edit_text(summary, parse_mode=ParseMode.HTML)


async def _present_qp_result(status_msg: types.Message, result: QPResult) -> None:
    try:
        if result.delivered:
            try:
                await status_msg.delete()
            except Exception:
                pass
            return

        if result.not_available:
            await status_msg.edit_text(
                "ℹ️ <b>Paper not uploaded</b>\n\n"
                "NITRIS has no downloadable question paper for this exam type right now.\n"
                "This is usually because it hasn't been uploaded yet (or it's a lab / "
                "1-credit subject with no paper).\n\n"
                "It will be re-checked automatically — no need to keep tapping.",
                parse_mode=ParseMode.HTML,
            )
            return

        if result.in_progress:
            await status_msg.edit_text(
                "⏳ <b>Acquisition in progress</b>\n\n"
                "Another student is currently fetching this paper from NITRIS. "
                "Tap the button again in ~30 seconds — it will deliver instantly "
                "once cached.",
                parse_mode=ParseMode.HTML,
            )
            return

        if result.permanent:
            await status_msg.edit_text(
                f"❌ <b>Paper unavailable</b>\n\n"
                f"This paper could not be acquired after multiple attempts.\n"
                f"Reason: <code>{html.escape(result.error or 'unknown')[:300]}</code>\n\n"
                f"Contact support if this persists.",
                parse_mode=ParseMode.HTML,
            )
            return

        await status_msg.edit_text(
            f"⚠️ <b>Temporary error fetching paper</b>\n\n"
            f"The system failed to fetch this paper right now. Please try again.\n"
            f"Error: <code>{html.escape(result.error or 'unknown')[:300]}</code>",
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        logger.error("Failed to present QP result to user: %r", e)
        try:
            await status_msg.edit_text("❌ Internal error. Please try again.")
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

    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode=ParseMode.HTML)


@router.message(QuestionPaperFlow.waiting_for_search_query, F.text)
async def process_qp_search_query(message: types.Message, state: FSMContext) -> None:
    """Processes search queries via the job queue."""
    query = message.text.strip()
    telegram_id = message.from_user.id

    if len(query) < 2:
        await message.answer("❌ Search query is too short. Please enter at least 2 characters:")
        return

    status_msg = await message.answer(f"🔍 Searching for <b>\"{esc(query)}\"</b> on NITRIS portal...")

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
            await status_msg.edit_text(
                "⏳ <b>Search is taking longer than expected.</b>\n\n"
                "NITRIS may be slow. Please try again in a moment.",
                parse_mode=ParseMode.HTML,
            )
            await state.clear()
            return

        if not result.get("success"):
            error = result.get("error", "Unknown error")
            await status_msg.edit_text(
                f"❌ Portal query failed: {html.escape(str(error)[:200])}",
                parse_mode=ParseMode.HTML,
            )
            await state.clear()
            return

        records = result.get("records", [])
        parsed_records = records

    except NitrisCircuitOpenError:
        await status_msg.edit_text(
            "⚠️ <b>NITRIS is temporarily unavailable.</b>\n\n"
            "The system is protecting the portal from overload. "
            "Please try again in ~60 seconds.",
            parse_mode=ParseMode.HTML,
        )
        await state.clear()
        return

    if not parsed_records:
        builder = InlineKeyboardBuilder()
        builder.row(types.InlineKeyboardButton(text="🔍 Search Again", callback_data="qp_search_prompt"))
        builder.row(types.InlineKeyboardButton(text="◀️ Back to Menu", callback_data="qp_back_subjects"))

        await status_msg.edit_text(
            f"🔍 <b>Search Results</b>\n\n"
            f"No matching subjects found on NITRIS for: \"<b>{esc(query)}</b>\".\n\n"
            f"Please verify the subject code or course spelling and try again.",
            reply_markup=builder.as_markup(),
            parse_mode=ParseMode.HTML
        )
        await state.clear()
        return

    unique_subjects = {}
    for r in parsed_records:
        unique_subjects[r.subject_code] = r.subject_name

    await state.clear()

    if len(unique_subjects) == 1:
        subject_code = list(unique_subjects.keys())[0]
        try:
            await status_msg.delete()
        except Exception:
            pass

        builder = InlineKeyboardBuilder()
        for code, label in YEAR_MAP.items():
            builder.row(types.InlineKeyboardButton(text=label, callback_data=f"qp_yr_{subject_code}_{code}"))
        builder.row(types.InlineKeyboardButton(text="◀️ Search Menu", callback_data="qp_search_prompt"))

        await message.answer(
            f"📅 <b>Select Academic Year</b>\n\n"
            f"Subject: <b>{esc(subject_code)} - {esc(unique_subjects[subject_code])}</b>\n\n"
            f"Please select the historical exam year you want to retrieve papers for:",
            reply_markup=builder.as_markup(),
            parse_mode=ParseMode.HTML
        )
        return

    try:
        await status_msg.delete()
    except Exception:
        pass

    text = f"🔍 <b>Search Results for \"{esc(query)}\"</b>\n\nSelect a subject from the matches below:\n\n"
    builder = InlineKeyboardBuilder()

    for idx, (code, name) in enumerate(unique_subjects.items(), start=1):
        text += f"<b>{idx}.</b> <code>{esc(code)}</code> | <i>{esc(name)}</i>\n"
        builder.row(types.InlineKeyboardButton(text=f"📚 {code} - {name[:25]}...", callback_data=f"qp_sub_{code}"))

    builder.row(types.InlineKeyboardButton(text="🔍 Search Again", callback_data="qp_search_prompt"))
    builder.row(types.InlineKeyboardButton(text="🏠 Back to Menu", callback_data="qp_back_subjects"))

    await message.answer(text, reply_markup=builder.as_markup(), parse_mode=ParseMode.HTML)


@router.message(QuestionPaperFlow.waiting_for_search_query, ~F.text)
async def qp_search_needs_text(message: types.Message) -> None:
    await message.answer(
        "⚠️ Please send your search as a <b>text message</b>.\n\nSend /cancel to abort.",
        parse_mode=ParseMode.HTML,
    )
