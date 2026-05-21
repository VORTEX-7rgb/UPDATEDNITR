# Database Review: CollegeClaw / NITRISClaw

This document provides a comprehensive database audit and review of the persistence layers, models, repositories, and migration schemas of the project.

---

## 1. Schema, Keys, & Integrity Constraints

### A. Core Tables & Foreign Keys
All relationships and schema structures strictly map to standard third normal form (3NF) persistence targets:
1. **`users` Table**: Master table storing student roll numbers and encrypted passwords.
2. **`snapshots` Table**: Periodic snapshots with `user_id` pointing to `users.id` with `ondelete='CASCADE'`.
3. **`events` Table**: Delta changes tracked with `user_id` pointing to `users.id` with `ondelete='CASCADE'`.
4. **`sync_states` Table**: Performance metrics with `user_id` pointing to `users.id` with `ondelete='CASCADE'`.

* **Cascading Integrity**: Defining `ondelete='CASCADE'` on all child table schemas ensures that when a user registers again or is deleted, all their matching events, snapshots, and sync states are deleted atomically at the database engine level. This completely prevents orphaned rows.

---

## 2. Index Optimization Strategy

### A. Current Index Catalog
The following indexes are defined:
* `ix_users_roll_number` on `users.roll_number`
* `ix_snapshots_snapshot_hash` on `snapshots.snapshot_hash`
* `ix_snapshots_user_id` on `snapshots.user_id`
* `ix_events_sent` on `events.sent`
* `ix_events_user_id` on `events.user_id`
* `ix_sync_states_user_id` on `sync_states.user_id` (Unique index)
* `idx_snapshots_user_module` (Composite) on `snapshots (user_id, module_name)`
* `idx_events_user_sent` (Composite) on `events (user_id, sent)`

### B. Indexing Efficiency Analysis
1. **Latest Snapshot Lookup**:
   ```sql
   SELECT * FROM snapshots 
   WHERE user_id = :user_id AND module_name = :module_name 
   ORDER BY id DESC LIMIT 1;
   ```
   * *Index Coverage*: Covered perfectly by composite index `idx_snapshots_user_module`. This index allows the engine to jump directly to the user's module snapshots, completely bypassing full table scans.
2. **Unsent Events Batching**:
   ```sql
   SELECT * FROM events 
   WHERE sent = FALSE 
   ORDER BY created_at ASC LIMIT 50;
   ```
   * *Index Coverage*: Employs the single-column index `ix_events_sent` on `sent`. Since the vast majority of historical events are marked as `sent = True`, the search space for `sent = False` is highly selective, making the index query extremely cheap.

---

## 3. JSONB Column Performance Analysis

* **Structure**: Both `Snapshot.snapshot_json` and `Event.payload_json` utilize PostgreSQL-native `JSONB` format via SQLAlchemy's `JSON().with_variant(JSONB, "postgresql")` builder.
* **Why this is optimal**:
  1. **Binary Storage**: Unlike standard JSON columns that store strings, `JSONB` parses and stores the payload in a decomposed binary format, allowing fast structural updates and index creation.
  2. **SQLAlchemy Variant Mapping**: Fallback to standard standard-compliant JSON types on other dialect engines (e.g. SQLite for testing frameworks), making the codebase easy to test offline while retaining production performance.

---

## 4. Query Analysis & N+1 Prevention

### A. Dispatcher N+1 Risk Mitigation
The event notification dispatcher fetches unsent events and immediately resolves their owners:
```python
stmt = (
    select(Event)
    .options(selectinload(Event.user))
    .where(Event.sent == False)
    .order_by(Event.id.asc())
    .limit(50)
)
```
* **Performance Impact**: Utilizing `selectinload(Event.user)` forces SQLAlchemy to load the matching `User` rows in a single batch query (`SELECT ... FROM users WHERE id IN (...)`) rather than executing an independent query for every event row in the loop.
* **Result**: Query count is reduced from $O(N)$ (where $N$ is the batch size) to exactly 2 queries, eliminating N+1 performance degradation.

---

## 5. Session Lifecycle & Transaction Boundaries

1. **Isolation in `telegram.py`**:
   Database sessions are closed immediately when transaction blocks end. This keeps connection hold times extremely short:
   * During FSM registration, the session is held open for less than 10 milliseconds.
   * During `/attendance` fetches, the DB session is closed while the bot performs the slow network request to the NITRIS portal, preventing connection pool starvation.
2. **Worker Isolation**:
   Background sync cycles concurrency-throttle calls and use isolated `async with get_db_session()` context scopes for every individual user sync, ensuring failures in one sync do not leak resources or block other loops.
