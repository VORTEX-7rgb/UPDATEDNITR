# Snapshot Audit: CollegeClaw / NITRISClaw

This document presents a deep dive architectural and correctness audit of the Snapshot system in the repository, focusing on `SnapshotService` and `SnapshotRepository`.

---

## 1. Hashing & Serialization Determinism

* **Goal**: Guarantee that two identical attendance results generate identical hash signatures, preventing duplicate snapshot inserts and false-positive event detections.
* **Implementation**:
  ```python
  data_dict = attendance_result.to_dict()
  deterministic_json = json.dumps(data_dict, sort_keys=True)
  snapshot_hash = hashlib.sha256(deterministic_json.encode("utf-8")).hexdigest()
  ```
* **Evaluation**:
  1. **`to_dict()` mapping**: Properly extracts nested domain models to standard dictionary layouts, ensuring type consistency (e.g. converting dataclasses to dicts).
  2. **`sort_keys=True`**: Forces python `json.dumps` to sort keys alphabetically. This is critical because Python dictionaries do not guarantee insertion order preservation, so raw serializations of identical dicts could differ in string output. Sorted keys yield absolute stability.
  3. **SHA-256 Hashing**: Extremely robust cryptographic hash, guaranteeing collision resistance and generating standard 64-character hex strings perfectly fitting the table's `snapshot_hash` constraints.

---

## 2. Immutability & History Preservation

* **Design Pattern**: Append-only event mapping.
* **Evaluation**:
  * Instead of mutating a single user snapshot row in-place, the service inserts a new, immutable `Snapshot` row every time a state change occurs.
  * This is a highly robust architectural pattern that preserves historical user data, allowing the system to track attendance changes over time and trace changes chronologically.
  * **Storage considerations**: Attendance data is compact (typically less than 2KB per snapshot). With standard sync intervals, the database storage footprint remains extremely light even over multiple academic semesters.

---

## 3. Duplicate Prevention & Comparison Flow

* **Flow logic**:
  1. Fetch latest snapshot row by `(user_id, module_name)` sorted by `id.desc()`.
  2. If a latest snapshot is present, compare its stored `snapshot_hash` with the calculated `snapshot_hash` of the new data.
  3. **Early return on match**: If hashes are identical, log the match, skip database inserts, and return `(changed=False, latest_snapshot, latest_snapshot)`.
* **Correctness**:
  * Fully prevents duplicate snapshot records.
  * Correctly skips event generation logic entirely on unchanged states, saving database resources and downstream notifications.

---

## 4. Transaction Safety & Atomicity

* **Context Isolation**:
  * `SnapshotService` requires an active `AsyncSession` passed into its constructor, which is shared directly with the initialized `EventService` instance.
  * The snapshot insertion and event detections are executed together inside this session scope:
    ```python
    new_snapshot = await self.snapshot_repo.create_snapshot(...)
    await self.event_service.detect_and_store_changes(..., previous_snapshot, new_snapshot)
    ```
  * **Atomic Consistency**: Because both operations run inside the same `async with session.begin()` transaction boundary:
    * If event generation raises an exception, the snapshot insert is rolled back.
    * If snapshot persistence fails, event creation is rolled back.
    * This prevents mismatched states (e.g., snapshot created without its corresponding events, or events pointing to a non-existent snapshot).
