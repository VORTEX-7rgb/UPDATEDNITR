# End-to-End Test Plan: CollegeClaw / NITRISClaw

This document outlines the testing strategy and scenarios for verifying the end-to-end correctness, reliability, concurrency, and security of the CollegeClaw system. These scenarios are fully automated in the verification suite located in `scratch/verify_system.py`.

---

## 1. Core Verification Scenarios

### 🧪 TEST 1: Login, Fetch, & Parse Flow
* **Objective**: Verify that the scraping pipeline successfully establishes a session, performs ASP.NET postbacks, retrieves the raw attendance page, and parses the fields into domain records.
* **Verification**: Mock successful portal responses containing a valid attendance HTML table. Verify that the parser extracts the student's name, course code, subject name, faculty, TC, UA, LE, and OA correctly.

### 🧪 TEST 2: Snapshot Creation
* **Objective**: Verify that when new attendance data is fetched, the Snapshot system persists it as a new record in the database.
* **Verification**: Insert a mock user, trigger `SnapshotService.create_snapshot_if_changed` on the parsed data. Verify that a new database row is added to the `snapshots` table with the correct SHA-256 hash.

### 🧪 TEST 3: Duplicate Snapshot Prevention
* **Objective**: Verify that fetching identical attendance data twice in a row does not create a redundant snapshot row.
* **Verification**: Run `SnapshotService.create_snapshot_if_changed` with the exact same data as in TEST 2. Verify that `changed` returns `False`, and no new row is created in the `snapshots` table.

### 🧪 TEST 4: Attendance Change Event Generation
* **Objective**: Verify that a change in class stats (e.g. TC or UA changes) generates semantic events.
* **Verification**: Inject a synthetic change in the mock attendance (e.g. increment unauthorized absences UA by 1 for a subject). Trigger snapshot update. Verify that two events are generated: `attendance_updated` (general changes) and `new_absence_detected` (absence warning).

### 🧪 TEST 5: Event Dispatcher Loop
* **Objective**: Verify that unsent events are queued, successfully formatted, sent via Telegram, and marked as sent in the database.
* **Verification**: Inject an unsent event, trigger `run_dispatch_worker` with a mock bot client. Verify that the mock bot's `send_message` was called with a beautifully formatted HTML message, and the event's `sent` flag in the database is updated to `True`.

---

## 2. Fault Tolerance & Edge-Case Scenarios

### 🧪 TEST 6: Invalid Credentials Isolation
* **Objective**: Verify that if a user has invalid credentials (causing login failure), their sync failure is isolated, and other users continue to sync successfully.
* **Verification**: Seed two users: one with valid credentials and one with invalid credentials. Run `run_sync_worker`'s gather execution. Verify that the invalid user's `sync_states.failure_count` increments and `last_error` is logged, while the valid user's sync succeeds without issue.

### 🧪 TEST 7: Telegram Blocked User Recovery
* **Objective**: Verify that if a user blocks the Telegram bot (raising a `TelegramForbiddenError`), the dispatcher marks their event as sent to clear the queue and continues without crashing.
* **Verification**: Simulate `TelegramForbiddenError` on a `send_message` call. Run the dispatcher. Verify that the event is still marked `sent` in the database to prevent blocking the dispatch queue, and the worker loop continues running.

### 🧪 TEST 8: Worker Cancellation & Resumption
* **Objective**: Verify that when workers are cancelled (during shutdown) and restarted, the system shuts down cleanly and resumes without database session leaks or lockups.
* **Verification**: Start worker tasks, cancel them mid-sleep or mid-execution, restart them, and verify that they resume normal loop scheduling cleanly.

### 🧪 TEST 9: Concurrent User Scale Test
* **Objective**: Verify that the worker successfully limits active sync concurrency using the Semaphore constraint and handles 100 concurrent users without session leaks, connection starvation, or crashes.
* **Verification**: Mock 100 users in the test database. Run parallel synchronization. Verify that at most 10 sync tasks run in parallel, database connection pools are not exhausted, and all tasks complete cleanly.

### 🧪 TEST 10: Database Outage Recovery
* **Objective**: Verify database session rollback safety during database communication errors, and self-recovery when connection is restored.
* **Verification**: Intercept SQL execution with a simulated database connection drop. Verify that the context manager rolls back the transaction successfully. Restore the database connection and verify that subsequent transactions execute and commit successfully.
