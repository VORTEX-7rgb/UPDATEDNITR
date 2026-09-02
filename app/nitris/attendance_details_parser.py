"""Parse NITRIS's per-subject "Subject/Date wise Attendance Details" page.

Reverse-engineered from the live portal (ClassAttendanceDetails.aspx, captured
2026-09-01 — see docs/ATTENDANCE_DETAILS_RECON.json for the full anatomy):

Page anatomy
------------
* Header labels (label/value pairs rendered as "Label : Value"):
    "Student Name / RollNo : ARADHY SINGH CHAUHAN {725MN1011}"
    "Academic Year/Session : 2026-27 / Autumn"
    "Subject : ER2251 : Mining Geology"
* ONE GridView matrix table:
    header row : "Class No" | 1 | 2 | 3 | …  (class ordinals)
    month rows : "July (5) / Submitted"  | <day cells, colored by outcome>
                 "August (11) / Pending" | <day cells, colored by outcome>
    totals row : "Total Class (16)" | "Present = 13" | "Absent = 3"
                 | "Leave = 0" | "Overall Absence (Absent + Leave) = 3"
* Day cells carry their outcome as an INLINE background-color (sometimes a
  bgcolor attribute). Palette (see constants for the status keys):
    green  = Present                    red    = Absent
    blue   = Leave Sanctioned           orange = Present (Late Registration)
    pink   = Absent (Late Registration)
  Per the page NOTE, until a month's attendance is submitted every cell
  defaults to red (ABSENT).

Link harvesting
---------------
The ClassAttendance.aspx grid links each subject row to its details page via
an href containing "ClassAttendanceDetails.aspx?ApId=<b64>-<token>&AppName=…"
whose security tokens ROTATE periodically. Exactly like the module-URL
resolver, tokens are never stored or guessed — `extract_details_link()`
harvests the CURRENT href from the live attendance page HTML at runtime and
matches it to a subject by the text of the row that contains the link.

This module is PURE: html-in, dataclass-out. No DB, no Telegram, no portal.
Parse functions run inside worker threads — callers must never run them on
the event loop (BS4 over 100KB+ of ASP.NET HTML would stall it).
"""
from __future__ import annotations

import colorsys
import html as html_mod
import logging
import posixpath
import re
from dataclasses import dataclass, asdict
from typing import Optional

from bs4 import BeautifulSoup

from app.nitris.constants import (
    ATTENDANCE_DETAILS_LINK_KEYWORD,
    ATTENDANCE_PAGE_PATH,
    DETAILS_STATUS_PRESENT,
    DETAILS_STATUS_ABSENT,
    DETAILS_STATUS_LEAVE,
    DETAILS_STATUS_PRESENT_LATE,
    DETAILS_STATUS_ABSENT_LATE,
    DETAILS_STATUS_UNKNOWN,
    HTML_PARSER,
)
from app.nitris.exceptions import AttendanceParseError

logger = logging.getLogger(__name__)

# The folder the details page lives in — relative grid hrefs resolve against it.
_DETAILS_DIR = posixpath.dirname(ATTENDANCE_PAGE_PATH)  # /nitris/Student/Attendance

_MONTH_NAMES = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)

# "July (5) / Submitted" · "August (11) /Pending" · "September (0) / Pending"
_MONTH_ROW_RE = re.compile(
    rf"^({'|'.join(_MONTH_NAMES)})\s*\((\d+)\)\s*(?:/\s*)?(Submitted|Pending)?\s*$",
    re.IGNORECASE,
)

# "Total Class (16)" | "Present = 13" | "Absent = 3" | "Leave = 0"
# | "Overall Absence (Absent + Leave) = 3"
_TOTALS_TOTAL_RE = re.compile(r"Total\s*Class\s*\(\s*(\d+)\s*\)", re.IGNORECASE)
_TOTALS_PRESENT_RE = re.compile(r"Present\s*=\s*(\d+)", re.IGNORECASE)
_TOTALS_ABSENT_RE = re.compile(r"Absent\s*=\s*(\d+)", re.IGNORECASE)
_TOTALS_LEAVE_RE = re.compile(r"Leave\s*=\s*(\d+)", re.IGNORECASE)
_TOTALS_OVERALL_RE = re.compile(r"Overall\s*Absence[^=]*=\s*(\d+)", re.IGNORECASE)

