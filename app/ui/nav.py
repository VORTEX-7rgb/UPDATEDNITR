"""Callback-data vocabulary.

Existing legacy formats stay untouched in Phase A (db_attendance, msg_<id>,
inbox_page_<n>, qp_dl_<id>, tt_day_<n>, …) so no routing breaks. New buttons
introduced by the UI layer use the compact pipe codec below.
"""
from __future__ import annotations

# Reused existing targets (already routed by handlers):
HOME_CB = "inbox_back_dashboard"     # renders the dashboard into the tapped bubble
ATT_REFRESH_CB = "db_attendance"     # cache-first attendance refresh

# New UI-layer callbacks (pipe codec — max ~20 bytes, far under Telegram's 64):
GLOSSARY_CB = "ui|gloss"


def cb(*parts) -> str:
    """Build a callback string: cb('ui', 'gloss') -> 'ui|gloss'."""
    return "|".join(str(p) for p in parts)


def split(data: str) -> list[str]:
    return data.split("|")
