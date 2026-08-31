"""NITRClaw's voice — EVERY user-visible string introduced by the UI layer.

Rules of the voice:
  * 90% utility, 10% personality. Humor is decoration; facts are always plain.
  * Humor NEVER hides information: every persona includes status + next step.
  * One emoji per concept: 📊 attendance · 📅 timetable · 📬 inbox · 📝 papers.
"""

# ── System states ───────────────────────────────────────────────────────────
UPDATING = "↻ <i>Checking NITRIS…</i>"
SLOW_NITRIS = "🐌 <i>NITRIS is taking its sweet time — your request is queued safely.</i>"
STILL_RUNNING = "⏳ <i>Still updating in the background. Check again in a moment.</i>"
CACHED_NOTE = "⚡ Served from Claw cache — NITRIS wasn't needed."
UPDATED_JUST_NOW = "🟢 Updated just now."
SYNC_COMPLETE = "🟢 Sync complete."

CIRCUIT_DOWN = (
    "💀 <b>NITRIS is having a moment.</b>\n\n"
    "Requests are paused so we don't make it worse.\n"
    "<i>Try again in ~60 seconds.</i>"
)
GENERIC_ERROR = (
    "🦀 <b>Something broke on our side.</b>\n\n"
    "Your existing data is still safe. Try again in a moment."
)
QUEUE_BUSY = (
    "🚦 <b>Claw's queue is jammed right now.</b>\n\n"
    "Too many students are hitting the portal at once.\n"
    "<i>Give it a few seconds and tap again.</i>"
)

# ── Programme support gate ──────────────────────────────────────────────────
# Fires at registration time when the roll number's programme digit (roll[5])
# isn't in the supported BTech / undergraduate set. Friendly, honest, with a
# clear next step. Voice per file rules: utility first, one emoji for the
# concept, no false promises. {roll} is filled in by the caller (HTML-escaped).
POSTGRAD_UNSUPPORTED_NOTICE = (
    "🎓 <b>Hey! We're not ready for your programme just yet.</b>\n\n"
    "NitrClaw currently supports <b>BTech, Dual Degree, and Integrated MSc</b> "
    "(all undergraduate programmes) on the NITRIS portal.\n\n"
    "Your roll number (<code>{roll}</code>) belongs to a <b>Master's / postgraduate</b> "
    "programme (MTech / MSc 2-year / PhD), and NITRIS serves those with a slightly "
    "different page layout that we're still teaching the bot to handle.\n\n"
    "🛠️ <b>We're working day and night to bring you in.</b> Hang tight — "
    "Master's and PhD support is <b>coming soon</b>.\n\n"
    "<i>If you believe this is a mistake, double-check your roll number and try again, "
    "or send /cancel to abort.</i>"
)

# ── Attendance glossary (plain-English translations of NITRIS jargon) ──────
GLOSSARY_TITLE = "📖 What NITRIS actually means"
GLOSSARY_BODY = (
    "<blockquote>"
    "<b>Classes held</b> — how many classes happened so far.\n"
    "<b>Skip (UA)</b> — you weren't there and had NO approved leave.\n"
    "<b>Approved leave (LE)</b> — medical / SAC / T&amp;P etc. Doesn't count as a skip.\n"
    "<b>Missed total (OA)</b> — skips + approved leaves combined.\n\n"
    "💀 <b>Debar</b> — cross the skip limit for a subject's L-T-P pattern and "
    "you're barred from that subject's end-sem exam."
    "</blockquote>"
)
GLOSSARY_NOTE = "Each subject has its OWN limits based on its lecture-tutorial-practical pattern."

# ── Smart empties ───────────────────────────────────────────────────────────
INBOX_EMPTY = (
    f"📬 <b>INBOX</b>\n\n"
    "<blockquote>You're caught up 🎉\n\nNo notices yet.\n"
   "That's either ✅ good news or 💀 suspiciously peaceful.</blockquote>"
)
INBOX_EMPTY_STALE = (
    f"📬 <b>INBOX</b>\n\n"
    "<blockquote>Inbox is empty.\nRun a sync to pull your notices from NITRIS.</blockquote>"
)

# ── Delivery micro-surface ──────────────────────────────────────────────────
QP_DROPPED = "🚀 <b>Dropped.</b>\n<i>NITRIS wasn't touched.</i> 🦀"

# ── Latency personas ────────────────────────────────────────────────────────
def slow_note(seconds_hint: str) -> str:
    return f"🦀 <i>NITRIS is being slow ({seconds_hint}). You're seeing cached data while Claw updates it.</i>"
