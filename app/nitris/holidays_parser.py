"""Parse the ASP.NET Calendar on Home.aspx into structured holiday data.

Reverse-engineered 2026-09-05 by walking the live NITRIS dashboard DOM:

  <table id="ContentPlaceHolder3_cal1" title="Calendar">
    <tr><td colspan="7">
      <table>  <!-- month-header row -->
        <tr>
          <td><a href="javascript:__doPostBack('ctl00$ContentPlaceHolder3$cal1','V9709')"
                 title="Go to the previous month">&lt;</a></td>
          <td align="center">September 2026</td>
          <td><a href="javascript:__doPostBack('ctl00$ContentPlaceHolder3$cal1','V9770')"
                 title="Go to the next month">&gt;</a></td>
        </tr>
      </table>
    </td></tr>
    <tr><th>Mon</th>...<th>Sun</th></tr>
    <tr>
      <td>31</td>                                  <!-- trailing day, prev month -->
      <td>1</td>                                   <!-- regular weekday -->
      <td title="Janmashtami" style="background-color:#D9534F;...">4</td>  <!-- HOLIDAY -->
      <td style="color:#FF0000;...">5</td>          <!-- weekend (red text, no title) -->
    </tr>
    ...
  </table>

KEY INSIGHT: Only holiday cells carry a `title` attribute. Weekend cells use
red TEXT color but no title. Trailing/leading days from adjacent months ALSO
carry titles if they are holidays (e.g. Sep calendar shows "2 - Mahatma
Gandhis Birthday" which is actually Oct 2). We therefore cannot rely on the
day number alone to know which month a holiday belongs to — we attach every
parsed holiday to the CURRENTLY DISPLAYED month header so callers always know
the calendar's logical month.

This module is pure (no I/O) — it takes HTML in, returns dataclasses out.
The HTTP + postback mechanics live in app.nitris.client.NitrisClient.
"""
from __future__ import annotations

import calendar as _cal
import logging
import re
from dataclasses import dataclass
from typing import Optional

from bs4 import BeautifulSoup, Tag

from app.nitris.constants import (
    HTML_PARSER,
    HOLIDAYS_CALENDAR_ID,
    HOLIDAYS_CALENDAR_EVENT_TARGET,
    HOLIDAYS_PREV_LINK_TITLE,
    HOLIDAYS_NEXT_LINK_TITLE,
)
from app.nitris.exceptions import HolidaysParseError

logger = logging.getLogger(__name__)

# __doPostBack('ctl00$ContentPlaceHolder3$cal1','V9709')  ->  ("ctl00$ContentPlaceHolder3$cal1", "V9709")
_DOPOSTBACK_RE = re.compile(
    r"__doPostBack\s*\(\s*'([^']+)'\s*,\s*'([^']*)'\s*\)"
)

# Month-header text like "September 2026" or "September, 2026" — tolerant parse.
_MONTH_HEADER_RE = re.compile(
    r"^\s*([A-Za-z]+)\s*,?\s+(\d{4})\s*$"
)

_MONTH_NAMES = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
}


@dataclass(frozen=True)
class HolidayEntry:
    """One parsed holiday cell.

    `day` is the day-of-month as shown on the calendar cell. Note that ASP.NET
    Calendar grids display trailing days from the previous/next month, so a
    holiday appearing on the September grid with day=2 may actually be
    October 2 (Gandhi Jayanti). NITRIS renders trailing holidays identically
    to in-month holidays — there is no reliable HTML signal to distinguish
    them from a single month's HTML alone.

    For the bot's UX (showing the month's holidays), we attach every parsed
    holiday to the displayed month header — that matches what the student
    sees on the portal. The `is_trailing` heuristic flags cells whose
    day number is from adjacent months (e.g. Aug 31 or Oct 2 on the September grid).
    This is acceptable: the student sees those holidays on the grid and the bot
    shows them too.
    """
    day: int
    name: str
    month: int        # 1-12, from the calendar's displayed month header
    year: int         # full year, from the calendar's displayed month header
    is_trailing: bool = False   # True if day is from previous/next adjacent month


@dataclass
class HolidaysPage:
    """The full parsed calendar state for one Home.aspx render.

    Captures everything the bot needs to:
      - render the current month's holidays,
      - navigate to prev/next month via postback (arguments harvested from the
        rendered <a> hrefs — guarantees correctness even if NITRIS revises
        the calendar's internal date-offset encoding).
    """
    month: int                    # displayed month (1-12)
    year: int                     # displayed year
    month_label: str              # "September 2026" — human label as shown on portal
    holidays: list[HolidayEntry]  # holidays visible on THIS calendar grid
    prev_event_argument: Optional[str]   # "V9709" for prev-month postback, None if unavailable
    next_event_argument: Optional[str]   # "V9770" for next-month postback, None if unavailable
    event_target: str = HOLIDAYS_CALENDAR_EVENT_TARGET
    raw_html: str = ""            # full Home.aspx HTML (for follow-up postbacks via extract_form_fields)


def _find_calendar(soup: BeautifulSoup) -> Tag:
    """Locate the ASP.NET Calendar table by its stable ID."""
    cal = soup.find("table", {"id": HOLIDAYS_CALENDAR_ID})
    if cal is None:
        raise HolidaysParseError(
            f"Calendar table #{HOLIDAYS_CALENDAR_ID} not found in Home.aspx — "
            f"portal markup may have changed."
        )
    return cal


