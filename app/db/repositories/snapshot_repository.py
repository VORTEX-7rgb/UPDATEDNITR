"""Snapshot persistence repository using SQLAlchemy async sessions."""

import logging
from typing import Optional, Any
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Snapshot

logger = logging.getLogger(__name__)


class SnapshotRepository:
    """Manages database persistence for the Snapshot model."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create_snapshot(
        self, user_id: int, module_name: str, snapshot_json: dict[str, Any], snapshot_hash: str
    ) -> Snapshot:
        """Create and store a new immutable snapshot of a module's state."""
        logger.debug("Creating new snapshot for user_id=%s, module='%s'", user_id, module_name)
        
        snapshot = Snapshot(
            user_id=user_id,
            module_name=module_name,
            snapshot_json=snapshot_json,
            snapshot_hash=snapshot_hash,
        )
        
        self.session.add(snapshot)
        await self.session.flush()
        return snapshot

    async def get_latest_snapshot(
        self, user_id: int, module_name: str, for_update: bool = False
    ) -> Optional[Snapshot]:
        """Fetch the single latest snapshot for a user and module by ID."""
        stmt = (
            select(Snapshot)
            .where(Snapshot.user_id == user_id, Snapshot.module_name == module_name)
            .order_by(Snapshot.id.desc())
            .limit(1)
        )
        if for_update:
            bind = self.session.get_bind()
            if bind.dialect.name != "sqlite":
                stmt = stmt.with_for_update()
                
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()
