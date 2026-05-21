# SOUL.md

## Project Identity

CollegeClaw / NitrClaw is a backend automation platform for NITRIS (NIT Rourkela ERP).

The system automates:

* attendance fetching
* notices/messages
* PDF retrieval
* attendance/debar warnings
* Telegram interaction

The project is NOT AI-first.

AI/Hermes layers are secondary UX enhancements.

Core priority:
reliable deterministic backend automation.

---

## Engineering Philosophy

Prefer:

* simple systems
* boring systems
* deterministic systems
* maintainable systems
* debuggable systems

Avoid:

* overengineering
* unnecessary abstractions
* architecture complexity
* premature optimization
* speculative future systems

Reliability is more important than feature count.

---

## Architecture Principles

* Keep architecture monolithic
* Keep implementation modular
* Build features incrementally in phases
* Keep responsibilities separated
* Avoid mixing concerns

Separation rules:

* networking logic
* parsing logic
* business logic
* database logic
* Telegram/UI logic

must remain isolated.

All portal communication must go through `NitrisClient`.

---

## AI Agent Rules

Do NOT:

* redesign architecture unless explicitly requested
* generate unnecessary abstractions
* introduce microservices
* introduce orchestration frameworks
* introduce browser automation unless necessary
* add speculative future systems
* add unnecessary dependencies
* generate giant files/functions

Implement ONLY the requested scope.

Prefer:

* small focused functions
* readable code
* explicit flow
* predictable behavior
* production-oriented implementation

---

## Reliability Rules

* Network requests must use timeouts/retries
* Fail loudly instead of silently
* Add parser validation checks
* Keep systems easy to debug
* Add structured logging around important operations

---

## Development Rules

* Build one verified layer at a time
* Verify components independently before integration
* Keep commits small and focused
* Avoid rewriting working systems impulsively

The system should remain understandable months later without AI assistance.
