"""Event detection service — semantic diff engine for snapshot changes."""

import logging
from typing import Optional, Any
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Snapshot, Event, EventType
from app.db.repositories.event_repository import EventRepository

logger = logging.getLogger(__name__)


class EventService:
    """Orchestrates delta detection between snapshots and event creation."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.event_repo = EventRepository(session)

    @staticmethod
    def _safe_int(val: Any, default: int = 0) -> int:
        """Safely convert a string/object to integer, handling empty, non-numeric, or dash characters."""
        if val is None:
            return default
        val_str = str(val).strip()
        if not val_str:
            return default
        try:
            return int(val_str)
        except ValueError:
            digits = "".join(c for c in val_str if c.isdigit() or c == '-')
            if digits:
                try:
                    return int(digits)
                except ValueError:
                    pass
            return default

    async def detect_and_store_changes(
        self, user_id: int, previous_snapshot: Optional[Snapshot], new_snapshot: Snapshot
    ) -> list[Event]:
        """Detect and store differences between snapshots inside the Event Catalog.
        
        If previous_snapshot is None, treats all records as new subjects.
        Returns the list of created Event objects.
        """
        logger.info("Detecting changes for user_id=%s, snapshot_id=%s", user_id, new_snapshot.id)
        
        events_created: list[Event] = []
        new_records = new_snapshot.snapshot_json.get("records", [])

        # Case 1: First sync (No previous snapshot)
        if not previous_snapshot:
            logger.info("First snapshot detected for user_id=%s. Logging all records as new subjects.", user_id)
            for rec in new_records:
                payload = {
                    "subject_code": rec.get("subject_code", "Unknown"),
                    "subject_name": rec.get("subject_name", "Unknown"),
                    "faculty": rec.get("faculty", ""),
                    "tc": rec.get("tc", "0"),
                    "ua": rec.get("ua", "0"),
                }
                event = await self.event_repo.create_event(
                    user_id=user_id,
                    event_type=EventType.NEW_SUBJECT_ADDED,
                    payload_json=payload,
                )
                events_created.append(event)
            return events_created

        # Case 2: Comparative sync (Previous snapshot exists)
        prev_records = previous_snapshot.snapshot_json.get("records", [])
        
        # Build subject code mappings
        prev_map = {r.get("subject_code"): r for r in prev_records if r.get("subject_code")}
        new_map = {r.get("subject_code"): r for r in new_records if r.get("subject_code")}

        for sub_code, new_rec in new_map.items():
            sub_name = new_rec.get("subject_name", "Unknown")
            
            # Sub-case A: New subject added in registration
            if sub_code not in prev_map:
                payload = {
                    "subject_code": sub_code,
                    "subject_name": sub_name,
                    "faculty": new_rec.get("faculty", ""),
                    "tc": new_rec.get("tc", "0"),
                    "ua": new_rec.get("ua", "0"),
                }
                event = await self.event_repo.create_event(
                    user_id=user_id,
                    event_type=EventType.NEW_SUBJECT_ADDED,
                    payload_json=payload,
                )
                events_created.append(event)
                continue

            # Sub-case B: Compare existing subject records
            prev_rec = prev_map[sub_code]
            
            changes = {}
            for field in ("tc", "ua", "le", "oa"):
                old_val = prev_rec.get(field, "0")
                new_val = new_rec.get(field, "0")
                if old_val != new_val:
                    changes[field] = {"old": old_val, "new": new_val}

            if changes:
                # 1. Store standard update event
                payload = {
                    "subject_code": sub_code,
                    "subject_name": sub_name,
                    "changes": changes,
                }
                event = await self.event_repo.create_event(
                    user_id=user_id,
                    event_type=EventType.ATTENDANCE_UPDATED,
                    payload_json=payload,
                )
                events_created.append(event)

                # 2. Store specific absence warnings if unauthorized absence (UA) increased
                old_ua = self._safe_int(prev_rec.get("ua", "0"))
                new_ua = self._safe_int(new_rec.get("ua", "0"))
                if new_ua > old_ua:
                    absence_payload = {
                        "subject_code": sub_code,
                        "subject_name": sub_name,
                        "old_ua": str(old_ua),
                        "new_ua": str(new_ua),
                        "total_classes": new_rec.get("tc", "0"),
                    }
                    absence_event = await self.event_repo.create_event(
                        user_id=user_id,
                        event_type=EventType.NEW_ABSENCE_DETECTED,
                        payload_json=absence_payload,
                    )
                    events_created.append(absence_event)

        logger.info("Change detection complete. Created %d events.", len(events_created))
        return events_created
