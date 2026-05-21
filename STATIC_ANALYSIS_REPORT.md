# Static Analysis Report: CollegeClaw / NITRISClaw

This report inspects the codebase for static correctness, asynchronous loop patterns, session or connection leakage, exception handling patterns, and potential transaction issues.

---

## 1. Async & Event Loop Inspection

### A. Windows Event Loop Workaround
The entrypoint `app/main.py` and `stress_test.py` correctly implement the selector event loop policy on Windows platform:
```python
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
```
* **Why this is critical**: This prevents transient WinError socket connection terminations (e.g., 10054, 121, 64) that commonly crash asyncio loops on Windows when running long-running operations.

### B. Missing Awaits & Blocking Operations
* **Async Calls**: All asynchronous methods (`client.login`, `client.fetch_attendance`, `client.close`, `get_attendance_data`, `snapshot_service.create_snapshot_if_changed`) are invoked with explicit `await` keywords.
* **Non-blocking sleep**: Background loops correctly utilize `await asyncio.sleep(...)` instead of synchronous `time.sleep()`, preventing thread-blocking scenarios.
* **HTTP Client**: `httpx.AsyncClient` is utilized for all network calls rather than synchronous libraries.

---

## 2. Database Session & Connection Lifecycle Audit

### A. Context Manager Validation (`database.py`)
```python
@asynccontextmanager
async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    session: AsyncSession = async_session_factory()
    try:
        yield session
    except Exception as e:
        logger.error("Database session error encountered, rolling back: %s", e)
        await session.rollback()
        raise
    finally:
        await session.close()
```
* **Strengths**: This guarantees that every session acquired is cleanly closed in the `finally` block, eliminating connection leaks. Rollback is correctly called if an exception occurs inside the context.
* **Weaknesses**: The `except` block catches `Exception`, performs `await session.rollback()`, and then `raise`s it. While functional, it is redundant when nested under `async with session.begin():` blocks (since `session.begin` has its own exception rollback handler), but it acts as a safe double-net.

### B. Detached Instance Access Pattern
In `telegram.py`:
```python
async with get_db_session() as session:
    user_repo = UserRepository(session)
    user = await user_repo.get_by_telegram_id(telegram_id)
# Session closed here!
...
plaintext_password = decrypt_password(user.encrypted_password)
```
* **Risk**: Once the session exits, the `user` object is detached. Accessing mapped scalar attributes (`user.id`, `user.roll_number`, `user.encrypted_password`) is safe here because `expire_on_commit=False` is set on the session factory and these attributes are loaded. However, accessing relationships or lazy-loaded properties on a detached object will raise `sqlalchemy.orm.exc.DetachedInstanceError`.
* **Fix/Mitigation**: Currently, the application only accesses primary keys and scalars, which avoids crashes. Developers must load all required relations eagerly (e.g., via `selectinload`) if they need to access them outside transaction blocks.

---

## 3. Transaction Integrity & Concurrent Safety

### A. Transaction Boundaries
Transactions are bounded correctly via `async with session.begin():` blocks:
* Registration: Encapsulates user verification and creation/credential updates in one atomic write transaction.
* Snapshotting: Envelops snapshot creation and event logging inside one atomic transaction block in `SnapshotService.create_snapshot_if_changed`.
* Event Updates: The dispatcher updates sent events individually inside `async with session.begin():` blocks.

### B. Duplicate Snapshot Hashing
* Hashing is performed deterministically on key-sorted JSON fields:
  ```python
  deterministic_json = json.dumps(data_dict, sort_keys=True)
  snapshot_hash = hashlib.sha256(deterministic_json.encode("utf-8")).hexdigest()
  ```
* **Result**: Highly stable signatures that guarantee duplicate runs on unchanged data will not persist redundant snapshot rows.

---

## 4. Resource Allocation & Cleanup Audit

### A. HTTP Client Safety
`NitrisClient` creates `httpx.AsyncClient` inside its constructor. It provides an async `close()` method:
```python
async def close(self) -> None:
    await self.client.aclose()
```
* `attendance_service.py` correctly wraps the client usage inside a `finally` block:
  ```python
  client = NitrisClient()
  try:
      ...
  finally:
      await client.close()
  ```
* **Result**: Zero leaked TCP connections or HTTP clients.

---

## 5. Exceptions & Logging

### A. Password Censorship
* Password fields are omitted from representation functions in the declarative mapping:
  ```python
  def __repr__(self) -> str:
      return f"<User id={self.id} telegram_id={self.telegram_id} roll_number='{self.roll_number}'>"
  ```
* **Result**: Plaintext passwords or encrypted password hashes are never written to server logs during model printing.

### B. Uncaught Exception Silencing
* In `telegram.py` line 70, message deletion is wrapped inside a generic try-except:
  ```python
  try:
      await message.delete()
  except Exception:
      pass
  ```
  * This is correct, as a failure to delete the user's password message (e.g. lack of permissions in private chat) should not crash the transaction workflow.
* In `sync_worker.py` line 65, database updates are wrapped cleanly to isolate failures per user, preventing sync cycles from crashing when a single record fails:
  ```python
  except Exception as e:
      logger.error("Failed to persist snapshot/events in database for User ID %d: %r", user_id, e)
  ```

---

## 6. Dead Code & Code Quality Findings

1. **`EventRepository.mark_sent`**:
   * Located in `app/db/repositories/event_repository.py`.
   * **State**: Never called within the application. The background event dispatcher updates sent states via direct ORM property manipulation inside transaction blocks instead of repository calls.
2. **Missing Type Annotations**:
   * Several internal helper methods (`_update_sync_state`, `_get_sorted_academic_years`) have incomplete type hints.
3. **No Retries on Session Expirations**:
   * If a transient issue causes `SessionExpiredError` in the middle of scraping, it is bypassed by retries and raised immediately. Retries are restricted only to `AttendanceWorkflowError` and `httpx.TransportError`.
