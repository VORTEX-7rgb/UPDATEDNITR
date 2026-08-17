import os
import sys
import asyncio
import logging
from sqlalchemy import select
from bs4 import BeautifulSoup
import httpx

# Add app directory to sys.path to resolve imports correctly
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.db.database import get_db_session
from app.db.models import User
from app.db.crypto import decrypt_password
from app.nitris.client import NitrisClient
from app.nitris.aspnet import extract_form_fields, extract_dropdown_options, submit_postback
from app.nitris.constants import CTL_QP_ACADEMIC_YEAR, CTL_QP_DEPARTMENT, CTL_QP_SUBJECT_SEARCH, CTL_QP_SEARCH_BTN, QUESTION_TABLE_ID

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

async def test_download():
    async with get_db_session() as session:
        stmt = select(User).where(User.roll_number == "725MN1011")
        res = await session.execute(stmt)
        user = res.scalars().first()
        roll = user.roll_number
        password = decrypt_password(user.encrypted_password)

    client = NitrisClient()
    try:
        await client.login(roll, password)
        url = await client.fetch_question_papers_page_url()
        
        # Step 0: Default.aspx
        import urllib.parse
        parsed_url = urllib.parse.urlparse(str(url))
        default_url = client.base_url + f"/nitris/Student/Default.aspx?{parsed_url.query}"
        await client.client.get(default_url)
        
        # Step 1: GET page
        resp = await client.client.get(url)
        html = resp.text
        
        # Step 2: Select Year
        form_state = extract_form_fields(html)
        year_options = extract_dropdown_options(html, CTL_QP_ACADEMIC_YEAR)
        selected_year_value = client._pick_option(year_options, "2025-26 /Spring", fallback_idx=0)
        html = await submit_postback(client.client, url, form_state, CTL_QP_ACADEMIC_YEAR, {CTL_QP_ACADEMIC_YEAR: selected_year_value})
        
        # Step 3: Search
        form_state = extract_form_fields(html)
        form_updates = {
            CTL_QP_ACADEMIC_YEAR: selected_year_value,
            CTL_QP_SUBJECT_SEARCH: "MA1004",
        }
        
        # We know Method A is correct for search postback
        html = await submit_postback(
            client.client, url, form_state, CTL_QP_SEARCH_BTN,
            form_updates, "qp_step3_search"
        )
        
        logger.info("Search response HTML size: %d", len(html))
        
        # Parse postback targets from search result
        soup = BeautifulSoup(html, "html.parser")
        table = soup.find(id=QUESTION_TABLE_ID)
        if not table:
            logger.error("Could not find table in search response!")
            return
            
        rows = table.find_all("tr")
        logger.info("GridView Rows: %d", len(rows))
        
        mid_sem_target = None
        for r in rows:
            cells = r.find_all("td")
            if len(cells) >= 7 and cells[1].get_text(strip=True) == "MA1004":
                import re
                mid_a = cells[5].find("a")
                if mid_a:
                    href = mid_a.get("href", "")
                    match = re.search(r"__doPostBack\('([^']*)'", href)
                    if match:
                        mid_sem_target = match.group(1)
                        break
                        
        if not mid_sem_target:
            logger.error("Could not locate mid sem print target in search results!")
            return
            
        logger.info("Located PDF target: %s", mid_sem_target)
        
        # Step 4: Download PDF
        form_state = extract_form_fields(html)
        payload = {
            **form_state,
            "__EVENTTARGET": mid_sem_target,
            "__EVENTARGUMENT": "",
        }
        payload.pop(CTL_QP_SEARCH_BTN, None) # Exclude search button!
        
        # Let's print payload keys & values for download
        logger.info("\n=== DOWNLOAD PAYLOAD KEY-VALUES ===")
        for k, v in payload.items():
            if k not in ("__VIEWSTATE", "__VIEWSTATEGENERATOR", "__EVENTVALIDATION"):
                logger.info("  %s: %r", k, v)
                
        headers = {"Referer": str(url)}
        resp_download = await client.client.post(url, data=payload, headers=headers)
        
        logger.info("\n=== DOWNLOAD RESPONSE ANALYSIS ===")
        logger.info("Status Code: %d", resp_download.status_code)
        logger.info("Response Size: %d bytes", len(resp_download.content))
        for h_k, h_v in resp_download.headers.items():
            logger.info("  %s: %s", h_k, h_v)
            
        if "text/html" in resp_download.headers.get("Content-Type", ""):
            # It returned HTML! Let's check why by looking for error messages
            d_soup = BeautifulSoup(resp_download.text, "html.parser")
            err_lbl = d_soup.find("span", {"id": "lblMessage"}) # check if there is an error label
            if err_lbl:
                logger.error("Error Label found in response: %s", err_lbl.get_text())
            else:
                logger.error("No error label. Response contains GridView? %s", QUESTION_TABLE_ID in resp_download.text)

    finally:
        await client.close()

if __name__ == "__main__":
    asyncio.run(test_download())
