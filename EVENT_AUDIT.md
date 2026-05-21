# Event Audit: CollegeClaw / NITRISClaw

This document presents a correctness and taxonomy audit of the Event tracking system in the application, primarily checking `EventService` and its semantic diff rules.

---

## 1. Semantic Diff Integrity & Change Analysis

The diff logic in `EventService.detect_and_store_changes` evaluates two distinct states:

### A. Case 1: Bootstrap Sync (First-time User Run)
* **Rule**: When `previous_snapshot` is `None`, every course returned is treated as newly discovered.
* **Events Created**: Emits `new_subject_added` events for every record found.
* **Payload Structure**:
  ```json
  {
    "subject_code": "CS-301",
    "subject_name": "Database Management Systems",
    "faculty": "Dr. A. K. Smith",
    "tc": "12",
    "ua": "2"
  }
  ```

### B. Case 2: Incremental Sync (Comparative Check)
When a previous snapshot exists, the engine maps records by unique `subject_code` and executes two distinct diff processes:
1. **Discovered Courses**: Any `subject_code` present in the new snapshot but absent in the old one yields a `new_subject_added` event.
2. **Attendance Drift**: For courses matching across both snapshots, the engine compares individual stats (`tc`, `ua`, `le`, `oa`). If differences occur, it generates:
   * An `attendance_updated` event outlining the field-level changes.
   * **Absence Detection Warning**: If the unauthorized absence count (`ua`) increases (`new_ua > old_ua`), the service generates a secondary `new_absence_detected` event.

---

## 2. Taxonomy & Type Hints Safety

The current event catalog has three main event types:
1. `new_subject_added`: Signals course registrations.
2. `attendance_updated`: General change event detailing updated metrics.
3. `new_absence_detected`: Alerts on unauthorized absences.

* **Audit Finding — Raw String Taxonomy**:
  The event types are represented as raw strings (`"attendance_updated"`, `"new_absence_detected"`, `"new_subject_added"`).
  * **Risk**: Typographical errors during development can lead to silent operation failures or dispatcher formatting failures.
  * **Recommendation**: Consolidate these strings into an explicit Enumeration type class (e.g. `EventType(str, Enum)`) to achieve full IDE code completion and static type validation checks.

---

## 3. Robustness & Value Parse Audit

### A. Numeric Parsing Fragility
Inside the incremental sync loop:
```python
old_ua = int(prev_rec.get("ua", "0") or "0")
new_ua = int(new_rec.get("ua", "0") or "0")
```
* **Vulnerability**: While it correctly guards against missing or `None` attributes via `or "0"`, it assumes that the scraped values are standard numeric strings. If the NITRIS portal renders non-numeric characters (e.g. letters, placeholders like `-`, or whitespace), calling `int()` will raise a `ValueError`, crashing the sync process for that user.
* **Mitigation**: Implement a safe integer parse helper that strips non-digits and returns `0` upon conversion failures.

---

## 4. Notification Safety

* **Asynchronous Buffer Pattern**:
  Instead of sending Telegram notifications synchronously in the middle of scraping, the event system saves events in the database with `sent = False`.
  * **Benefit**: This acts as a reliable message buffer. If the Telegram Bot API suffers downtime, rate limits, or network timeouts, the dispatcher worker can resume cleanly later without losing any notification alerts.
* **Isolation of Telegram Errors**:
  The dispatcher evaluates each event inside a separate database transaction scope. If an individual dispatch fails (e.g., a blocked user raising `TelegramForbiddenError`), the event dispatcher logs it, marks the event as processed to avoid queue blocks, and cleanly processes the remaining items.
