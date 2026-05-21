# CURRENT_PHASE.md

## Current Phase

Phase 1A — Attendance MVP

---

## Current Goal

Build a working Telegram attendance flow:

Telegram Command
→ login to NITRIS
→ fetch attendance page
→ parse attendance data
→ return attendance response

---

## Current Scope

Implement ONLY:

* NitrisClient login/session flow
* attendance page fetch
* attendance parser
* minimal Telegram command
* minimal DB models if required
* basic logging/retries

---

## Current Priorities

Priority order:

1. login reliability
2. session handling
3. attendance fetch
4. parser correctness
5. Telegram response
6. clean architecture

---

## Current Constraints

Do NOT implement:

* AI assistant systems
* Hermes orchestration
* advanced notifications
* dashboards
* analytics
* Redis
* caching layers
* microservices
* browser automation
* plugin systems
* speculative scaling systems

---

## Attendance Notes

NITRIS attendance system contains:

* L-T-P structures
* TC / UA / LE / OA values
* debar rules
* varying attendance logic

Current phase ONLY focuses on:
accurate attendance extraction.

Debar intelligence/rule engine will be implemented later.

---

## Success Condition

Successful completion means:

A real Telegram bot can:

* authenticate with NITRIS
* fetch real attendance data
* parse attendance correctly
* return attendance to user reliably

without manual intervention.
