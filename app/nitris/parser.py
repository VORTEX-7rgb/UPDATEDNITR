"""Parse the final rendered attendance HTML into structured data."""

import logging
import re
from dataclasses import dataclass, asdict
from datetime import time, datetime
from typing import Optional
from bs4 import BeautifulSoup
from app.nitris.constants import (
    ATTENDANCE_TABLE_ID, STUDENT_INFO_LABEL_ID,
    TIMETABLE_HEADING_TEXT, TIMETABLE_TABLE_CSS_CLASS,
    TIMETABLE_TITLE_RE, TIMETABLE_ROOM_RE, TIMETABLE_TIME_RE,
    HTML_PARSER,
)
from app.nitris.exceptions import AttendanceParseError, HomeParseError, InboxParseError

logger = logging.getLogger(__name__)


# ── Timetable data classes ───────────────────────────────────────────────────

# Day name → weekday number (Python convention: Monday=0 ... Sunday=6).
# Used by the parser AND by the now/next algorithm — both sides MUST agree.
_DAY_TO_WEEKDAY = {
    "Monday": 0, "Tuesday": 1, "Wednesday": 2,
    "Thursday": 3, "Friday": 4, "Saturday": 5, "Sunday": 6,
}
WEEKDAY_NAMES = tuple(_DAY_TO_WEEKDAY.keys())  # ("Monday", "Tuesday", ...)


@dataclass(frozen=True)
class TimetableSlot:
    """One parsed timetable entry. Matches the recon JSON shape 1:1."""
    day: str                    # "Monday" ... "Sunday"
    weekday: int                # 0..6 (Mon=0, Sun=6)
    period_index: int           # 1..N
    start_time: str             # "08:00" (24-hour, HH:MM)
    end_time: str               # "08:55"
    subject: str                # course code, e.g. "MN2101"; "LUNCH" for break
    room: str                   # "205" / "LA 117" / "" if no room
    is_break: bool = False      # True for the LUNCH row
    # Bonus metadata scraped from the cell `title` attribute. Empty if NITRIS
    # doesn't supply them. Not part of the recon canonical shape, but useful
    # for future "show course name" UX without re-scraping.
    subject_name: str = ""
    course_type: str = ""


@dataclass
class HomeParseResult:
    """Result of parse_home_page(). Today only the timetable is extracted;
    webmail creds, recent messages, anti-ragging etc. are reserved for
    future phases (see NITRIS_PORTAL_RECON.json `files_to_add_or_modify_in_repo`)."""
    timetable: list[TimetableSlot]
    raw_html_bytes: int = 0

    def to_timetable_dicts(self) -> list[dict]:
        return [asdict(s) for s in self.timetable]


# ── Timetable parser ─────────────────────────────────────────────────────────

def _find_timetable_table(soup: BeautifulSoup):
    """Locate the timetable <table> element in the Home.aspx HTML.

    The timetable table is directly preceded by the heading 'Course Class Time Table'
    and contains period times ('08:00', '09:00') along with day names ('Monday', 'Tuesday').
    """
    # 1. Heading anchor (h1..h6 containing Course Class Time Table)
    for h in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6"]):
        htext = h.get_text(" ", strip=True).lower()
        if "course class time table" in htext or "class time table" in htext or "timetable" in htext:
            tbl = h.find_next("table")
            if tbl is not None:
                return tbl

    # 2. Table containing both day names AND period time strings
    for tbl in soup.find_all("table"):
        txt = tbl.get_text(" ", strip=True)
        if "Monday" in txt and ("08:00" in txt or "8:00" in txt or "09:00" in txt or "12:00" in txt):
            return tbl

    # 3. Fallback: table with PERIOD and Monday
    for tbl in soup.find_all("table"):
        txt = tbl.get_text(" ", strip=True)
        if "PERIOD" in txt and "Monday" in txt:
            return tbl

    return None


