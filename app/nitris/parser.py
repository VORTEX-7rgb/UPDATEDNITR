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
