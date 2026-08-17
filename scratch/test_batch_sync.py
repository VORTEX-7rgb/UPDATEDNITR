import os
import sys
import asyncio
import logging
from sqlalchemy import select

# Add app directory to sys.path to resolve imports correctly
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.db.database import get_db_session
from app.db.models import User
from app.db.crypto import decrypt_password
from app.db.repositories.snapshot_repository import SnapshotRepository
from app.services.examination_service import ExaminationService

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

async def test_batch():
    async with get_db_session() as session:
        # Get active user
        stmt = select(User).where(User.roll_number == "725MN1011")
        res = await session.execute(stmt)
        user = res.scalars().first()
        if not user:
            logger.error("User not found!")
            return
            
        logger.info("Found User: %s (ID: %d)", user.roll_number, user.id)
        
        # Get latest attendance snapshot
        snapshot_repo = SnapshotRepository(session)
        snapshot = await snapshot_repo.get_latest_snapshot(user.id, "attendance")
        
        if not snapshot:
            logger.error("No snapshot found!")
            return
            
        logger.info("Snapshot modules: %s", snapshot.module_name)
        courses = snapshot.snapshot_json.get("records", [])
        logger.info("Courses count: %d", len(courses))
        
        # Sync metadata
        exam_service = ExaminationService(session)
        password = decrypt_password(user.encrypted_password)
        current_year_str = "2025-26/Spring"
        
        for idx, course in enumerate(courses[:2], start=1): # test first 2 courses
            code = course.get("subject_code", "Unknown")
            logger.info("Syncing catalog for code: %s", code)
            try:
                records = await exam_service.sync_subject_papers_metadata(
                    username=user.roll_number,
                    password=password,
                    academic_year=current_year_str,
                    subject_code=code
                )
                await session.commit()
                logger.info("  Synced %d records successfully for %s", len(records), code)
            except Exception as e:
                logger.error("  Sync failed for %s: %r", code, e)

if __name__ == "__main__":
    asyncio.run(test_batch())