def _parse_header_times(header_container) -> list[tuple[str, str]]:
    """Parse the header element (thead or first tr) into a list of (start_time, end_time) tuples.

    The first cell is the corner cell ("PERIOD DAY") and is skipped. Each
    subsequent cell renders as "08:00 hr 08:55 hr" — the regex extracts the
    two HH:MM tokens; the " hr" suffixes are discarded.
    """
    time_slots: list[tuple[str, str]] = []
    # th/td cells inside the header container
    header_cells = header_container.find_all(["th", "td"])
    if not header_cells or len(header_cells) < 2:
        raise HomeParseError(
            f"Timetable header has too few cells (got {len(header_cells)})."
        )

    for cell in header_cells[1:]:  # skip corner
        text = cell.get_text(" ", strip=True)  # "08:00 hr 08:55 hr"
        times = TIMETABLE_TIME_RE.findall(text)
        if len(times) >= 2:
            time_slots.append((times[0], times[1]))

    if not time_slots:
        raise HomeParseError(
            f"Could not extract time slots from timetable header."
        )

    return time_slots


def _parse_period_cell(cell, day_name: str, weekday: int, period_index: int,
                       start: str, end: str) -> Optional[TimetableSlot]:
    """Parse a single <th> period cell into a TimetableSlot (or None for empty).

    Cell structure:
        <th title="{ER2251 : Mining Geology : Theory}">
            ER2251<br/> <i style="...">{# 205}</i>
        </th>
    OR for empty slots:
        <th title="">-</th>
    OR for the LUNCH rowspan cell:
        <th rowspan="5" ...>L<br/>U<br/>N<br/>C<br/>H</th>
    """
    text = cell.get_text(" ", strip=True)

    # LUNCH cell — NITRIS renders it as vertical letters L<br/>U<br/>N<br/>C<br/>H
    # which bs4 turns into "L U N C H" (with separators). Strip ALL whitespace
    # before checking, so the column-shifted / line-broken forms both match.
    text_compact = "".join(text.upper().split())
    if "LUNCH" in text_compact or text_compact == "LUNCH":
        return TimetableSlot(
            day=day_name, weekday=weekday, period_index=period_index,
            start_time=start, end_time=end,
            subject="LUNCH", room="", is_break=True,
        )

    # Empty slot — NITRIS uses "-" as placeholder
    if not text or text.strip() == "-":
        return None

    # Real class cell — extract subject + room + bonus metadata from title
    title_attr = (cell.get("title") or "").strip()

    subject_code = ""
    subject_name = ""
    course_type = ""

    if title_attr:
        m = re.match(TIMETABLE_TITLE_RE, title_attr)
        if m:
            subject_code = m.group(1).strip()
            subject_name = m.group(2).strip()
            course_type = m.group(3).strip()

    # Fallback: parse subject code from the first text node (in case title is
    # empty or differently formatted). The visible cell text starts with the
    # subject code, then has the <i>{# ...}</i> suffix appended.
    if not subject_code:
        # Get only the leading text node, before any <br/> or <i>
        # (bs4 .contents gives direct children in document order)
        for child in cell.children:
            if isinstance(child, str):
                candidate = child.strip()
                if candidate and candidate != "-":
                    subject_code = candidate
                    break
        if not subject_code:
            # Last-ditch fallback
            subject_code = text.split()[0] if text.split() else ""

    # Room — from <i>{# 205}</i>
    room = ""
    i_el = cell.find("i")
    if i_el:
        i_text = i_el.get_text(" ", strip=True)
        m = re.search(TIMETABLE_ROOM_RE, i_text)
        if m:
            room = m.group(1).strip()

    return TimetableSlot(
        day=day_name, weekday=weekday, period_index=period_index,
        start_time=start, end_time=end,
        subject=subject_code, room=room, is_break=False,
        subject_name=subject_name, course_type=course_type,
    )


