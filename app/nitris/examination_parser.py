"""Parse the NITRIS previous year question papers HTML page into structured data."""

import logging
import re
from dataclasses import dataclass
from typing import Optional
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

@dataclass
class QuestionPaperRecord:
    subject_code: str
    subject_name: str
    ltp: str
    credit: str
    mid_sem_target: Optional[str] = None
    end_sem_target: Optional[str] = None


def parse_question_papers_html(html: str) -> list[QuestionPaperRecord]:
    """Parse previous year question papers page HTML and extract subject lists with download triggers.
    
    Supports Chrome view-source pages automatically.
    """
    from app.nitris.constants import QUESTION_TABLE_ID
    from app.nitris.exceptions import AttendanceParseError
    
    soup = BeautifulSoup(html, "html.parser")
    
    # 1. Handle Chrome view-source automatically
    td_lines = soup.find_all('td', class_='line-content')
    if td_lines:
        logger.info("Chrome view-source presentation wrapper detected. Reconstructing raw HTML...")
        reconstructed = "\n".join(td.get_text() for td in td_lines)
        soup = BeautifulSoup(reconstructed, "html.parser")
        
    # 2. Locate the GridView table
    # Standard GridView ID: ContentPlaceHolder2_ContentPlaceHolder1_mainContent_gvSubjects
    table = soup.find(id=QUESTION_TABLE_ID)
    if not table:
        # Fallback: try searching for any table whose ID contains gvSubjects or gvQuestions
        for t in soup.find_all("table"):
            t_id = t.get("id", "")
            if t_id and ("gvSubjects" in t_id or "gvQuestions" in t_id):
                table = t
                break
                
    if not table:
        raise AttendanceParseError("Question Papers GridView table not found in HTML.")
        
    rows = table.find_all("tr", recursive=False)
    if not rows:
        tbody = table.find("tbody")
        if tbody:
            rows = tbody.find_all("tr", recursive=False)
            
    if not rows:
        raise AttendanceParseError("Question Papers table has no rows.")
        
    logger.info("Parsing question papers grid: found %d rows in total", len(rows))
    
    records: list[QuestionPaperRecord] = []
    skipped = 0
    
    for idx, row in enumerate(rows):
        cells = row.find_all("td", recursive=False)
        
        # GridView rows must have at least 7 cells (#, code, name, LTP, credit, mid, end)
        if len(cells) < 7:
            skipped += 1
            continue
            
        idx_str = cells[0].get_text(strip=True)
        # Verify first column is a numeric serial to filter header/footer templates
        if not idx_str.isdigit():
            skipped += 1
            continue
            
        subject_code = cells[1].get_text(strip=True)
        subject_name = cells[2].get_text(strip=True)
        ltp = cells[3].get_text(strip=True)
        credit = cells[4].get_text(strip=True)
        
        # 3. Parse Mid Sem postback target safely
        mid_sem_target = None
        mid_a = cells[5].find("a")
        if mid_a:
            href = mid_a.get("href", "")
            match = re.search(r"__doPostBack\('([^']*)'", href)
            if match:
                mid_sem_target = match.group(1)
                
        # 4. Parse End Sem postback target safely
        end_sem_target = None
        end_a = cells[6].find("a")
        if end_a:
            href = end_a.get("href", "")
            match = re.search(r"__doPostBack\('([^']*)'", href)
            if match:
                end_sem_target = match.group(1)
                
        record = QuestionPaperRecord(
            subject_code=subject_code,
            subject_name=subject_name,
            ltp=ltp,
            credit=credit,
            mid_sem_target=mid_sem_target,
            end_sem_target=end_sem_target
        )
        records.append(record)
        
    logger.info(
        "Question Papers parsing complete - Total Rows: %d, Valid Records: %d, Skipped: %d",
        len(rows), len(records), skipped
    )
    
    return records
