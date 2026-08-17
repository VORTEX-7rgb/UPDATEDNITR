"""Examination service — manages previous year paper caches, metadata sync, and downloads."""

import logging
from datetime import datetime, timezone
from typing import Optional
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import QuestionPaperCache
from app.db.crypto import decrypt_password
from app.nitris.client import NitrisClient
from app.nitris.examination_parser import parse_question_papers_html

logger = logging.getLogger(__name__)


class ExaminationService:
    """Orchestrates question paper lookup, database caching, and portal downloads."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_cached_paper(
        self, subject_code: str, academic_year: str, exam_type: str
    ) -> Optional[QuestionPaperCache]:
        """Retrieve a cached question paper record from the database by composite key, using robust normalization."""
        clean_code = subject_code.upper().replace(" ", "").replace("-", "").replace("_", "")
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
        """Create a new cache entry with the portal postback target and a robustly normalized subject code."""
        clean_code = subject_code.upper().replace(" ", "").replace("-", "").replace("_", "")
        record = QuestionPaperCache(
            subject_code=clean_code,
            academic_year=academic_year,
            exam_type=exam_type,
            portal_postback_target=postback_target,
            telegram_file_id=None
        )
        self.session.add(record)
        return record

    async def update_telegram_file_id(self, cache_id: int, telegram_file_id: str) -> None:
        """Cache the uploaded Telegram file ID for sub-millisecond future retrievals."""
        stmt = (
            update(QuestionPaperCache)
            .where(QuestionPaperCache.id == cache_id)
            .values(telegram_file_id=telegram_file_id)
        )
        await self.session.execute(stmt)

    async def sync_subject_papers_metadata(
        self, username: str, password: str, academic_year: str, subject_code: str,
        client: Optional[NitrisClient] = None
    ) -> list[QuestionPaperCache]:
        """Log in to NITRIS, search for the subject, parse results, and write postback targets to cache.
        
        Returns all QuestionPaperCache records for this subject and year.
        """
        def clean_code(c: str) -> str:
            return c.upper().replace(" ", "").replace("-", "").replace("_", "")
            
        cleaned_subject_code = clean_code(subject_code)
        logger.info("Syncing question paper metadata from portal for Subject: %s (Normalized: %s), Year: %s", subject_code, cleaned_subject_code, academic_year)
        
        local_client = False
        if not client:
            client = NitrisClient()
            await client.login(username, password)
            local_client = True
            
        try:
            # Fetch search results using cleaned subject code for maximum portal search reliability
            html = await client.fetch_question_papers(academic_year=academic_year, subject_query=cleaned_subject_code)
            parsed_records = parse_question_papers_html(html)
        finally:
            if local_client:
                await client.close()

        synced_records: list[QuestionPaperCache] = []
        
        # Filter for exact subject match using robust normalized comparison
        target_records = [r for r in parsed_records if clean_code(r.subject_code) == cleaned_subject_code]
        
        if not target_records:
            logger.warning("No portal matches found for Subject: %s (Normalized: %s), Year: %s", subject_code, cleaned_subject_code, academic_year)
            return []

        for r in target_records:
            # 1. Handle Mid Sem if link exists
            if r.mid_sem_target:
                existing = await self.get_cached_paper(r.subject_code, academic_year, "mid_sem")
                if not existing:
                    new_rec = await self.create_stub_cache_record(
                        r.subject_code, academic_year, "mid_sem", r.mid_sem_target
                    )
                    synced_records.append(new_rec)
                else:
                    # Update target dynamically if it shifted
                    if existing.portal_postback_target != r.mid_sem_target:
                        existing.portal_postback_target = r.mid_sem_target
                    synced_records.append(existing)

            # 2. Handle End Sem if link exists
            if r.end_sem_target:
                existing = await self.get_cached_paper(r.subject_code, academic_year, "end_sem")
                if not existing:
                    new_rec = await self.create_stub_cache_record(
                        r.subject_code, academic_year, "end_sem", r.end_sem_target
                    )
                    synced_records.append(new_rec)
                else:
                    # Update target dynamically if it shifted
                    if existing.portal_postback_target != r.end_sem_target:
                        existing.portal_postback_target = r.end_sem_target
                    synced_records.append(existing)

        return synced_records

    async def download_paper_bytes(
        self, username: str, password: str, cache_record: QuestionPaperCache,
        client: Optional[NitrisClient] = None
    ) -> bytes:
        """Submit the cached postback target to the portal and retrieve the raw PDF bytes."""
        logger.info(
            "Downloading PDF bytes from portal for cache ID: %d (%s %s)",
            cache_record.id, cache_record.subject_code, cache_record.exam_type
        )
        local_client = False
        if not client:
            client = NitrisClient()
            await client.login(username, password)
            local_client = True
            
        try:
            pdf_bytes = await client.download_question_paper_pdf(
                academic_year=cache_record.academic_year,
                subject_query=cache_record.subject_code,
                event_target=cache_record.portal_postback_target
            )
            return pdf_bytes
        finally:
            if local_client:
                await client.close()
