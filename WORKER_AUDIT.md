# Worker Audit: CollegeClaw / NITRISClaw

This report presents a safety, correctness, and architecture audit of the background worker loops located in `app/workers/sync_worker.py`.

---

## 1. Concurrency Controls & Semaphore Analysis

* **Setup**:
  ```python
  SYNC_SEMAPHORE_LIMIT = 10
  semaphore = asyncio.Semaphore(SYNC_SEMAPHORE_LIMIT)
  ```
* **Gather Execution**:
  The background sync worker fetches all users, builds a list of coroutines, and gathers them concurrently:
  ```python
  tasks = [
      sync_user_data(..., semaphore=semaphore)
      for u in users
  ]
  await asyncio.gather(*tasks)
  ```
* **Evaluation**:
  1. **Semaphore Throttle**: When gathered, all `sync_user_data` tasks start execution simultaneously. However, they must acquire the shared `asyncio.Semaphore(10)` before proceeding. This guarantees that at most 10 parallel HTTP login/fetch sequences and database sessions are active.
  2. **Preventing Target Exhaustion**: Throttling concurrency prevents the target NITRIS server from being overwhelmed by synchronous login requests from a single IP, reducing the risk of rate-limiting or firewall blockings.
  3. **Database Connection Pool safety**: Since our connection pool size is configured as `pool_size=10, max_overflow=20`, keeping concurrent worker operations capped at 10 guarantees that worker tasks alone will never exhaust the database connection pool, leaving headroom for live user `/attendance` queries.

---

## 2. Exception Isolation & Fault Tolerance

The worker implements a highly modular try-except architecture to isolate faults:
* **User-level Isolation**:
  The `sync_user_data` function is split into isolated logical blocks:
  1. Encryption failures (key mismatch) -> logged, failure updated, task exits.
  2. Scraper network failures -> logged, failure updated, task exits.
  3. Database persistence errors -> logged, failure updated, task exits.
  * **Result**: An exception in syncing a single user's credentials or connection will never crash the global sync cycle. Other users in the gather group continue processing normally.
* **Global Loop Fault Tolerance**:
  The worker's loop is wrapped in a high-level try-except block:
  ```python
  except Exception as e:
      logger.error("Unexpected error in background sync loop: %r", e)
  ```
  * **Result**: If an unexpected exception is raised (e.g. database connection drops during user retrieval), the worker logs the error, waits for the next cycle, and resumes. The background daemon survives.

---

## 3. Worker Shutdown & Cancellation Safety

### A. Core Cancellation Propagation
When the application shuts down, the tasks are cancelled explicitly:
```python
sync_worker_task.cancel()
dispatch_worker_task.cancel()
```
* Under asyncio rules, calling `.cancel()` raises an `asyncio.CancelledError` inside the active coroutine.
* If the worker is sleeping at `await asyncio.sleep(SYNC_INTERVAL_SECONDS)`, the sleep is terminated and the loop is cleanly broken.
* If the worker is running `await asyncio.gather(*tasks)`, the `CancelledError` is propagated downward, cancelling all currently running `sync_user_data` subtasks.

### B. Clean Database Resource Reclamation
* During a cancellation, is there a risk of unclosed database connections?
  **No**. All database interactions in `sync_user_data` utilize the transactional context manager `get_db_session()`:
  ```python
  async with get_db_session() as session:
      ...
  ```
  Even when `CancelledError` is raised inside the transaction, Python's context manager rules guarantee that the `finally` block of `get_db_session` runs:
  ```python
  finally:
      await session.close()
  ```
  This guarantees that all active database connections are returned to the SQLAlchemy pool during a cancellation, avoiding pool leaks.
