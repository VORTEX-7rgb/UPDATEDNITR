"""Parse ASP.NET Examination Seating Chart pages into structured data.

Reverse-engineered from live NITRIS session (Mid_Semester.aspx & View_Sitting_Details.aspx).
See docs/NITRIS_EXAM_SEATING_RECON.json for the full protocol recon.

Physical Orientation Specification:
- NITRIS displays the room seating chart facing the BLACKBOARD / PODIUM at the FRONT.
- Rows: Horizontal benches from FRONT (Row 1) to BACK (Row N).
- Columns: Vertical seat positions from LEFT (Column 1) to RIGHT (Column N) facing the board.
- Front neighbour: (row - 1, col)
- Behind neighbour: (row + 1, col)
- Left neighbour: (row, col - 1)
- Right neighbour: (row, col + 1)

This module is pure (no I/O) — taking HTML in and returning typed dataclasses out.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Optional

from bs4 import BeautifulSoup, Tag

from app.nitris.constants import EXAM_SEATING_GRID_ID, HTML_PARSER
from app.nitris.exceptions import ExamSeatingParseError

logger = logging.getLogger(__name__)

_DOPOSTBACK_RE = re.compile(r"__doPostBack\('([^']+)'", re.IGNORECASE)
_ROW_LABEL_RE = re.compile(r"Row\s*(\d+)", re.IGNORECASE)
_COL_LABEL_RE = re.compile(r"Column\s*(\d+)", re.IGNORECASE)


@dataclass(frozen=True)
class ExamScheduleItem:
    """A single exam entry in the student's exam schedule grid."""

    num: int
    day: str
    date_str: str
    time_str: str
    seating: str
    subject: str
    room_no: str
    postback_target: str


@dataclass(frozen=True)
class SeatPosition:
    """Coordinates of a student's seat in an exam hall."""

    row: int  # 1-indexed: Row 1 is closest to the Blackboard (Front)
    col: int  # 1-indexed: Column 1 is leftmost facing the Blackboard
    total_rows: int
    total_cols: int


@dataclass(frozen=True)
class NeighbourInfo:
    """The 4-way adjacent student roll numbers surrounding a seat."""

    left: Optional[str] = None
    right: Optional[str] = None
    front: Optional[str] = None
    behind: Optional[str] = None


@dataclass(frozen=True)
class RoomLayoutResult:
    """Parsed room layout and student positioning from View_Sitting_Details.aspx."""

    academic_year: str
    examination: str
    exam_date_seating: str
    subject: str
    room_no: str
    student_seat: Optional[SeatPosition]
    neighbours: Optional[NeighbourInfo]
    total_seats: int
    grid: dict[tuple[int, int], str]  # (row, col) -> roll_number


def parse_exam_schedule_html(html: str) -> list[ExamScheduleItem]:
    """Parse the gvExamSchedule table on Mid_Semester.aspx or End_Semester.aspx.

    Returns an empty list if the schedule is not yet published (off-season, rowCount <= 1).
    Raises ExamSeatingParseError if the page structure is unexpected or broken.
    """
    if not html or not html.strip():
        raise ExamSeatingParseError("Empty HTML provided to exam schedule parser.")

    soup = BeautifulSoup(html, HTML_PARSER)

    table = soup.find("table", id=lambda x: x and EXAM_SEATING_GRID_ID in x)
    if not table:
        # Check if table exists with simple id check
        table = soup.find("table", id=EXAM_SEATING_GRID_ID)

    if not table:
        # Off-season or pre-selection: check if form/container exists
        if "ddlSemesterType" in html or "Examination Seating Chart" in html:
            # Dropdown page without grid rendered yet
            return []
        raise ExamSeatingParseError("Exam schedule table (gvExamSchedule) not found in HTML.")

    rows = table.find_all("tr")
    if len(rows) <= 1:
        # Off-season behavior: Grid renders with only the header row (rowCount=1).
        logger.info("Exam schedule table has <= 1 row; returning empty schedule.")
        return []

    items: list[ExamScheduleItem] = []
    # Skip header row (index 0)
    for row_idx, row in enumerate(rows[1:], start=1):
        cells = row.find_all(["td", "th"])
        if len(cells) < 7:
            logger.debug("Skipping non-data row with %d cells in schedule grid.", len(cells))
            continue

        num_txt = cells[0].get_text(strip=True)
        try:
            num = int(num_txt)
        except ValueError:
            num = row_idx

        day = cells[1].get_text(strip=True)
        date_str = cells[2].get_text(strip=True)
        time_str = cells[3].get_text(strip=True)
        seating = cells[4].get_text(strip=True)
        subject = cells[5].get_text(strip=True)
        room_no = cells[6].get_text(strip=True)

        postback_target = ""
        # Link button is in column 7 ("Details")
        a_tag = row.find("a")
        if a_tag:
            href = a_tag.get("href", "")
            match = _DOPOSTBACK_RE.search(href)
            if match:
                postback_target = match.group(1)
            else:
                a_id = a_tag.get("id", "")
                if a_id:
                    postback_target = a_id.replace("_", "$")

        items.append(
            ExamScheduleItem(
                num=num,
                day=day,
                date_str=date_str,
                time_str=time_str,
                seating=seating,
                subject=subject,
                room_no=room_no,
                postback_target=postback_target,
            )
        )

    return items


