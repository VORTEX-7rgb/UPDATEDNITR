"""Snapshot persistence repository using SQLAlchemy async sessions."""

import logging
from typing import Optional, Any
from sqlalchemy import delete, func, select
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

    async def purge_superseded_batch(self, keep_per_key: int = 10, limit: int = 5000) -> int:
        """Delete ONE batch of snapshots ranked older than ``keep_per_key`` per (user, module).

        Set-based: rows are ranked by ROW_NUMBER() OVER (PARTITION BY user_id,
        module_name ORDER BY id DESC); anything past the keep-window is a victim.
        Nothing is materialized in Python — the ranking runs entirely inside the
        database. Callers own transaction boundaries; run repeatedly until it
        returns less than ``limit`` to drain fully (see RetentionService).
        """
        if keep_per_key < 1 or limit < 1:
            return 0
        ranked = (
            select(
                Snapshot.id,
                func.row_number()
                .over(
                    partition_by=[Snapshot.user_id, Snapshot.module_name],
                    order_by=Snapshot.id.desc(),
                )
                .label("rn"),
            )
            .subquery()
        )
        victim_ids = select(ranked.c.id).where(ranked.c.rn > keep_per_key).limit(limit)
        result = await self.session.execute(
            delete(Snapshot).where(Snapshot.id.in_(victim_ids))
        )
        deleted = result.rowcount or 0
        if deleted:
            logger.info("Retention: purged %d superseded snapshot(s) this batch.", deleted)
        return deleted
