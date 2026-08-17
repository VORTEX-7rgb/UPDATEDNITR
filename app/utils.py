"""Shared formatting utilities for HTML-safe Telegram message rendering."""

import html
from typing import Any


def esc(val: Any) -> str:
    """Safely cast value to string, handle None, and HTML-escape for Telegram."""
    if val is None:
        return ""
    return html.escape(str(val))


def safe_truncate(escaped_str: str, limit: int) -> str:
    """Truncate an already-escaped HTML string, ensuring no HTML entities are cut in half."""
    if len(escaped_str) <= limit:
        return escaped_str
    
    truncated = escaped_str[:limit]
    # Find the last occurrence of '&' in the truncated portion
    last_amp = truncated.rfind('&')
    if last_amp != -1 and ';' not in truncated[last_amp:]:
        # A '&' exists without a subsequent ';' in the slice, meaning an entity was split
        truncated = truncated[:last_amp]
        
    return truncated.rstrip() + "..."