# inline style background declarations: "background-color:#00B050" /
# "background:#FFF" / "background-color: rgb(0, 176, 80)"
_BG_COLOR_RE = re.compile(
    r"background(?:-color)?\s*:\s*([^;\"']+)", re.IGNORECASE
)


# ── Data model (snapshot_json shape — keep stable, it is persisted) ─────────

@dataclass(frozen=True)
class DetailsDay:
    """One attended/missed class date inside a month row."""
    class_no: int      # ordinal under the "Class No" header (1-based)
    day: int           # day-of-month shown in the cell (e.g. 27)
    status: str        # one of the DETAILS_STATUS_* constants


@dataclass
class DetailsMonth:
    """One month row of the matrix."""
    name: str                  # "July"
    count: int                 # "(5)" — classes held that month
    submission: str            # "Submitted" / "Pending" / "" (unlabeled)
    days: list[DetailsDay]

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "count": self.count,
            "submission": self.submission,
            "days": [asdict(d) for d in self.days],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "DetailsMonth":
        return cls(
            name=str(d.get("name") or ""),
            count=int(d.get("count") or 0),
            submission=str(d.get("submission") or ""),
            days=[
                DetailsDay(
                    class_no=int(x.get("class_no") or 0),
                    day=int(x.get("day") or 0),
                    status=str(x.get("status") or DETAILS_STATUS_UNKNOWN),
                )
                for x in (d.get("days") or [])
            ],
        )