def parse_room_sitting_details_html(
    html: str, target_roll: Optional[str] = None
) -> RoomLayoutResult:
    """Parse View_Sitting_Details.aspx to extract exam metadata, room grid, and student seat.

    Args:
        html: Raw HTML string of View_Sitting_Details.aspx.
        target_roll: The roll number of the student viewing the chart (optional).

    Returns:
        RoomLayoutResult containing room metadata, seat coordinates, neighbours, and full grid.

    Raises:
        ExamSeatingParseError if the room grid cannot be found or parsed.
    """
    if not html or not html.strip():
        raise ExamSeatingParseError("Empty HTML provided to room sitting details parser.")

    soup = BeautifulSoup(html, HTML_PARSER)

    # Extract metadata fields with whitespace normalization
    full_text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))

    academic_year = _extract_meta_field(full_text, r"Academic Year(?:/Session)?")
    examination = _extract_meta_field(full_text, r"Examination")
    exam_date_seating = _extract_meta_field(full_text, r"Exam Date(?:/Sitting)?")
    subject = _extract_meta_field(full_text, r"Subject")
    room_no = _extract_meta_field(full_text, r"Room No")

    # Locate the room grid table: contains "Column 1" and "Row 1" (case-insensitive)
    # Prefer innermost table without nested tables to avoid wrapper containers
    grid_table: Optional[Tag] = None
    for table in soup.find_all("table"):
        norm_table_txt = re.sub(r"\s+", " ", table.get_text(" ", strip=True))
        if re.search(r"column\s*1", norm_table_txt, re.I) and re.search(r"row\s*1", norm_table_txt, re.I):
            grid_table = table
            if not table.find("table"):
                break

    if not grid_table:
        raise ExamSeatingParseError("Room layout grid table not found in HTML.")

    # Parse rows of the grid
    tr_tags = grid_table.find_all("tr")
    col_mapping: dict[int, int] = {}  # cell_index -> column_number (1-indexed)
    grid: dict[tuple[int, int], str] = {}

    for tr in tr_tags:
        cells = tr.find_all(["td", "th"])
        row_text = re.sub(r"\s+", " ", tr.get_text(" ", strip=True))

        # Check for column header row (e.g. Column 1, Column 2, ...)
        if not col_mapping and any(re.search(r"Column\s*\d+", c.get_text(), re.I) for c in cells):
            for cell_idx, cell in enumerate(cells):
                txt = re.sub(r"\s+", " ", cell.get_text(" ", strip=True))
                col_match = _COL_LABEL_RE.search(txt)
                if col_match:
                    col_mapping[cell_idx] = int(col_match.group(1))
            continue

        # Check for data rows (Row 1, Row 2, ...)
        row_match = _ROW_LABEL_RE.search(row_text)
        if row_match and col_mapping:
            row_num = int(row_match.group(1))
            for cell_idx, col_num in col_mapping.items():
                if cell_idx < len(cells):
                    roll = cells[cell_idx].get_text(strip=True)
                    # Ignore non-roll placeholder characters
                    if roll and roll not in ("-", "--", "&nbsp;"):
                        grid[(row_num, col_num)] = roll

    if not grid:
        raise ExamSeatingParseError("No student seats found in room layout grid table.")

    total_seats = len(grid)
    max_row = max(r for r, _ in grid.keys())
    max_col = max(c for _, c in grid.keys())

    # Find student seat and neighbours
    student_seat: Optional[SeatPosition] = None
    neighbours: Optional[NeighbourInfo] = None

    if target_roll:
        normalized_target = target_roll.strip().upper()
        for (r, c), roll in grid.items():
            if roll.strip().upper() == normalized_target:
                student_seat = SeatPosition(
                    row=r,
                    col=c,
                    total_rows=max_row,
                    total_cols=max_col,
                )
                # Boundary-safe neighbour lookup
                neighbours = NeighbourInfo(
                    left=grid.get((r, c - 1)),
                    right=grid.get((r, c + 1)),
                    front=grid.get((r - 1, c)),
                    behind=grid.get((r + 1, c)),
                )
                break

    return RoomLayoutResult(
        academic_year=academic_year,
        examination=examination,
        exam_date_seating=exam_date_seating,
        subject=subject,
        room_no=room_no,
        student_seat=student_seat,
        neighbours=neighbours,
        total_seats=total_seats,
        grid=grid,
    )


def _extract_meta_field(text: str, label_pattern: str) -> str:
    """Extract a metadata value by label regex from page text, returning stripped string or empty."""
    norm = re.sub(r"\s+", " ", text).strip()
    pattern = rf"{label_pattern}\s*:\s*(.*?)(?=\s+(?:Academic Year|Examination|Exam Date|Subject|Room No|BLACK\s*BOARD|Column\s*1|$))"
    match = re.search(pattern, norm, re.IGNORECASE)
    if match:
        return match.group(1).strip()
    return ""
