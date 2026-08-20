"""Examination service — manages previous year paper caches, metadata sync, and downloads.

REFACTORED to NEVER hold a DB session open during slow HTTP work (NITRIS login,
portal search, PDF download). All public methods are split into two phases:

  Phase 1 (HTTP): fetch_*_from_portal() — does NITRIS work, returns parsed data,
                   does NOT touch the DB. Safe to call without an open session.
  Phase 2 (DB):   persist_*() — does DB work only, no HTTP. Uses self.session
                   which the caller must have opened as a SHORT transaction.

Includes persistent NEGATIVE CACHING (QPStatus.PAPER_NOT_AVAILABLE) so subjects
without exam papers (labs, practicals) are NEVER re-queried on NITRIS.
"""

import logging
from typing import Optional
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import QuestionPaperCache, QPStatus
from app.nitris.client import NitrisClient
from app.nitris.examination_parser import parse_question_papers_html, QuestionPaperRecord

logger = logging.getLogger(__name__)


def _clean_code(c: str) -> str:
    """Normalize a subject code: uppercase, no spaces/dashes/underscores."""
    return c.upper().replace(" ", "").replace("-", "").replace("_", "")


class ExaminationService:
    """Orchestrates question paper lookup, database caching, and portal downloads.

    Lifecycle: this service wraps a SHORT-LIVED AsyncSession. Callers should
    construct an instance, do their DB work, then let the session close. Do NOT
    hold an instance across slow HTTP work — use the static fetch_*_from_portal()
    methods instead.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ── DB-only methods (short transactions, no HTTP) ──────────────────────

    async def get_cached_paper(
        self, subject_code: str, academic_year: str, exam_type: str
    ) -> Optional[QuestionPaperCache]:
        """Retrieve a cached question paper record from the database by composite key."""
        clean_code = _clean_code(subject_code)
        stmt = (
            select(QuestionPaperCache)
            .where(QuestionPaperCache.subject_code == clean_code)
            .where(QuestionPaperCache.academic_year == academic_year)
            .where(QuestionPaperCache.exam_type == exam_type)
        )
        res = await self.session.execute(stmt)
        return res.scalar_one_or_none()

    async def create_stub_cache_record(
        self, subject_code: str, academic_year: str, exam_type: str, postback_target: str
    ) -> QuestionPaperCache:
        """Create a new cache entry with the portal postback target."""
        clean_code = _clean_code(subject_code)
        record = QuestionPaperCache(
            subject_code=clean_code,
            academic_year=academic_year,
            exam_type=exam_type,
            portal_postback_target=postback_target,
            telegram_file_id=None,
            status=QPStatus.RETRYABLE_FAILURE.value,
        )
        self.session.add(record)
        return record

    async def update_telegram_file_id(self, cache_id: int, telegram_file_id: str) -> None:
        """Cache the uploaded Telegram file ID for sub-millisecond future retrievals."""
        stmt = (
            update(QuestionPaperCache)
            .where(QuestionPaperCache.id == cache_id)
            .values(telegram_file_id=telegram_file_id, status=QPStatus.PAPER_AVAILABLE.value)
        )
        await self.session.execute(stmt)

    async def persist_subject_metadata(
        self,
        parsed_records: list[QuestionPaperRecord],
        academic_year: str,
        subject_code: str,
    ) -> list[QuestionPaperCache]:
        """Persist parsed NITRIS metadata to the question_paper_caches table.

        Pure DB work — NO HTTP calls. Caller must have an open session AND must
        commit after this returns.

        If no papers exist on NITRIS for this subject/year (e.g. lab/practical subjects),
        explicitly stores rows with status='paper_not_available' so NITRIS is NEVER queried again.

        Args:
            parsed_records: records returned by fetch_subject_metadata_from_portal()
            academic_year: academic year string (e.g. "2025-26/Spring")
            subject_code: subject code (will be normalized)

        Returns:
            list of QuestionPaperCache rows that were created or updated.
        """
        cleaned_subject_code = _clean_code(subject_code)
        target_records = [r for r in parsed_records if _clean_code(r.subject_code) == cleaned_subject_code]

        synced_records: list[QuestionPaperCache] = []

        if not target_records:
            logger.info(
                "No portal matches found for Subject: %s (Normalized: %s), Year: %s — caching as paper_not_available",
                subject_code, cleaned_subject_code, academic_year,
            )
            for ex in ("mid_sem", "end_sem"):
                existing = await self.get_cached_paper(cleaned_subject_code, academic_year, ex)
                if not existing:
                    not_avail = QuestionPaperCache(
                        subject_code=cleaned_subject_code,
                        academic_year=academic_year,
                        exam_type=ex,
                        portal_postback_target="",
                        telegram_file_id=None,
                        status=QPStatus.PAPER_NOT_AVAILABLE.value,
                    )
                    self.session.add(not_avail)
                    synced_records.append(not_avail)
                else:
                    synced_records.append(existing)
            return synced_records

        for r in target_records:
            # 1. Handle Mid Sem
            if r.mid_sem_target:
                existing = await self.get_cached_paper(r.subject_code, academic_year, "mid_sem")
                if not existing:
                    new_rec = await self.create_stub_cache_record(
                        r.subject_code, academic_year, "mid_sem", r.mid_sem_target
                    )
                    synced_records.append(new_rec)
                else:
                    if existing.portal_postback_target != r.mid_sem_target:
                        existing.portal_postback_target = r.mid_sem_target
                    synced_records.append(existing)
            else:
                existing = await self.get_cached_paper(r.subject_code, academic_year, "mid_sem")
                if not existing:
                    not_avail = QuestionPaperCache(
                        subject_code=cleaned_subject_code,
                        academic_year=academic_year,
                        exam_type="mid_sem",
                        portal_postback_target="",
                        telegram_file_id=None,
                        status=QPStatus.PAPER_NOT_AVAILABLE.value,
                    )
                    self.session.add(not_avail)
                    synced_records.append(not_avail)

            # 2. Handle End Sem
            if r.end_sem_target:
                existing = await self.get_cached_paper(r.subject_code, academic_year, "end_sem")
                if not existing:
                    new_rec = await self.create_stub_cache_record(
                        r.subject_code, academic_year, "end_sem", r.end_sem_target
                    )
                    synced_records.append(new_rec)
                else:
                    if existing.portal_postback_target != r.end_sem_target:
                        existing.portal_postback_target = r.end_sem_target
                    synced_records.append(existing)
            else:
                existing = await self.get_cached_paper(r.subject_code, academic_year, "end_sem")
                if not existing:
                    not_avail = QuestionPaperCache(
                        subject_code=cleaned_subject_code,
                        academic_year=academic_year,
                        exam_type="end_sem",
                        portal_postback_target="",
                        telegram_file_id=None,
                        status=QPStatus.PAPER_NOT_AVAILABLE.value,
                    )
                    self.session.add(not_avail)
                    synced_records.append(not_avail)

        return synced_records

    # ── HTTP-only methods (no DB work, no session required) ────────────────

    @staticmethod
    async def fetch_subject_metadata_from_portal(
        username: str,
        password: str,
        academic_year: str,
        subject_code: str,
        client: Optional[NitrisClient] = None,
    ) -> list[QuestionPaperRecord]:
        """Log in to NITRIS, search for the subject, return parsed records.

        Pure HTTP work — NO DB calls. Safe to call without an open session.
        Caller must persist the returned records separately via
        persist_subject_metadata() in a short DB transaction.

        Args:
            username: NITRIS roll number
            password: plaintext password (caller must decrypt)
            academic_year: e.g. "2025-26/Spring"
            subject_code: subject code (will be normalized for search)
            client: optional pre-authenticated NitrisClient (caller manages lifecycle).
                If None, a new client is created + logged in + closed internally.

        Returns:
            list of QuestionPaperRecord (dataclass) — NOT QuestionPaperCache rows.
            Empty list if NITRIS search returned no results.
        """
        cleaned_subject_code = _clean_code(subject_code)
        logger.info(
            "Fetching question paper metadata from portal for Subject: %s (Normalized: %s), Year: %s",
            subject_code, cleaned_subject_code, academic_year,
        )

        local_client = False
        if not client:
            raise RuntimeError(
                "fetch_subject_metadata_from_portal requires a pre-authenticated "
                "client (logged in through the gateway); direct login removed to "
                "enforce the credential-quarantine gate."
            )

        try:
            html = await client.fetch_question_papers(
                academic_year=academic_year, subject_query=cleaned_subject_code
            )
            return parse_question_papers_html(html)
        finally:
            if local_client:
                await client.close()

    # ── Legacy compatibility wrappers (DEPRECATED) ─────────────────────────

    async def sync_subject_papers_metadata(
        self, username: str, password: str, academic_year: str, subject_code: str,
        client: Optional[NitrisClient] = None
    ) -> list[QuestionPaperCache]:
        """DEPRECATED wrapper: fetches metadata via HTTP and persists it."""
        parsed_records = await ExaminationService.fetch_subject_metadata_from_portal(
            username=username,
            password=password,
            academic_year=academic_year,
            subject_code=subject_code,
            client=client,
        )
        return await self.persist_subject_metadata(
            parsed_records=parsed_records,
            academic_year=academic_year,
            subject_code=subject_code,
        )

    async def download_paper_bytes(
        self, username: str, password: str, cache_record: QuestionPaperCache,
        client: Optional[NitrisClient] = None
    ) -> bytes:
        """DEPRECATED wrapper: downloads paper bytes."""
        logger.info(
            "Downloading paper bytes from portal for cache ID: %d (%s %s)",
            cache_record.id, cache_record.subject_code, cache_record.exam_type,
        )
        local_client = False
        if not client:
            raise RuntimeError(
                "download_paper_bytes requires a pre-authenticated client (logged in "
                "through the gateway); direct login removed to enforce the "
                "credential-quarantine gate."
            )

        try:
            pdf_bytes = await client.download_question_paper_bytes(
                academic_year=cache_record.academic_year,
                subject_query=cache_record.subject_code,
                event_target=cache_record.portal_postback_target,
            )
            return pdf_bytes
        finally:
            if local_client:
                await client.close()
