"""Snapshot service — manages deterministic serialization, hashing, and state validation."""

import json
import hashlib
import logging
from typing import Optional, Tuple
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Snapshot
from app.db.repositories.snapshot_repository import SnapshotRepository
from app.services.event_service import EventService
from app.nitris.parser import AttendanceResult

logger = logging.getLogger(__name__)


class SnapshotService:
    """Manages serialization, hash comparison, and snapshot persistence workflows."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.snapshot_repo = SnapshotRepository(session)
        self.event_service = EventService(session)

    async def create_snapshot_if_changed(
        self, user_id: int, module_name: str, attendance_result: AttendanceResult,
        baseline: bool = False,
    ) -> Tuple[bool, Optional[Snapshot], Snapshot]:
        """Verify, hash, and persist snapshot data if state changes are detected.
        
        Runs state comparisons, snapshot creation, and event mapping atomically
        within the active database transaction context.
        
        Returns:
            Tuple[changed, previous_snapshot, current_snapshot]
        """
        # 1. Convert domain model to dict and serialize to deterministic, key-sorted JSON
        data_dict = attendance_result.to_dict()
        deterministic_json = json.dumps(data_dict, sort_keys=True)
        
        # 2. Generate SHA-256 hash of the sorted JSON payload
        snapshot_hash = hashlib.sha256(deterministic_json.encode("utf-8")).hexdigest()
        
        # Serialize concurrent first-snapshot creation per (user, module). A plain
        # FOR UPDATE on get_latest_snapshot cannot lock a row that does not exist
        # yet, so two concurrent FIRST syncs would both see "no prior snapshot" and
        # both insert + both fire events. This transaction-scoped advisory lock
        # closes that window (auto-released on commit/rollback).
        await self.session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
            {"key": f"snapshot:{user_id}:{module_name}"},
        )

        # 3. Retrieve the single latest snapshot for comparisons, using a write lock
        latest_snapshot = await self.snapshot_repo.get_latest_snapshot(user_id, module_name, for_update=True)
        
        if latest_snapshot:
            # 4. Compare hash signatures first
            if latest_snapshot.snapshot_hash == snapshot_hash:
                logger.info(
                    "State unchanged for user_id=%s, module='%s' (hash matches). Skipping creation.",
                    user_id, module_name
                )
                return False, latest_snapshot, latest_snapshot
            
            logger.info(
                "State change detected for user_id=%s, module='%s'. Old hash: %s..., New hash: %s...",
                user_id, module_name, latest_snapshot.snapshot_hash[:8], snapshot_hash[:8]
            )
            previous_snapshot = latest_snapshot
        else:
            logger.info("No prior snapshots found for user_id=%s, module='%s'. This is a new record.", user_id, module_name)
            previous_snapshot = None

        # 5. Persist the new immutable snapshot (flushes inside active transaction)
        new_snapshot = await self.snapshot_repo.create_snapshot(
            user_id=user_id,
            module_name=module_name,
            snapshot_json=data_dict,
            snapshot_hash=snapshot_hash,
        )

        # 6. Delegate change detection and event storage
        # This operates inside the same session context, maintaining absolute atomic consistency
        await self.event_service.detect_and_store_changes(
            user_id=user_id,
            previous_snapshot=previous_snapshot,
            new_snapshot=new_snapshot,
            baseline=baseline,
        )

        return True, previous_snapshot, new_snapshot
