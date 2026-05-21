"""ASP.NET WebForms postback engine — full form extraction and submission."""

import logging
import os
import json
from typing import Optional

import httpx
from bs4 import BeautifulSoup

from app.nitris.exceptions import HiddenFieldExtractionError, AttendanceWorkflowError, SessionExpiredError, InvalidContextError
from app.nitris.constants import ATTENDANCE_TABLE_ID, CTL_SESSION

logger = logging.getLogger(__name__)

DEBUG_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "debug_html")


def is_error_page(html: str, response: httpx.Response = None) -> bool:
    """Check if the response is an IIS/ASP.NET error page (e.g. 503.aspx)."""
    if response and response.status_code in (301, 302, 303, 307, 308):
        return "503.aspx" in response.headers.get("Location", "")
    return "Error Pages/503.aspx" in html

def is_login_page(html: str) -> bool:
    """Check if the response is the login page."""
    return "Login.aspx" in html or "txtusername" in html.lower()

def has_session_dropdown(html: str) -> bool:
    """Check if the session dropdown is present in the HTML."""
    return f'name="{CTL_SESSION}"' in html

def has_attendance_table(html: str) -> bool:
    """Check if the attendance table is present in the HTML."""
    return f'id="{ATTENDANCE_TABLE_ID}"' in html


def extract_form_fields(html: str) -> dict[str, str]:
    """Extract ALL form state (inputs, selects, textareas). Emulates browser behavior."""
    soup = BeautifulSoup(html, "html.parser")
    form = soup.find("form")
    if not form:
        raise HiddenFieldExtractionError("No <form> found in HTML.")

    fields: dict[str, str] = {}

    # Extract all <input> elements (hidden, text, checkbox, radio)
    for tag in form.find_all("input"):
        name = tag.get("name")
        if not name:
            continue
        
        type_ = tag.get("type", "").lower()
        if type_ in ("checkbox", "radio") and not tag.has_attr("checked"):
            continue
            
        fields[name] = tag.get("value", "")

    # Extract all <select> elements (get currently selected option)
    for tag in form.find_all("select"):
        name = tag.get("name")
        if not name:
            continue
            
        selected_option = tag.find("option", selected=True)
        if selected_option:
            fields[name] = selected_option.get("value", "")
        else:
            # If nothing selected, browser sends first option
            first_option = tag.find("option")
            fields[name] = first_option.get("value", "") if first_option else ""

    # Extract all <textarea> elements
    for tag in form.find_all("textarea"):
        name = tag.get("name")
        if name:
            fields[name] = tag.get_text()

    if "__VIEWSTATE" not in fields:
        raise HiddenFieldExtractionError("Missing required field: __VIEWSTATE")

    logger.debug("Extracted %d form fields (VIEWSTATE: %d bytes)", len(fields), len(fields.get("__VIEWSTATE", "")))
    return fields


def extract_dropdown_options(html: str, select_name: str) -> list[tuple[str, str]]:
    """Extract (value, text) pairs from a <select> dropdown by name attribute."""
    soup = BeautifulSoup(html, "html.parser")
    select = soup.find("select", {"name": select_name})
    if not select:
        return []
    return [(opt.get("value", ""), opt.get_text(strip=True)) for opt in select.find_all("option") if opt.get("value")]


async def submit_postback(
    client: httpx.AsyncClient,
    url: str | httpx.URL,
    form_state: dict[str, str],
    event_target: str,
    form_updates: dict[str, str],
    step_name: str = "",
    debug: bool = False,
) -> str:
    """Submit an ASP.NET postback with full form state and return response HTML."""
    # Build full payload: original state + target/args + specific field updates
    payload = {
        **form_state,
        "__EVENTTARGET": event_target,
        "__EVENTARGUMENT": "",
        **form_updates
    }

    url_str = str(url)
    logger.info("[%s] POST %s (target: %s, payload keys: %d)", step_name, url_str, event_target.split("$")[-1], len(payload))
    
    headers = {"Referer": url_str}
    
    # follow_redirects=False to catch 302s to Error or Login pages
    response = await client.post(url, data=payload, headers=headers, follow_redirects=False)

    logger.info("[%s] Response: Status %d, %d bytes", step_name, response.status_code, len(response.content))

    if response.status_code in (301, 302, 303, 307, 308):
        location = response.headers.get("Location", "")
        logger.error("[%s] Unexpected redirect to: %s", step_name, location)
        if "Login.aspx" in location:
            raise SessionExpiredError("Session expired — redirected to login.")
        if "503.aspx" in location:
            raise InvalidContextError(f"[{step_name}] Invalid context: Redirected to 503.aspx")
        raise AttendanceWorkflowError(f"[{step_name}] Unexpected redirect to {location}")

    if response.status_code != 200:
        raise AttendanceWorkflowError(f"[{step_name}] POST returned {response.status_code}")

    html = response.text

    if is_login_page(html):
        raise SessionExpiredError("Session expired — login form detected in response.")

    if debug and step_name:
        metadata = {
            "step": step_name,
            "status": response.status_code,
            "url": url_str,
            "response_size": len(html),
            "viewstate_present": "__VIEWSTATE" in html,
            "table_found": ATTENDANCE_TABLE_ID in html
        }
        _save_debug_snapshot(html, metadata, step_name)

    return html


def _save_debug_snapshot(html: str, metadata: dict, step_name: str) -> None:
    """Save intermediate HTML and JSON metadata for debugging."""
    try:
        os.makedirs(DEBUG_DIR, exist_ok=True)
        
        # Save HTML
        html_path = os.path.join(DEBUG_DIR, f"{step_name}.html")
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(html)
            
        # Save Metadata
        json_path = os.path.join(DEBUG_DIR, f"{step_name}.json")
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(metadata, f, indent=2)
            
        logger.debug("Saved debug snapshot: %s.*", step_name)
    except OSError as e:
        logger.warning("Could not save debug snapshots: %s", e)
