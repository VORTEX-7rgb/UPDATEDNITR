"""NITRClaw presentation layer.

Iron rules of this package:
  * Pure presentation. No business logic, no DB sessions, no job enqueues.
  * Every user-visible string lives in copy.py (the "voice" is reviewable in ONE file).
  * All Telegram-message lifecycle goes through surface.py (edit-what-you-tapped).
"""