def _find_month_header_cell(cal: Tag) -> Tag:
    """Find the colspan='7' cell that wraps the month-header inner table.

    Structure:
      <td colspan="7">
        <table>
          <tr>
            <td><a title="Go to the previous month">&lt;</a></td>
            <td align="center">September 2026</td>
            <td><a title="Go to the next month">&gt;</a></td>
          </tr>
        </table>
      </td>
    """
    header_cell = cal.find("td", {"colspan": "7"})
    if header_cell is None:
        raise HolidaysParseError(
            "Calendar header row (td colspan=7) not found — markup changed."
        )
    return header_cell


def _parse_month_label(header_cell: Tag) -> tuple[int, int, str]:
    """Extract (month, year, raw_label) from the header cell.

    The centered <td align="center"> inside the header carries the visible
    "September 2026" text. We match case-insensitively against the canonical
    English month names.
    """
    # The centered inner cell is the one without an <a> (the prev/next cells contain anchors).
    for inner_td in header_cell.find_all("td"):
        if inner_td.find("a") is not None:
            continue
        text = inner_td.get_text(strip=True)
        if not text:
            continue
        m = _MONTH_HEADER_RE.match(text)
        if not m:
            continue
        month_name, year_str = m.group(1), m.group(2)
        month_num = _MONTH_NAMES.get(month_name.lower())
        if month_num is None:
            continue
        return month_num, int(year_str), text

    raise HolidaysParseError(
        "Could not parse month/year from calendar header cell."
    )


def _harvest_postback_arg(header_cell: Tag, link_title: str) -> Optional[str]:
    """Extract the __EVENTARGUMENT ('V9709') from a prev/next month anchor.

    The anchor's href looks like:
        javascript:__doPostBack('ctl00$ContentPlaceHolder3$cal1','V9709')

    We match by the anchor's `title` attribute ("Go to the previous month" /
    "Go to the next month") to be resilient to NITRIS flipping the icon glyph
    or reordering the cells.
    """
    for a in header_cell.find_all("a"):
        if a.get("title", "").strip().lower() != link_title.lower():
            continue
        href = a.get("href", "")
        m = _DOPOSTBACK_RE.search(href)
        if m:
            return m.group(2)
    return None


def _parse_holiday_cells(cal: Tag, month: int, year: int) -> list[HolidayEntry]:
    """Walk every <td> inside the calendar and collect holiday cells.

    A holiday cell is identified by the presence of a non-empty `title`
    attribute. This is the canonical marker — only holidays carry it.
    Weekend cells use red TEXT color but have no title, so they are correctly
    excluded.

    Trailing/leading day detection:
    ASP.NET Calendar paints days from the previous and next months in the grid.
    We detect leading days (before day 1 is seen, with day > 15) and trailing
    days (after mid-month is seen, with day < 15) and flag them with is_trailing=True.
    """
    holidays: list[HolidayEntry] = []
    days_in_month = _cal.monthrange(year, month)[1]  # (weekday, days_in_month)

    saw_day_1 = False
    saw_mid_month = False

    for td in cal.find_all("td"):
        day_text = td.get_text(strip=True)
        if not day_text or not day_text.isdigit():
            continue
        day = int(day_text)

        # Detect transition into the current month
        if day == 1:
            saw_day_1 = True
        # Only mark mid-month reached AFTER day 1 has been seen (avoids prev-month days like 31 triggering it)
        if saw_day_1 and day >= 15:
            saw_mid_month = True

        title = td.get("title", "").strip()
        if not title:
            continue

        is_trailing = False
        if not saw_day_1 and day > 15:
            is_trailing = True
        elif saw_mid_month and day < 15:
            is_trailing = True
        elif day < 1 or day > days_in_month:
            is_trailing = True

        holidays.append(
            HolidayEntry(
                day=day,
                name=title,
                month=month,
                year=year,
                is_trailing=is_trailing,
            )
        )

    # Sort holidays: in-month by day, trailing days at their relative positions
    def _sort_key(h: HolidayEntry) -> tuple[int, int]:
        if h.is_trailing:
            if h.day > 15:
                return (0, h.day)  # Leading days from prev month
            return (2, h.day)      # Trailing days into next month
        return (1, h.day)          # In-month days

    holidays.sort(key=_sort_key)
    return holidays


def parse_holidays_html(html: str) -> HolidaysPage:
    """Parse a Home.aspx response into a HolidaysPage.

    Args:
        html: The full HTML of /nitris/Student/Home/Home.aspx (current month)
              or a postback response (after a prev/next month click).

    Returns:
        HolidaysPage with month, year, holidays, and prev/next postback args.

    Raises:
        HolidaysParseError: If the calendar is missing or its header is
            unparseable.
    """
    soup = BeautifulSoup(html, HTML_PARSER)
    cal = _find_calendar(soup)
    header_cell = _find_month_header_cell(cal)
    month, year, month_label = _parse_month_label(header_cell)

    prev_arg = _harvest_postback_arg(header_cell, HOLIDAYS_PREV_LINK_TITLE)
    next_arg = _harvest_postback_arg(header_cell, HOLIDAYS_NEXT_LINK_TITLE)

    holidays = _parse_holiday_cells(cal, month, year)

    logger.info(
        "Parsed holidays for %s: %d holiday(s), prev_arg=%s, next_arg=%s",
        month_label,
        len(holidays),
        prev_arg,
        next_arg,
    )

    return HolidaysPage(
        month=month,
        year=year,
        month_label=month_label,
        holidays=holidays,
        prev_event_argument=prev_arg,
        next_event_argument=next_arg,
        raw_html=html,
    )


__all__ = [
    "HolidayEntry",
    "HolidaysPage",
    "parse_holidays_html",
]
