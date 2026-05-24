"""Parse the final rendered attendance HTML into structured data."""

import logging
from dataclasses import dataclass, asdict
from typing import Optional
from bs4 import BeautifulSoup
from app.nitris.constants import ATTENDANCE_TABLE_ID, STUDENT_INFO_LABEL_ID
from app.nitris.exceptions import AttendanceParseError

logger = logging.getLogger(__name__)


@dataclass
class AttendanceRecord:
    subject_code: str
    subject_name: str
    faculty: str
    tc: str
    ua: str
    le: str
    oa: str


@dataclass
class AttendanceResult:
    student_info: str
    records: list[AttendanceRecord]

    def to_dict(self) -> dict:
        return {"student_info": self.student_info, "records": [asdict(r) for r in self.records]}


def parse_attendance_html(html: str) -> AttendanceResult:
    """Parse final attendance page HTML. Expects table to already exist."""
    soup = BeautifulSoup(html, "html.parser")

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
        "tc": None, "ua": None, "le": None, "oa": None,
    }
    
    aliases = {
        "subject_code": ["subject code", "sub code", "code"],
        "subject_name": ["subject name", "course name", "sub name", "subject", "name"],
        "faculty": ["faculty name", "faculty", "teacher"],
        "tc": ["tc"], "ua": ["ua"], "le": ["le"], "oa": ["oa"],
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

def parse_messages_list_html(html: str) -> list[dict]:
    """Parse AllMessages.aspx page HTML and return raw list of message headers.
    
    Supports Chrome view-source pages.
    """
    from datetime import datetime
    import re
    import hashlib
    import base64
    
    soup = BeautifulSoup(html, "html.parser")
    
    # Handle Chrome view-source
    td_lines = soup.find_all('td', class_='line-content')
    if td_lines:
        reconstructed = "\n".join(td.get_text() for td in td_lines)
        soup = BeautifulSoup(reconstructed, "html.parser")
        
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
        
        # Parse portal ID
        portal_id = extract_message_id(token) or 0
            
        # Parse date
        try:
            sent_on_date = datetime.strptime(time_str, "%d %b %Y")
        except Exception:
            try:
                from email.utils import parsedate_to_datetime
                sent_on_date = parsedate_to_datetime(time_str)
            except Exception:
                sent_on_date = datetime.now()
                
        dropdown_messages.append({
            "portal_message_id": portal_id,
            "token": token,
            "sender": sender,
            "subject": subject,
            "sent_on": sent_on_date
        })
        
    # 2. Parse the GridView table
    table = soup.find(id=MESSAGES_TABLE_ID)
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
            # Generate deterministic portal message ID
            h = hashlib.sha256(f"{sender}:{subject}:{sent_on_date.isoformat()}".encode("utf-8")).hexdigest()
            portal_id = int(h[:8], 16)
            
        grid_messages.append({
            "portal_message_id": portal_id,
            "token": token,
            "sender": sender,
            "subject": subject,
            "sent_on": sent_on_date
        })
        
    return grid_messages if grid_messages else dropdown_messages



def parse_message_detail_html(html: str) -> dict:
    """Parse Message.aspx detail page HTML.
    
    Returns a dict containing: sender, sent_on, subject, body, attachment_url.
    """
    soup = BeautifulSoup(html, "html.parser")
    
    # Handle Chrome view-source
    td_lines = soup.find_all('td', class_='line-content')
    if td_lines:
        reconstructed = "\n".join(td.get_text() for td in td_lines)
        soup = BeautifulSoup(reconstructed, "html.parser")
        
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

