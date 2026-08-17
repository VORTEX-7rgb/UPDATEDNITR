import os
import sys
import asyncio
import logging
from sqlalchemy import select
from bs4 import BeautifulSoup

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

async def run_live_test():
    logger.info("Connecting to DB to get credentials...")
    async with get_db_session() as session:
        stmt = select(User).where(User.roll_number == "725MN1011")
        res = await session.execute(stmt)
        user = res.scalars().first()
        if not user:
            logger.error("Active user 725MN1011 not found in DB! Falling back to first user...")
            stmt = select(User)
            res = await session.execute(stmt)
            user = res.scalars().first()
            if not user:
                logger.error("No user found in DB!")
                return
        
        roll = user.roll_number
        password = decrypt_password(user.encrypted_password)

    logger.info("Logged in user: %s", roll)
    
    # Run Method A
    logger.info("\n=== TESTING METHOD A: __EVENTTARGET = btnSearch ===")
    client_a = NitrisClient()
    try:
        await client_a.login(roll, password)
        url = await client_a.fetch_question_papers_page_url()
        
        # Step 0 & 1: Initialize context and get initial page
        import urllib.parse
        parsed_url = urllib.parse.urlparse(str(url))
        default_raw_path = f"/nitris/Student/Default.aspx?{parsed_url.query}".encode("ascii")
        default_url = httpx_URL = client_a.base_url + f"/nitris/Student/Default.aspx?{parsed_url.query}"
        await client_a.client.get(default_url)
        resp = await client_a.client.get(url)
        html = resp.text
        
        # Select Year
        form_state = extract_form_fields(html)
        year_options = extract_dropdown_options(html, CTL_QP_ACADEMIC_YEAR)
        selected_year_value = client_a._pick_option(year_options, "2025-26 /Spring", fallback_idx=0)
        html = await submit_postback(client_a.client, url, form_state, CTL_QP_ACADEMIC_YEAR, {CTL_QP_ACADEMIC_YEAR: selected_year_value})
        
        # Search
        form_state = extract_form_fields(html)
        form_updates = {
            CTL_QP_ACADEMIC_YEAR: selected_year_value,
            CTL_QP_SUBJECT_SEARCH: "MA1004",
        }
        
        # POST with __EVENTTARGET = btnSearch
        resp_a = await client_a.client.post(
            url,
            data={
                **form_state,
                "__EVENTTARGET": CTL_QP_SEARCH_BTN,
                "__EVENTARGUMENT": "",
                **form_updates
            },
            headers={"Referer": str(url)}
        )
        logger.info("Method A Response Size: %d bytes", len(resp_a.content))
        soup_a = BeautifulSoup(resp_a.text, "html.parser")
        table_a = soup_a.find(id=QUESTION_TABLE_ID)
        rows_a = table_a.find_all("tr") if table_a else []
        logger.info("Method A Table Rows: %d", len(rows_a))
    except Exception as e:
        logger.error("Method A failed: %r", e)
    finally:
        await client_a.close()

    # Run Method B
    logger.info("\n=== TESTING METHOD B: __EVENTTARGET = '' and btnSearch = 'Search' ===")
    client_b = NitrisClient()
    try:
        await client_b.login(roll, password)
        url = await client_b.fetch_question_papers_page_url()
        
        # Step 0 & 1: Initialize context and get initial page
        import urllib.parse
        parsed_url = urllib.parse.urlparse(str(url))
        default_url = client_b.base_url + f"/nitris/Student/Default.aspx?{parsed_url.query}"
        await client_b.client.get(default_url)
        resp = await client_b.client.get(url)
        html = resp.text
        
        # Select Year
        form_state = extract_form_fields(html)
        year_options = extract_dropdown_options(html, CTL_QP_ACADEMIC_YEAR)
        selected_year_value = client_b._pick_option(year_options, "2025-26 /Spring", fallback_idx=0)
        html = await submit_postback(client_b.client, url, form_state, CTL_QP_ACADEMIC_YEAR, {CTL_QP_ACADEMIC_YEAR: selected_year_value})
        
        # Search
        form_state = extract_form_fields(html)
        form_updates = {
            CTL_QP_ACADEMIC_YEAR: selected_year_value,
            CTL_QP_SUBJECT_SEARCH: "MA1004",
            CTL_QP_SEARCH_BTN: "Search"  # Click the button
        }
        
        # POST with __EVENTTARGET = ""
        resp_b = await client_b.client.post(
            url,
            data={
                **form_state,
                "__EVENTTARGET": "",
                "__EVENTARGUMENT": "",
                **form_updates
            },
            headers={"Referer": str(url)}
        )
        logger.info("Method B Response Size: %d bytes", len(resp_b.content))
        soup_b = BeautifulSoup(resp_b.text, "html.parser")
        table_b = soup_b.find(id=QUESTION_TABLE_ID)
        rows_b = table_b.find_all("tr") if table_b else []
        logger.info("Method B Table Rows: %d", len(rows_b))
    except Exception as e:
        logger.error("Method B failed: %r", e)
    finally:
        await client_b.close()

if __name__ == "__main__":
    asyncio.run(run_live_test())
