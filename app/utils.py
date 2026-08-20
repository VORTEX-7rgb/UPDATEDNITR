"""Shared formatting utilities for HTML-safe Telegram message rendering and attachment handling."""

import html
import posixpath
import urllib.parse
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


def normalize_attachment_path(attachment_url: str) -> str:
    """Extract and normalize the URL path component of an attachment link.

    Strips query string parameters (which often contain per-user tokens) and
    scheme/hostname so that identical attachments referenced across multiple
    students or notices resolve to the exact same canonical path.

    Example:
      "../../docs/ReachYourStudent/notice1.pdf?token=abc" -> "/docs/ReachYourStudent/notice1.pdf"
      "/nitris/docs/ReachYourStudent/notice1.pdf" -> "/nitris/docs/ReachYourStudent/notice1.pdf"
    """
    if not attachment_url:
        return ""
    parsed = urllib.parse.urlsplit(attachment_url)
    path = parsed.path.strip()
    # Normalize relative '../' components
    path = posixpath.normpath(path)
    # Strip any leading dots, relative traversal tokens, and slashes
    clean = path.lstrip("./\\")
    return "/" + clean


def attachment_basename(attachment_path: str, fallback: str = "attachment.pdf") -> str:
    """Derive a clean filename for Telegram upload from a normalized path."""
    if not attachment_path:
        return fallback
    base = posixpath.basename(attachment_path)
    return base if base else fallback
