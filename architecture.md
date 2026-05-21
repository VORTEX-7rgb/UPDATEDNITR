# ARCHITECTURE.md

## Core System Flow

Telegram Bot
↓
Service Layer
↓
NitrisClient
↓
NITRIS Portal
↓
HTML Response
↓
Parser Layer
↓
Structured Data
↓
Database
↓
Telegram Response

---

## Layer Responsibilities

### `nitris/`

Handles:

* authentication
* session management
* portal requests
* HTTP communication

Does NOT handle:

* parsing
* Telegram logic
* DB logic

All NITRIS communication must go through `NitrisClient`.

---

### `parsers/`

Handles:

* HTML parsing
* structured data extraction

Returns:

* clean Python objects/dicts

Does NOT:

* make network requests
* access database
* send Telegram responses

---

### `services/`

Handles:

* business workflows
* orchestration
* coordination between layers

Examples:

* attendance sync
* notice sync
* debar evaluation

---

### `workers/`

Handles:

* background jobs
* periodic syncing
* scheduled tasks

Workers should:

* isolate failures per user
* log errors cleanly
* never crash entire sync cycle

---

### `db/`

Handles:

* models
* persistence
* database access

Keep schemas simple and maintainable.

---

### `bot/`

Handles:

* Telegram commands
* Telegram responses
* user interaction

Bot layer should remain thin.

Eventually:
Telegram should mostly read from DB instead of scraping live.

---

## Architectural Constraints

Avoid:

* giant god classes
* giant utility files
* duplicated login logic
* mixed concerns
* hidden implicit flows

Prefer:

* explicit flow
* isolated layers
* predictable control flow
* simple debugging

---

## Scaling Philosophy

Optimize for:

1. clarity
2. reliability
3. maintainability

before:

* performance optimization
* scaling optimization
* advanced infrastructure

Do NOT prematurely optimize for large scale.

Scale incrementally after stable functionality exists.

---

## AI/Hermes Philosophy

AI is a UX enhancement layer.

AI should NOT control:

* authentication
* scraping
* parsing
* deterministic business logic

Core backend behavior must remain deterministic and testable.