def parse_timetable_from_home(html: str) -> list[TimetableSlot]:
    """Parse the timetable widget from Home.aspx HTML.

    Returns a list of TimetableSlot objects (one per non-empty slot, plus
    one LUNCH slot per weekday the LUNCH rowspan covers).

    Raises HomeParseError if the timetable table cannot be located, if the
    header row is missing, or if the tbody is absent.

    Rowspan handling
    -----------------
    NITRIS renders LUNCH as a single `<th rowspan="5">` cell in Monday's row
    that visually spans Mon-Fri. BeautifulSoup does NOT auto-fill the missing
    cells in Tue-Fri rows, so a naive walk would shift Tuesday's classes left
    into the lunch slot. We track active rowspan cells (column → cell +
    remaining rows) and pre-fill the aligned[] array for each row, so the
    column layout stays correct for every day.
    """
    soup = BeautifulSoup(html, HTML_PARSER)
    tbl = _find_timetable_table(soup)
    if tbl is None:
        raise HomeParseError(
            "Timetable table not found in Home.aspx HTML. "
            "Either the student has no timetable published, or NITRIS changed the dashboard markup."
        )

    # Find header row — inside <thead> or first <tr> with time matches
    header_el = tbl.find("thead")
    if not header_el:
        for tr in tbl.find_all("tr"):
            if len(TIMETABLE_TIME_RE.findall(tr.get_text(" ", strip=True))) >= 2:
                header_el = tr
                break

    if not header_el:
        raise HomeParseError("Timetable table header row (with period times) missing.")

    time_slots = _parse_header_times(header_el)
    n_slots = len(time_slots)
    if n_slots == 0:
        raise HomeParseError("Timetable header parsed zero time slots.")

    # Find all data rows
    tbody = tbl.find("tbody")
    if tbody:
        data_rows = tbody.find_all("tr", recursive=False)
    else:
        # Filter out the header row
        data_rows = [r for r in tbl.find_all("tr", recursive=False) if r != header_el]

    if not data_rows:
        raise HomeParseError("Timetable table has no day rows.")

    # Track rowspan cells: col_idx → (cell_obj, remaining_rows_to_span)
    active_rowspans: dict[int, tuple[object, int]] = {}

    entries: list[TimetableSlot] = []

    for row in data_rows:
        cells = row.find_all(["th", "td"], recursive=False)
        if not cells:
            continue

        day_name = cells[0].get_text(strip=True)
        weekday = _DAY_TO_WEEKDAY.get(day_name)
        if weekday is None:
            logger.debug("Skipping unknown timetable row: day_name=%r", day_name)
            continue

        period_cells = cells[1:]  # drop the day label cell

        # Build the aligned column array (one slot per header column).
        # Step A: pre-fill columns that are absorbed by an active rowspan cell.
        aligned: list[Optional[object]] = [None] * n_slots
        for col, (cell_obj, _remaining) in active_rowspans.items():
            if 0 <= col < n_slots:
                aligned[col] = cell_obj

        # Step B: walk this row's explicit cells, placing each at the next
        # FREE column (skipping columns already occupied by rowspan cells).
        col_idx = 0
        new_rowspans: list[tuple[int, object, int]] = []
        for cell in period_cells:
            # Skip past occupied columns
            while col_idx < n_slots and aligned[col_idx] is not None:
                col_idx += 1
            if col_idx >= n_slots:
                logger.warning(
                    "Timetable row %s has more cells than header columns — dropping extras",
                    day_name,
                )
                break

            aligned[col_idx] = cell

            # Register new rowspan cells for future rows
            rs_str = cell.get("rowspan")
            if rs_str:
                try:
                    rs = int(rs_str)
                    if rs >= 2:
                        # This cell will appear in this row + (rs-1) future rows.
                        new_rowspans.append((col_idx, cell, rs - 1))
                except ValueError:
                    pass

            col_idx += 1

        # Step C: decrement remaining rowspans; expire those that hit zero.
        expired_cols = []
        for col in list(active_rowspans.keys()):
            cell_obj, remaining = active_rowspans[col]
            new_remaining = remaining - 1
            if new_remaining <= 0:
                expired_cols.append(col)
            else:
                active_rowspans[col] = (cell_obj, new_remaining)
        for col in expired_cols:
            del active_rowspans[col]

        # Step D: add new rowspans discovered this row.
        for col, cell_obj, rem in new_rowspans:
            active_rowspans[col] = (cell_obj, rem)

        # Step E: emit entries from the aligned array
        for slot_idx, cell in enumerate(aligned):
            if slot_idx >= n_slots:
                break
            start, end = time_slots[slot_idx]
            period_idx = slot_idx + 1  # 1-indexed

            if cell is None:
                # Empty slot — no class and no rowspan covering this column
                continue

            slot = _parse_period_cell(
                cell, day_name, weekday, period_idx, start, end
            )
            if slot is not None:
                entries.append(slot)

    if not entries:
        raise HomeParseError(
            "Timetable parser produced zero entries — the dashboard may be empty or the markup changed."
        )

    return entries


