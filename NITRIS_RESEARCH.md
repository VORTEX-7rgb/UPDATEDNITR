# NITRIS_RESEARCH.md

# NITRIS Reverse Engineering Research

This document contains confirmed technical discoveries about the NITRIS (NIT Rourkela ERP) system.

Only store verified findings here.
Avoid speculation unless explicitly marked.

---

# Core Architecture

NITRIS is a classic ASP.NET web application.

Observed stack:

* ASP.NET WebForms
* Microsoft IIS
* Session-cookie authentication
* Server-rendered HTML pages
* ASP.NET postbacks
* jQuery/AJAX requests

Important characteristics:

* deterministic backend behavior
* stateful sessions
* heavily HTML-driven
* uses ASP.NET hidden fields (__VIEWSTATE, __EVENTVALIDATION)
* many pages are rendered server-side instead of JSON APIs

---

# Authentication Flow

Authentication uses ASP.NET session-cookie authentication.

Authentication is NOT:

* JWT
* OAuth
* token-based auth

Authentication IS:

* server-side session auth using ASP.NET_SessionId

---

# Login Flow

## Step 1 — Password Transformation

Endpoint:

POST:
`/nitris/Login.aspx/GetPassword`

Headers:

* Content-Type: application/json; charset=UTF-8
* X-Requested-With: XMLHttpRequest

Payload:

```json
{
  "password": "raw_password"
}
```

Response:

```json
{
  "d": "transformed_password"
}
```

Important findings:

* raw password is sent to server
* server transforms password
* transformation is NOT frontend encryption
* transformed value replaces textbox value in browser
* no need to reverse engineer encryption algorithm

The transformed password is later used for login.

---

## Step 2 — Login Request

Endpoint:

POST:
`/nitris/Login.aspx/LoginUser`

Payload:

```json
{
  "username": "...",
  "logpassword": "transformed_password"
}
```

Successful login:

* creates ASP.NET session
* returns ASP.NET_SessionId cookie
* returns redirect string

Example response:

```text
SUCCESS:/nitris/Student/Home/Home.aspx
```

Important:
The ASP.NET_SessionId cookie is the authenticated identity.

---

# Session Behavior

NITRIS uses classic ASP.NET session state.

Authenticated requests require:

```text
ASP.NET_SessionId
```

Important findings:

* sessions eventually expire
* expired sessions require relogin
* session cookie reuse is important
* avoid unnecessary repeated logins
* large-scale systems should reuse sessions efficiently

Likely session expiry:

* inactivity timeout
* server-configured ASP.NET timeout

Estimated:
~10–20 minutes inactivity

Needs future confirmation.

---

# Attendance System

Attendance system uses server-rendered HTML pages.

Attendance is NOT primarily fetched through XHR APIs.

Main attendance page:

```text
GET /nitris/Student/Attendance/ClassAttendance.aspx
```

Query params currently observed:

```text
AppId
AppName
SubModId
ModId
```

Observed decoded values:

* AppId = 3
* AppName = Attendance and Leave
* SubModId = 12
* ModId = 10

Observed behavior:

* params currently appear static
* params are embedded in navigation links
* params may contain checksum/hash suffixes
* current implementation assumption:
  params can be hardcoded unless future evidence disproves it

Attendance page requires:

* authenticated ASP.NET session cookie

Attendance page returns:

* full HTML document

---

# Attendance Page Structure

Important parser targets:

## Student Info

Element ID:

```text
ContentPlaceHolder2_ContentPlaceHolder1_mainContent_lblSnameroll
```

Contains:

* student name
* roll number

---

## Main Attendance Table

Element ID:

```text
ContentPlaceHolder2_ContentPlaceHolder1_mainContent_gvSubjects
```

Attendance rows contain:

* subject code
* subject name
* LTP
* credits
* section
* faculty
* TC
* UA
* LE
* OA

---

# Attendance Field Meanings

Observed meanings:

## TC

Total Classes

## UA

Unauthorised Absence

## LE

Leave

## OA

Overall Absence

Observed rule:

```text
OA = UA + LE
```

Needs future verification across edge cases.

---

# Debar / Attendance Rules

Attendance system contains debar rules based on:

* LTP structure
* credits
* course type
* attendance category

Observed rules table exists in HTML.

Parser target:

```text
ContentPlaceHolder2_ContentPlaceHolder1_mainContent_gvAttendanceRule
```

Important:
Current Phase 1A does NOT implement debar intelligence.

Current phase ONLY focuses on:
accurate attendance extraction.

Future rule engine will later:

* evaluate thresholds
* predict safe absences
* generate warnings
* support varying branch/year logic

Do NOT hardcode simplistic "75%" rules.

---

# ASP.NET Postback Behavior

Some actions use ASP.NET postbacks instead of normal requests.

Observed example:
"Details" button in attendance table.

Example:

```text
javascript:__doPostBack(...)
```

These require:

* __VIEWSTATE
* __EVENTVALIDATION
* ASP.NET form submission handling

Current Phase 1A ignores these postback-driven details pages.

Current MVP only uses:
summary attendance table.

---

# Notices / Messages System

Observed endpoints:

## All Messages

```text
GET /nitris/Student/Home/AllMessages.aspx
```

## Individual Message

```text
GET /nitris/Student/Home/Message.aspx
```

Observed behavior:

* messages likely contain notices
* may contain PDFs
* may contain announcements/academic updates

Future phases will reverse engineer:

* notice parsing
* PDF retrieval
* notification syncing

---

# Other Observed Modules

Observed navigation modules:

* Student Info
* Registration
* Examination
* Fee Payment
* Hostel Management
* Assignments
* Attendance and Leave

Observed pattern:
modules use:

```text
AppId
AppName
SubModId
ModId
```

Current assumption:
module navigation is deterministic and reusable.

---

# System Characteristics

Important engineering conclusions:

## Deterministic System

NITRIS behavior appears deterministic and backend-driven.

Good for automation.

---

## HTML-Centric

Many important pages:

* return server-rendered HTML
* not JSON APIs

Parsing reliability is important.

---

## Session-Centric

Authentication depends heavily on:

```text
ASP.NET_SessionId
```

Session handling is core infrastructure.

---

## Browser Automation Not Required Initially

Current discoveries suggest:

* browserless automation is possible
* heavy Selenium/Playwright usage likely unnecessary for MVP

Prefer:

* httpx
* requests
* BeautifulSoup
* deterministic HTTP automation

---

# Known Risks

## HTML Changes

Parser breakage risk if:

* IDs/classes/layout changes

Need parser validation + monitoring.

---

## Session Expiry

Need:

* relogin handling
* session reuse
* retry logic

---

## Rate Limiting / Detection

Potential future risks:

* excessive logins
* excessive scraping
* IP-based restrictions

Need efficient sync behavior.

---

# Current MVP Scope

Phase 1A only targets:

```text
Telegram command
→ login
→ fetch attendance page
→ parse attendance
→ return attendance response
```

Do NOT prematurely implement:

* advanced debar systems
* AI orchestration
* large-scale infra
* speculative future systems
* browser automation

Focus:

* reliability
* clean architecture
* deterministic flow
* maintainable implementation