@dataclass
class SubjectAttendanceDetails:
    """Full parsed details page for ONE subject."""
    student_info: str            # "ARADHY SINGH CHAUHAN {725MN1011}"
    session_label: str           # "2026-27 / Autumn"
    subject_label: str           # "ER2251 : Mining Geology"
    months: list[DetailsMonth]
    totals: dict[str, int]       # total/present/absent/leave/overall_absence

    def to_dict(self) -> dict:
        return {
            "student_info": self.student_info,
            "session_label": self.session_label,
            "subject_label": self.subject_label,
            "months": [m.to_dict() for m in self.months],
            "totals": dict(self.totals),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "SubjectAttendanceDetails":
        return cls(
            student_info=str(d.get("student_info") or ""),
            session_label=str(d.get("session_label") or ""),
            subject_label=str(d.get("subject_label") or ""),
            months=[DetailsMonth.from_dict(m) for m in (d.get("months") or [])],
            totals={
                k: int(v) for k, v in (d.get("totals") or {}).items()
                if isinstance(v, (int, float)) or str(v).strip().lstrip("-").isdigit()
            },
        )


# ── Link harvesting (ClassAttendance.aspx → details href per subject) ───────

def normalize_details_href(href: str) -> str:
    """Resolve a harvested details href to an absolute portal path+query.

    Grid hrefs are usually folder-relative ("ClassAttendanceDetails.aspx?…"),
    sometimes root-relative, occasionally ../../-relative. Query tokens
    (ApId=-<token>&AppName=…) must survive BYTE-FOR-BYTE — the '=' padding
    and '-' separators are meaningful; no re-encoding ever happens here.
    """
    clean = html_mod.unescape((href or "").strip())
    if not clean:
        return ""
    if clean.startswith("/"):
        return clean
    return posixpath.normpath(posixpath.join(_DETAILS_DIR, clean))


def extract_details_target(attendance_html: str, subject_code: str) -> Optional[tuple[str, str]]:
    """Harvest the details link OR postback event target for ONE subject.

    On the live NITRIS portal, each row in gvSubjects renders an ASP.NET LinkButton:
      href="javascript:__doPostBack('ctl00$...$btnDetails','')"
    When clicked, ASP.NET returns a 302 redirect with the dynamically signed
    ClassAttendanceDetails.aspx URL in the Location header.

    Some mock/test environments or direct-link variants instead render a direct
    <a href="ClassAttendanceDetails.aspx?...">.

    Returns:
      - ("link", absolute_url) if a direct URL exists.
      - ("postback", event_target) if an ASP.NET postback exists.
      - None if no details link/button exists for this subject.
    """
    if not attendance_html:
        return None
    code = (subject_code or "").strip().lower()
    if not code:
        return None

    soup = BeautifulSoup(attendance_html, HTML_PARSER)
    kw = ATTENDANCE_DETAILS_LINK_KEYWORD.lower()

    # Prefer the subject grid table (gvSubjects) to avoid outer layout tables
    table = soup.find(id=lambda x: x and "gvsubjects" in x.lower())
    search_root = table if table is not None else soup

    for tr in search_root.find_all("tr"):
        cells = tr.find_all(["td", "th"])
        if not cells:
            continue

        # Check if this specific row matches the subject code
        row_text = " ".join(c.get_text(" ", strip=True) for c in cells).lower()
        if code not in row_text:
            continue

        # 1. Direct link on this row
        for a in tr.find_all("a", href=True):
            href = a.get("href") or ""
            if kw in href.lower():
                absolute = normalize_details_href(href)
                if absolute:
                    return ("link", absolute)

        # 2. ASP.NET postback link button on this row
        for a in tr.find_all("a", href=True):
            href = a.get("href") or ""
            if "__dopostback" in href.lower():
                m = re.search(r"__doPostBack\('([^']+)'", href)
                if m:
                    return ("postback", m.group(1))

    # Fallback: single direct link candidate in page
    candidates: list[str] = []
    for a in soup.find_all("a", href=True):
        href = a.get("href") or ""
        if kw in href.lower():
            absolute = normalize_details_href(href)
            if absolute:
                candidates.append(absolute)

    if len(candidates) == 1:
        return ("link", candidates[0])

    logger.info(
        "No details target matched subject %s (%d candidate(s) on page).",
        subject_code, len(candidates),
    )
    return None


def extract_details_link(attendance_html: str, subject_code: str) -> Optional[str]:
    """Harvest the ClassAttendanceDetails.aspx direct href for ONE subject."""
    target = extract_details_target(attendance_html, subject_code)
    if target and target[0] == "link":
        return target[1]
    return None


# ── Cell color → outcome classification ─────────────────────────────────────

# Exact hexes observed on the live portal (and their obvious Office cousins).
_EXACT_HEX_STATUS: dict[str, str] = {
    # greens — Present
    "00b050": DETAILS_STATUS_PRESENT, "92d050": DETAILS_STATUS_PRESENT,
    "008000": DETAILS_STATUS_PRESENT, "70ad47": DETAILS_STATUS_PRESENT,
    "00cc00": DETAILS_STATUS_PRESENT, "33cc33": DETAILS_STATUS_PRESENT,
    # reds — Absent (default before submission)
    "ff0000": DETAILS_STATUS_ABSENT, "c00000": DETAILS_STATUS_ABSENT,
    "ff3333": DETAILS_STATUS_ABSENT, "e06666": DETAILS_STATUS_ABSENT,
    "cc0000": DETAILS_STATUS_ABSENT,
    # blues — Leave Sanctioned
    "0070c0": DETAILS_STATUS_LEAVE, "4472c4": DETAILS_STATUS_LEAVE,
    "5b9bd5": DETAILS_STATUS_LEAVE, "0000ff": DETAILS_STATUS_LEAVE,
    "00b0f0": DETAILS_STATUS_LEAVE,
    # oranges/yellows — Present (Late Registration/Admission)
    "ffc000": DETAILS_STATUS_PRESENT_LATE, "ed7d31": DETAILS_STATUS_PRESENT_LATE,
    "ffa500": DETAILS_STATUS_PRESENT_LATE, "ffff00": DETAILS_STATUS_PRESENT_LATE,
    "f4b183": DETAILS_STATUS_PRESENT_LATE,
    # pinks — Absent (Late Registration)
    "ff99cc": DETAILS_STATUS_ABSENT_LATE, "ff80c0": DETAILS_STATUS_ABSENT_LATE,
    "ffb3d9": DETAILS_STATUS_ABSENT_LATE, "ff66cc": DETAILS_STATUS_ABSENT_LATE,
    "ffc0cb": DETAILS_STATUS_ABSENT_LATE,
}


def _parse_color(raw: str) -> Optional[tuple[int, int, int]]:
    """'#00B050' / '00B050' / 'rgb(0, 176, 80)' → (r, g, b); else None."""
    s = (raw or "").strip().lower()
    if not s:
        return None
    if s.startswith("rgb"):
        nums = re.findall(r"\d{1,3}", s)
        if len(nums) >= 3:
            r, g, b = (int(n) for n in nums[:3])
            if all(0 <= v <= 255 for v in (r, g, b)):
                return (r, g, b)
        return None
    hex_part = s.lstrip("#").strip()
    if re.fullmatch(r"[0-9a-f]{6}", hex_part):
        return (int(hex_part[0:2], 16), int(hex_part[2:4], 16), int(hex_part[4:6], 16))
    if re.fullmatch(r"[0-9a-f]{3}", hex_part):
        return tuple(int(c * 2, 16) for c in hex_part)  # type: ignore[return-value]
    return None


def classify_cell_color(raw: Optional[str]) -> str:
    """Map a CSS color string to a DETAILS_STATUS_* key (hue-bucket fallback).

    Hue buckets (HSV, saturation-gated so white/grey cells stay 'unknown'):
      green ~70-170° Present · red ≤14°/≥346° Absent · pink/magenta 300-345°
      Absent-Late · orange/yellow 15-70° Present-Late · blue 170-300° Leave.
    """
    if not raw:
        return DETAILS_STATUS_UNKNOWN
    rgb = _parse_color(raw)
    if rgb is None:
        return DETAILS_STATUS_UNKNOWN

    exact = _EXACT_HEX_STATUS.get("%02x%02x%02x" % rgb)
    if exact:
        return exact

    r, g, b = (v / 255.0 for v in rgb)
    h, s, v = colorsys.rgb_to_hsv(r, g, b)
    hue = h * 360.0
    if s < 0.15:          # white / grey / near-invisible shading
        return DETAILS_STATUS_UNKNOWN
    if 70.0 <= hue < 170.0:
        return DETAILS_STATUS_PRESENT
    if hue <= 14.0 or hue >= 346.0:
        return DETAILS_STATUS_ABSENT
    if 300.0 <= hue < 346.0:
        return DETAILS_STATUS_ABSENT_LATE
    if 15.0 <= hue < 70.0:
        return DETAILS_STATUS_PRESENT_LATE
    if 170.0 <= hue < 300.0:
        return DETAILS_STATUS_LEAVE
    return DETAILS_STATUS_UNKNOWN


def _cell_status(cell) -> str:
    """Classify one matrix <td> by its inline style / bgcolor attribute."""
    style = cell.get("style") or ""
    m = _BG_COLOR_RE.search(style)
    if m:
        status = classify_cell_color(m.group(1))
        if status != DETAILS_STATUS_UNKNOWN:
            return status
    bgcolor = cell.get("bgcolor")
    if bgcolor:
        status = classify_cell_color(bgcolor)
        if status != DETAILS_STATUS_UNKNOWN:
            return status
    return DETAILS_STATUS_UNKNOWN


# ── Header label extraction ─────────────────────────────────────────────────

def _label_value(soup: BeautifulSoup, label_prefixes: tuple[str, ...]) -> str:
    """Find 'Label : Value' text pairs anywhere on the page (inline or 3-cell table)."""
    def _clean(value: str) -> str:
        return re.sub(r"\s+", " ", value).strip().lstrip(":").strip()

    for el in soup.find_all(string=True):
        text = re.sub(r"\s+", " ", str(el)).strip()
        if not text:
            continue
        low = text.lower()
        for prefix in label_prefixes:
            if low.startswith(prefix):
                # 1. Inline "Label : Value"
                _, sep, value = text.partition(":")
                if sep and value.strip():
                    return _clean(value)
                # 2. Table cells: <td>Label</td> [<td>:</td>] <td>Value</td>
                parent_el = el.find_parent(["td", "span", "label", "b"])
                if parent_el is not None:
                    td = parent_el if parent_el.name == "td" else parent_el.find_parent("td")
                    if td is not None:
                        for sib in td.find_next_siblings("td"):
                            sib_text = sib.get_text(" ", strip=True)
                            if sib_text == ":":
                                continue
                            if sib_text:
                                return _clean(sib_text)
    return ""


# ── Matrix table selection ──────────────────────────────────────────────────

def _find_matrix_table(soup: BeautifulSoup):
    """The matrix table is the table with the MOST month-labeled rows."""
    best, best_score = None, 0
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        score = 0
        has_class_no = False
        for tr in rows:
            label_cells = tr.find_all(["td", "th"])
            first = label_cells[0].get_text(" ", strip=True) if label_cells else ""
            if _MONTH_ROW_RE.match(first):
                score += 1
            if re.search(r"Class\s*No", tr.get_text(" ", strip=True), re.IGNORECASE):
                has_class_no = True
        if score > 0 and has_class_no and score > best_score:
            best, best_score = table, score
    return best


def _parse_totals_row(row_text: str) -> dict[str, Optional[int]]:
    """Parse the matrix totals row into typed dictionary."""
    def _int(rx: re.Pattern) -> Optional[int]:
        m = rx.search(row_text)
        return int(m.group(1)) if m else None

    return {
        "total": _int(_TOTALS_TOTAL_RE),
        "present": _int(_TOTALS_PRESENT_RE),
        "absent": _int(_TOTALS_ABSENT_RE),
        "leave": _int(_TOTALS_LEAVE_RE),
        "overall_absence": _int(_TOTALS_OVERALL_RE),
    }


# ── Main entry ──────────────────────────────────────────────────────────────

def parse_attendance_details_html(html: str) -> SubjectAttendanceDetails:
    """Parse the details page HTML into a SubjectAttendanceDetails."""
    if not html or not html.strip():
        raise AttendanceParseError("Attendance details page returned empty HTML.")

    soup = BeautifulSoup(html, HTML_PARSER)

    table = _find_matrix_table(soup)
    if table is None:
        raise AttendanceParseError(
            "Attendance details matrix (Class No / Month grid) not found in HTML."
        )

    months: list[DetailsMonth] = []
    totals: dict[str, Optional[int]] = {}
    totals_seen = False

    for tr in table.find_all("tr"):
        cells = tr.find_all("td")
        if not cells:
            cells = tr.find_all(["td", "th"])
        first_text = cells[0].get_text(" ", strip=True) if cells else ""
        row_text = tr.get_text(" ", strip=True)

        if _TOTALS_TOTAL_RE.search(first_text) and not _MONTH_ROW_RE.match(first_text):
            totals = _parse_totals_row(row_text)
            totals_seen = True
            continue

        m = _MONTH_ROW_RE.match(first_text)
        if not m:
            continue

        name = m.group(1).capitalize()
        count = int(m.group(2))
        submission = (m.group(3) or "").capitalize()

        days: list[DetailsDay] = []
        for cell in cells[1:]:
            txt = cell.get_text(strip=True)
            if not txt or not txt.isdigit():
                continue
            days.append(DetailsDay(
                class_no=len(days) + 1,
                day=int(txt),
                status=_cell_status(cell),
            ))

        months.append(DetailsMonth(
            name=name, count=count, submission=submission, days=days,
        ))

    if not months and not totals_seen:
        raise AttendanceParseError(
            "Attendance details matrix found but no month rows could be parsed."
        )

    details = SubjectAttendanceDetails(
        student_info=_label_value(soup, ("student name",)),
        session_label=_label_value(soup, ("academic year",)),
        subject_label=_label_value(soup, ("subject",)),
        months=months,
        totals=totals,
    )

    details.totals = _reconcile_totals(details)
    return details


def _reconcile_totals(details: SubjectAttendanceDetails) -> dict[str, int]:
    """Merge portal totals with matrix-derived counts."""
    days = [d for m in details.months for d in m.days]
    derived = {
        "total": len(days),
        "present": sum(1 for d in days if d.status in (DETAILS_STATUS_PRESENT, DETAILS_STATUS_PRESENT_LATE)),
        "absent": sum(1 for d in days if d.status in (DETAILS_STATUS_ABSENT, DETAILS_STATUS_ABSENT_LATE)),
        "leave": sum(1 for d in days if d.status == DETAILS_STATUS_LEAVE),
    }
    derived["overall_absence"] = derived["absent"] + derived["leave"]

    merged = dict(derived)
    for key, value in (details.totals or {}).items():
        if value is not None:
            merged[key] = int(value)
    return merged