def parse_home_page(html: str) -> HomeParseResult:
    """Parse the full Home.aspx dashboard.

    Currently extracts only the timetable. Future versions will also extract:
      - webmail credentials (per NITRIS_PORTAL_RECON.json `home_page_extracts`)
      - recent messages
      - anti-ragging status
      - calendar month events

    Returns a HomeParseResult with all extracted data.
    """
    slots = parse_timetable_from_home(html)
    return HomeParseResult(timetable=slots, raw_html_bytes=len(html))





@dataclass
class AttendanceRecord:
    subject_code: str
    subject_name: str
    faculty: str
    tc: str
    ua: str
    le: str
    oa: str
    # NITRIS's debar table is keyed on the Lecture-Tutorial-Practical pattern.
    # Empty string on rows where the portal omits it (health engine falls back).
    ltp: str = ""


@dataclass
class AttendanceResult:
    student_info: str
    records: list[AttendanceRecord]

    def to_dict(self) -> dict:
        return {"student_info": self.student_info, "records": [asdict(r) for r in self.records]}


def parse_attendance_html(html: str) -> AttendanceResult:
    """Parse final attendance page HTML. Expects table to already exist."""
    soup = BeautifulSoup(html, HTML_PARSER)

    # Student info
    info_el = soup.find(id=STUDENT_INFO_LABEL_ID)
    student_info = info_el.get_text(strip=True) if info_el else "Unknown Student"

    # Attendance table
    table = soup.find(id=ATTENDANCE_TABLE_ID)
    if not table:
        raise AttendanceParseError("Attendance table not found in HTML.")

    rows = table.find_all("tr", recursive=False)
    if not rows:
        # Sometimes GridView renders rows inside a tbody
        tbody = table.find("tbody")
        if tbody:
            rows = tbody.find_all("tr", recursive=False)
            
    if len(rows) < 2:
        raise AttendanceParseError(f"Attendance table has insufficient rows (found {len(rows)}).")

    # Dynamic header mapping
    headers = [th.get_text(strip=True) for th in rows[0].find_all(["th", "td"])]
    col = _map_columns(headers)
    logger.debug("Mapped headers: %s", headers)

    # Extract records
    records: list[AttendanceRecord] = []
    skipped = 0
    
    for row in rows[1:]:
        cells = row.find_all("td", recursive=False)
        
        # Skip rows with insufficient cells (often pagers or footers)
        if len(cells) <= col["max_idx"]:
            skipped += 1
            continue

        # Check for colspan which usually indicates a pager/template row
        if any(cell.has_attr("colspan") for cell in cells):
            skipped += 1
            continue

        tc_val = _cell(cells, col.get("tc"))
        
        # Skip rows where TC is not numeric (likely empty or header/footer row)
        if not tc_val or not tc_val.isdigit():
            skipped += 1
            continue

        record = AttendanceRecord(
            subject_code=_cell(cells, col.get("subject_code")),
            subject_name=_cell(cells, col.get("subject_name")),
            faculty=_cell(cells, col.get("faculty")),
            tc=tc_val,
            ua=_cell(cells, col.get("ua")),
            le=_cell(cells, col.get("le")),
            oa=_cell(cells, col.get("oa")),
            ltp=_cell(cells, col.get("ltp")),
        )
        records.append(record)

    logger.info(
        "Parser stats - Headers: %d, Total Rows: %d, Valid Records: %d, Skipped: %d", 
        len(headers), len(rows), len(records), skipped
    )

    if not records:
        raise AttendanceParseError("No valid attendance records found after filtering rows.")

    logger.info("Successfully parsed attendance for %s", student_info)
    return AttendanceResult(student_info=student_info, records=records)


def _map_columns(headers: list[str]) -> dict[str, int]:
    """Map column names to indices. Case-insensitive, partial match."""
    mapping: dict[str, Optional[int]] = {
        "subject_code": None, "subject_name": None, "faculty": None,
        "tc": None, "ua": None, "le": None, "oa": None, "ltp": None,
    }

    aliases = {
        "subject_code": ["subject code", "sub code", "code"],
        "subject_name": ["subject name", "course name", "sub name", "subject", "name"],
        "faculty": ["faculty name", "faculty", "teacher"],
        "tc": ["tc"], "ua": ["ua"], "le": ["le"], "oa": ["oa"],
        # Header renders as "L-T-P"; match before generic patterns.
        "ltp": ["l-t-p", "ltp", "l t p"],
    }

    for i, header in enumerate(headers):
        h = header.lower().strip()
        for key, names in aliases.items():
            if mapping[key] is None and any(n in h for n in names):
                mapping[key] = i
                break

    # TC/UA/LE/OA are mandatory
    for required in ("tc", "ua", "le", "oa"):
        if mapping[required] is None:
            raise AttendanceParseError(f"Required column '{required.upper()}' not found. Found headers: {headers}")

    indices = [v for v in mapping.values() if v is not None]
    mapping["max_idx"] = max(indices) if indices else 0
    return mapping


def _cell(cells: list, idx: Optional[int]) -> str:
    """Safely get text from a cell by index."""
    if idx is None or idx >= len(cells):
        return ""
    return cells[idx].get_text(strip=True)


# ── Message Inbox Parsers ────────────────────────────────────

import urllib.parse
from app.nitris.constants import (
    MESSAGES_TABLE_ID, MSG_FROM_LABEL_ID, MSG_SENTON_LABEL_ID, MSG_SUBJECT_LABEL_ID, MSG_BODY_LABEL_ID
)

def extract_message_id(token: str) -> Optional[int]:
    """Extract stable numeric message ID from Base64-encoded token."""
    import base64
    import urllib.parse
    if not token or token.startswith("postback:"):
        return None
    try:
        decoded_token = urllib.parse.unquote(token)
        b64_part = decoded_token.split("-")[0]
        # URL safe decoding with correct padding
        padded = b64_part + "=" * (4 - len(b64_part) % 4)
        decoded_bytes = base64.urlsafe_b64decode(padded.encode("utf-8"))
        return int(decoded_bytes.decode("utf-8"))
    except Exception:
        return None

def _content_portal_id(sender: str, subject: str, sent_on: datetime) -> int:
    """Deterministic, collision-resistant portal_message_id from message identity.

    Used as the fallback when a portal token's numeric ID cannot be decoded
    (token format changed) or for historical messages whose ASP.NET postback
    token is unstable. 60-bit SHA-256 digest fits the BigInteger column and
    requires two messages with byte-identical (sender, subject, sent_on) to
    collide.
    """
    import hashlib
    digest = hashlib.sha256(
        f"{sender}|{subject}|{sent_on.isoformat()}".encode("utf-8")
    ).hexdigest()
    return int(digest[:15], 16)


def parse_messages_list_html(html: str) -> list[dict]:
    """Parse AllMessages.aspx page HTML and return raw list of message headers.
    
    Supports Chrome view-source pages.
    """
    from datetime import datetime
    import re
    import hashlib
    import base64
    
    soup = BeautifulSoup(html, HTML_PARSER)
    
    # Handle Chrome view-source
    td_lines = soup.find_all('td', class_='line-content')
    if td_lines:
        reconstructed = "\n".join(td.get_text() for td in td_lines)
        soup = BeautifulSoup(reconstructed, HTML_PARSER)
        
    # 1. Parse all message items in the notification dropdown to get direct tokens
    dropdown_messages = []
    dropdown_items = soup.find_all("a", class_="message-item")
    for item in dropdown_items:
        href = item.get("href", "")
        if "Message.aspx?i=" not in href:
            continue
            
        token = href.split("Message.aspx?i=")[-1]
        
        mail_content = item.find("div", class_="mail-contnet")
        if not mail_content:
            continue
            
        title_el = mail_content.find("span", class_="message-title")
        desc_el = mail_content.find("span", class_="mail-desc")
        time_el = mail_content.find("span", class_="time")
        
        subject = title_el.get_text(strip=True) if title_el else ""
        sender = desc_el.get_text(strip=True) if desc_el else ""
        time_str = time_el.get_text(strip=True) if time_el else ""
        
        # Parse date
        try:
            sent_on_date = datetime.strptime(time_str, "%d %b %Y")
        except Exception:
            try:
                from email.utils import parsedate_to_datetime
                sent_on_date = parsedate_to_datetime(time_str)
            except Exception:
                sent_on_date = datetime.now()

        # Parse portal ID - fall back to a content hash when the token's numeric
        # ID cannot be decoded (never 0, which would collide across messages).
        portal_id = extract_message_id(token)
        if portal_id is None:
            portal_id = _content_portal_id(sender, subject, sent_on_date)
                
        dropdown_messages.append({
            "portal_message_id": portal_id,
            "token": token,
            "sender": sender,
            "subject": subject,
            "sent_on": sent_on_date
        })
        
    # 2. Parse the GridView table
    table = soup.find(id=MESSAGES_TABLE_ID)

    # If neither the GridView nor the notification dropdown rendered, this is
    # not the expected messages page (NITRIS markup changed or an unexpected
    # page). Raise so the sync marks a failure and backs off instead of
    # silently treating it as an empty inbox (which would stop new-message
    # delivery with no error signal).
    if table is None and not dropdown_items:
        raise InboxParseError(
            "No message container found (neither the messages GridView nor the "
            "notification dropdown) - NITRIS messages markup may have changed."
        )

    if not table:
        # If table is not found, we fallback to dropdown notification items only!
        return dropdown_messages
        
    rows = table.find_all("tr", recursive=False)
    if not rows:
        tbody = table.find("tbody")
        if tbody:
            rows = tbody.find_all("tr", recursive=False)
            
    if not rows or len(rows) < 2:
        return dropdown_messages
        
    grid_messages = []
    
    for row in rows[1:]:
        cells = row.find_all(["td", "th"], recursive=False)
        if len(cells) < 5:
            continue
            
        # Pager row check
        if any(cell.has_attr("colspan") for cell in cells):
            continue
            
        idx_str = cells[0].get_text(strip=True)
        if not idx_str.isdigit():
            continue
            
        sender = cells[1].get_text(strip=True)
        subject_cell = cells[2]
        subject = subject_cell.get_text(strip=True)
        sent_on_str = cells[3].get_text(strip=True)
        
        # Get target from postback
        link_el = cells[4].find("a")
        if not link_el:
            continue
            
        href = link_el.get("href", "")
        # Extract target from: javascript:__doPostBack('ctl00$ContentPlaceHolder2$gvSubjects$ctl02$lnkViewMsg','')
        match = re.search(r"__doPostBack\('([^']*)'", href)
        if not match:
            continue
        postback_target = match.group(1)
        
        # Parse Sent On date
        try:
            sent_on_date = datetime.strptime(sent_on_str, "%d %b %Y")
        except Exception:
            try:
                from email.utils import parsedate_to_datetime
                sent_on_date = parsedate_to_datetime(sent_on_str)
            except Exception:
                sent_on_date = datetime.now()
                
        # 3. Match row against dropdown messages to get direct token
        matched_token = None
        matched_portal_id = None
        
        for dm in dropdown_messages:
            # Match sender, subject, and date
            s_match = dm["sender"].lower().strip() == sender.lower().strip()
            sub_match = dm["subject"].lower().strip() == subject.lower().strip()
            date_match = dm["sent_on"].date() == sent_on_date.date()
            
            if s_match and sub_match and date_match:
                matched_token = dm["token"]
                matched_portal_id = dm["portal_message_id"]
                break
                
        if matched_token:
            token = matched_token
            portal_id = matched_portal_id
        else:
            # Older historical message: use the postback target as token!
            token = f"postback:{postback_target}"
            # Generate deterministic portal message ID (collision-resistant, 60-bit)
            portal_id = _content_portal_id(sender, subject, sent_on_date)
            
        grid_messages.append({
            "portal_message_id": portal_id,
            "token": token,
            "sender": sender,
            "subject": subject,
            "sent_on": sent_on_date
        })
        
    # Dedupe by portal_message_id. Two distinct historical messages with
    # identical (sender, subject, date) hash to the same ID, and the DB enforces
    # UNIQUE(user_id, portal_message_id). Keeping the first avoids a constraint
    # violation that would roll back the whole sync.
    result = grid_messages if grid_messages else dropdown_messages
    seen: set = set()
    deduped = []
    for m in result:
        pid = m["portal_message_id"]
        if pid in seen:
            continue
        seen.add(pid)
        deduped.append(m)
    return deduped



def parse_message_detail_html(html: str) -> dict:
    """Parse Message.aspx detail page HTML.
    
    Returns a dict containing: sender, sent_on, subject, body, attachment_url.
    """
    soup = BeautifulSoup(html, HTML_PARSER)
    
    # Handle Chrome view-source
    td_lines = soup.find_all('td', class_='line-content')
    if td_lines:
        reconstructed = "\n".join(td.get_text() for td in td_lines)
        soup = BeautifulSoup(reconstructed, HTML_PARSER)
        
    from_el = soup.find(id=MSG_FROM_LABEL_ID)
    senton_el = soup.find(id=MSG_SENTON_LABEL_ID)
    subject_el = soup.find(id=MSG_SUBJECT_LABEL_ID)
    body_el = soup.find(id=MSG_BODY_LABEL_ID)
    
    sender = from_el.get_text(strip=True) if from_el else "Unknown Sender"
    subject = subject_el.get_text(strip=True) if subject_el else "No Subject"
    body = body_el.get_text("\n", strip=True) if body_el else ""
    sent_on_str = senton_el.get_text(strip=True) if senton_el else ""
    
    # Extract attachment relative href
    attachment_url = None
    anchors = soup.find_all("a")
    for a in anchors:
        href = a.get("href", "")
        text = a.get_text(strip=True).lower()
        if href and ("docs" in href or "attachment" in text or "pdf" in href.lower()):
            if "../../" in href:
                # Convert '../../docs/ReachYourStudent/file.pdf' -> '/nitris/docs/ReachYourStudent/file.pdf'
                attachment_url = "/nitris/" + href.split("../../")[-1]
            else:
                attachment_url = href
            break
            
    return {
        "sender": sender,
        "subject": subject,
        "body": body,
        "sent_on_str": sent_on_str,
        "attachment_url": attachment_url
    }

