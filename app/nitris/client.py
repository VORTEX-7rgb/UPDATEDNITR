"""NITRIS client — login + ASP.NET postback attendance workflow."""

import logging
import os

import httpx

from app.config import config
from app.nitris.constants import (
    DEFAULT_HEADERS, AJAX_HEADERS,
    LOGIN_PAGE_URL, GET_PASSWORD_ENDPOINT, LOGIN_USER_ENDPOINT,
    ATTENDANCE_PAGE_PATH, ATTENDANCE_RAW_QUERY,
    CTL_SEMESTER, CTL_ACADEMIC_YEAR, CTL_SESSION,
    ATTENDANCE_TABLE_ID,
)
from app.nitris.exceptions import LoginError, SessionExpiredError, AttendanceWorkflowError, AttendanceTableMissingError
from app.nitris.aspnet import extract_form_fields, extract_dropdown_options, submit_postback

logger = logging.getLogger(__name__)


class NitrisClient:
    """Persistent session client for NITRIS portal.

    ONE instance per user request. Cookies and ASP.NET state survive across calls.
    """

    def __init__(self) -> None:
        self.base_url = config.NITRIS_BASE_URL
        self._debug = os.getenv("DEBUG_ATTENDANCE", "").lower() in ("1", "true")
        limits = httpx.Limits(max_keepalive_connections=5, max_connections=10)
        self.client = httpx.AsyncClient(
            base_url=self.base_url,
            headers=DEFAULT_HEADERS,
            timeout=30.0,
            follow_redirects=True,
            limits=limits,
        )

    # ── Login Flow ──────────────────────────────────────────────

    async def login(self, username: str, password: str) -> None:
        """Authenticate: init session → transform password → login → visit home.
        
        Retries up to 3 times with exponential backoff on transient network/IIS errors.
        """
        import asyncio
        max_attempts = 3
        backoff = 1.0
        
        for attempt in range(1, max_attempts + 1):
            try:
                # Step 0: Seed ASP.NET_SessionId
                try:
                    resp = await self.client.get(LOGIN_PAGE_URL)
                    resp.raise_for_status()
                    logger.info("Session initialized via Login.aspx")
                except Exception as e:
                    raise LoginError("Could not initialize NITRIS session.") from e

                # Step 1: Server-side password transformation
                try:
                    resp = await self.client.post(GET_PASSWORD_ENDPOINT, json={"password": password}, headers=AJAX_HEADERS)
                    resp.raise_for_status()
                    transformed = resp.json().get("d", "")
                    if not transformed:
                        raise LoginError("Server returned empty transformed password.")
                    logger.info("Password transformed OK")
                except LoginError:
                    raise
                except Exception as e:
                    raise LoginError("Password transformation failed.") from e

                # Step 2: Authenticate
                try:
                    resp = await self.client.post(
                        LOGIN_USER_ENDPOINT,
                        json={"username": username, "logpassword": transformed},
                        headers=AJAX_HEADERS,
                    )
                    resp.raise_for_status()
                    result = resp.json().get("d", "")
                except Exception as e:
                    raise LoginError("Login request failed.") from e

                if not result or "SUCCESS" not in result:
                    raise LoginError(f"Invalid credentials. Server: {result}")

                # Step 3: Visit home page to finalize session
                parts = result.split(":", 1)
                if len(parts) > 1 and parts[1].strip():
                    resp = await self.client.get(parts[1].strip())
                    resp.raise_for_status()
                    logger.info("Session finalized via home page")

                logger.info("Login successful for %s", username)
                return
                
            except LoginError:
                # Hard authentication/invalid credentials fail fast and bypass retries
                raise
            except Exception as e:
                # Catch transient network/HTTP/IIS errors
                logger.warning(
                    "Login attempt %d/%d failed for %s: %s", 
                    attempt, max_attempts, username, e
                )
                if attempt == max_attempts:
                    raise LoginError(f"Login failed after {max_attempts} attempts.") from e
                await asyncio.sleep(backoff)
                backoff *= 2.0  # 1s, 2s, 4s backoff

    # ── Attendance Workflow ─────────────────────────────────────

    async def fetch_attendance(self, semester: str = "Spring") -> str:
        """Execute the full ASP.NET postback workflow to get the attendance table HTML.

        Returns the final HTML page containing the attendance table.
        """
        url = self._build_attendance_url()
        url_str = str(url)

        # Step 1: GET initial page
        logger.info("[step1] GET attendance page")
        headers = {"Referer": f"{self.base_url}/nitris/Student/Home/Home.aspx"}
        # Disable auto-redirect to catch auth drops explicitly
        resp = await self.client.get(url, headers=headers, follow_redirects=False)
        
        if resp.status_code in (301, 302, 303, 307, 308):
            loc = resp.headers.get("Location", "")
            if "Login.aspx" in loc:
                raise SessionExpiredError("Session expired — redirected to login.")
            raise AttendanceWorkflowError(f"Initial GET redirected to {loc}")
            
        if resp.status_code != 200:
            raise AttendanceWorkflowError(f"Initial GET returned {resp.status_code}")
            
        html = resp.text
        logger.info("[step1] Got %d bytes", len(html))
        
        if "txtusername" in html.lower() or "Login.aspx" in html:
            raise SessionExpiredError("Session expired — login form detected.")

        if self._debug:
            from app.nitris.aspnet import _save_debug_snapshot
            _save_debug_snapshot(
                html, 
                {"step": "step1_initial", "status": 200, "url": url_str, "response_size": len(html), "viewstate_present": "__VIEWSTATE" in html, "table_found": ATTENDANCE_TABLE_ID in html},
                "step1_initial"
            )

        # Step 2: POST semester selection
        form_state = extract_form_fields(html)
        sem_options = extract_dropdown_options(html, CTL_SEMESTER)
        sem_value = self._pick_option(sem_options, semester, fallback_idx=-1)
        logger.info("[step2] Selecting semester: %s", sem_value)

        html = await submit_postback(
            self.client, url, form_state, CTL_SEMESTER,
            {CTL_SEMESTER: sem_value}, "step2_semester", self._debug,
        )

        # Step 3: Iterate through academic years
        form_state = extract_form_fields(html)
        year_options = extract_dropdown_options(html, CTL_ACADEMIC_YEAR)
        if not year_options:
            raise AttendanceWorkflowError("Academic year dropdown not populated after step 2.")
            
        valid_years = self._get_sorted_academic_years(year_options)
        if not valid_years:
             raise AttendanceWorkflowError(f"No valid academic years found. Options: {year_options}")

        logger.info("[step3] All year options: %s", year_options)
        
        from app.nitris.exceptions import InvalidContextError
        from app.nitris.aspnet import has_session_dropdown, _save_debug_snapshot
        
        valid_year_html = None
        for year_value, year_text in valid_years:
            logger.info("[step3] Probing year: %s (%s)", year_value, year_text)
            try:
                temp_html = await submit_postback(
                    self.client, url, form_state, CTL_ACADEMIC_YEAR,
                    {CTL_ACADEMIC_YEAR: year_value}, f"step3_year_{year_value}", self._debug,
                )
                
                if not has_session_dropdown(temp_html):
                    logger.warning("[step3] Year %s response missing session dropdown. Skipping.", year_text)
                    continue
                    
                valid_year_html = temp_html
                logger.info("[step3] Successfully selected year: %s", year_text)
                break
                
            except InvalidContextError as e:
                logger.warning("[step3] Year %s invalid context (503 redirect). Skipping.", year_text)
                continue
                
        if not valid_year_html:
            raise AttendanceWorkflowError("All academic year fallbacks failed.")
            
        html = valid_year_html

        # Step 4: Iterate through sessions
        form_state = extract_form_fields(html)
        session_options = extract_dropdown_options(html, CTL_SESSION)
        if not session_options:
            raise AttendanceWorkflowError("Session dropdown not populated after valid year selected.")
            
        prioritized_sessions = self._get_prioritized_sessions(session_options)
        logger.info("[step4] Prioritized sessions: %s", prioritized_sessions)
        
        from app.nitris.aspnet import has_attendance_table
        from app.nitris.parser import parse_attendance_html
        from app.nitris.exceptions import AttendanceParseError
        
        final_html = None
        for session_value, session_text in prioritized_sessions:
            logger.info("[step4] Probing session: %s (%s)", session_value, session_text)
            try:
                temp_html = await submit_postback(
                    self.client, url, form_state, CTL_SESSION,
                    {CTL_SESSION: session_value},
                    f"step4_session_{session_value}", self._debug,
                )
                
                if not has_attendance_table(temp_html):
                    logger.warning("[step4] Session %s response missing attendance table. Skipping.", session_text)
                    _save_debug_snapshot(temp_html, {"step": "failed_session", "session": session_text}, f"failed_session_{session_value}")
                    continue
                    
                # Try parsing the table to ensure valid rows exist
                try:
                    parse_attendance_html(temp_html)
                    final_html = temp_html
                    logger.info("[step4] Successfully selected session: %s", session_text)
                    break
                except AttendanceParseError as e:
                    logger.warning("[step4] Session %s has table but parse failed: %s. Skipping.", session_text, e)
                    _save_debug_snapshot(temp_html, {"step": "failed_session_parse", "session": session_text, "error": str(e)}, f"failed_session_parse_{session_value}")
                    continue
                    
            except InvalidContextError as e:
                logger.warning("[step4] Session %s invalid context (503 redirect). Skipping.", session_text)
                continue
                
        if not final_html:
            raise AttendanceTableMissingError("All session fallbacks failed to produce a valid attendance table.")

        # Always save final snapshot unconditionally as requested for debugging
        html = final_html
        _save_debug_snapshot(
            html, 
            {"step": "step4_final", "status": 200, "url": url_str, "response_size": len(html), "viewstate_present": "__VIEWSTATE" in html, "table_found": True},
            "step4_final"
        )

        logger.info("Attendance workflow complete — valid table and rows found")
        return html

    # ── Helpers ─────────────────────────────────────────────────

    def _build_attendance_url(self) -> httpx.URL:
        """Build the attendance page URL with raw query string."""
        raw_path = f"{ATTENDANCE_PAGE_PATH}?{ATTENDANCE_RAW_QUERY}".encode("ascii")
        return httpx.URL(self.base_url).copy_with(raw_path=raw_path)

    @staticmethod
    def _get_sorted_academic_years(options: list[tuple[str, str]]) -> list[tuple[str, str]]:
        """Extract and sort all valid 4-digit academic years from options."""
        import re
        valid_years = []
        for value, text in options:
            match = re.search(r"(\d{4})", text)
            if match:
                valid_years.append((int(match.group(1)), value, text))
                
        valid_years.sort(key=lambda x: x[0], reverse=True)
        return [(item[1], item[2]) for item in valid_years]
        
    @staticmethod
    def _get_prioritized_sessions(options: list[tuple[str, str]]) -> list[tuple[str, str]]:
        """Order sessions: Spring first, Autumn second, others remaining."""
        spring = []
        autumn = []
        others = []
        
        for value, text in options:
            lower_text = text.lower()
            if "spring" in lower_text:
                spring.append((value, text))
            elif "autumn" in lower_text:
                autumn.append((value, text))
            else:
                others.append((value, text))
                
        return spring + autumn + others

    @staticmethod
    def _pick_option(options: list[tuple[str, str]], preferred: str, fallback_idx: int = -1) -> str:
        """Pick a dropdown option by text match, falling back to index."""
        # Normalize preferred string by removing all whitespace, slashes, and dashes
        pref_clean = preferred.lower().replace(" ", "").replace("/", "").replace("-", "")
        for value, text in options:
            text_clean = text.lower().replace(" ", "").replace("/", "").replace("-", "")
            if pref_clean in text_clean or text_clean in pref_clean:
                return value
        for value, text in options:
            if preferred.lower() in text.lower() or text.lower() in preferred.lower():
                return value
        if options:
            # Avoid picking `--Select--` (value '0' or empty) if fallback_idx is 0 and valid options exist
            if fallback_idx == 0 and len(options) > 1 and options[0][0] in ("0", ""):
                return options[1][0]
            return options[fallback_idx][0]
        return ""

    # ── Messages Workflow ───────────────────────────────────────

    async def fetch_messages_list(self) -> str:
        """Fetch the AllMessages.aspx page."""
        from app.nitris.constants import MESSAGES_PAGE_PATH
        logger.info("Fetching all messages list...")
        headers = {"Referer": f"{self.base_url}/nitris/Student/Home/Home.aspx"}
        resp = await self.client.get(MESSAGES_PAGE_PATH, headers=headers, follow_redirects=False)
        
        if resp.status_code in (301, 302, 303, 307, 308):
            loc = resp.headers.get("Location", "")
            if "Login.aspx" in loc:
                raise SessionExpiredError("Session expired — redirected to login.")
            raise AttendanceWorkflowError(f"Messages GET redirected to {loc}")
            
        if resp.status_code != 200:
            raise AttendanceWorkflowError(f"Messages GET returned {resp.status_code}")
            
        return resp.text

    async def fetch_message_detail(self, token: str) -> str:
        """Fetch details of a single message by token."""
        from app.nitris.constants import MESSAGE_DETAIL_PATH
        logger.info("Fetching message detail for token: %s", token)
        url = f"{MESSAGE_DETAIL_PATH}?i={token}"
        headers = {"Referer": f"{self.base_url}/nitris/Student/Home/AllMessages.aspx"}
        resp = await self.client.get(url, headers=headers, follow_redirects=False)
        
        if resp.status_code in (301, 302, 303, 307, 308):
            loc = resp.headers.get("Location", "")
            if "Login.aspx" in loc:
                raise SessionExpiredError("Session expired — redirected to login.")
            raise AttendanceWorkflowError(f"Message Detail GET redirected to {loc}")
            
        if resp.status_code != 200:
            raise AttendanceWorkflowError(f"Message Detail GET returned {resp.status_code}")
            
        return resp.text

    async def submit_message_postback(self, event_target: str) -> tuple[str, str]:
        """Submit an ASP.NET postback for the message list and follow redirect to detail page.
        
        Returns tuple of (real_token, detail_html).
        """
        from app.nitris.constants import MESSAGES_PAGE_PATH
        from app.nitris.aspnet import extract_form_fields
        from app.nitris.exceptions import AttendanceWorkflowError
        import urllib.parse
        
        # 1. Fetch current list page to get fresh __VIEWSTATE and form fields
        list_html = await self.fetch_messages_list()
        form_state = extract_form_fields(list_html)
        
        # 2. Build postback payload
        payload = {
            **form_state,
            "__EVENTTARGET": event_target,
            "__EVENTARGUMENT": "",
        }
        
        # 3. Post to AllMessages.aspx and follow redirects to detail page!
        url = MESSAGES_PAGE_PATH
        headers = {"Referer": f"{self.base_url}{MESSAGES_PAGE_PATH}"}
        resp = await self.client.post(url, data=payload, headers=headers, follow_redirects=True)
        
        if resp.status_code != 200:
            raise AttendanceWorkflowError(f"Message Postback returned status {resp.status_code}")
            
        # Extract token from final redirect URL
        final_url = str(resp.url)
        parsed_url = urllib.parse.urlparse(final_url)
        params = urllib.parse.parse_qs(parsed_url.query)
        token = params.get("i", [""])[0]
        
        if not token:
            # Fallback: try parsing from response HTML or a query string
            if "Message.aspx?i=" in final_url:
                token = final_url.split("Message.aspx?i=")[-1]
                
        return token, resp.text

    async def download_attachment(self, attachment_path: str) -> bytes:
        """Download binary attachment files with active session cookies."""
        logger.info("Downloading attachment path: %s", attachment_path)
        headers = {"Referer": f"{self.base_url}/nitris/Student/Home/AllMessages.aspx"}
        resp = await self.client.get(attachment_path, headers=headers)
        
        if resp.status_code != 200:
            raise AttendanceWorkflowError(f"Failed to download attachment (status {resp.status_code})")
            
        return resp.content

    async def fetch_question_papers_page_url(self) -> httpx.URL:
        """Visit Home.aspx, extract dynamic AppId and AppName parameters for QP page from the sidebar menu, and return URL.
        
        Uses a self-healing link auto-resolver to bypass transient Base64 navigation parameters.
        """
        from bs4 import BeautifulSoup
        import urllib.parse
        
        home_url = "/nitris/Student/Home/Home.aspx"
        headers = {"Referer": f"{self.base_url}/nitris/Student/Home/Home.aspx"}
        resp = await self.client.get(home_url, headers=headers)
        
        if resp.status_code != 200:
            resp = await self.client.get("/nitris/Student/Default.aspx", headers=headers)
            
        html = resp.text
        soup = BeautifulSoup(html, "html.parser")
        
        qp_link = None
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "previousyear_questions.aspx" in href.lower():
                qp_link = href
                break
                
        if qp_link:
            if "../../" in qp_link:
                qp_link = "/nitris/Student/" + qp_link.split("../../Student/")[-1]
            elif qp_link.startswith("/"):
                pass
            else:
                qp_link = f"/nitris/Student/Examination/QuestionPaperUpload/{qp_link}"
            
            logger.info("Auto-resolved Previous Year Questions URL: %s", qp_link)
            parsed = urllib.parse.urlparse(qp_link)
            raw_path = f"{parsed.path}?{parsed.query}".encode("ascii")
            return httpx.URL(self.base_url).copy_with(raw_path=raw_path)
            
        # Fallback to hardcoded query structure if sidebar parsing fails
        from app.nitris.constants import QUESTION_PAPERS_PATH
        fallback_query = "AppId=Ng%3d%3d-dYSTlPCIpzE%3d&AppName=RXhhbWluYXRpb24%3d-%2fdxDr14tNrU%3d&SubModId=NTM%3d-%2fZhjgU%2bo648%3d&ModId=MzE%3d-4rLwL%2batXX4%3d"
        logger.warning("Auto-resolver failed to find PreviousYear_Questions.aspx in sidebar. Using fallback query parameters.")
        raw_path = f"{QUESTION_PAPERS_PATH}?{fallback_query}".encode("ascii")
        return httpx.URL(self.base_url).copy_with(raw_path=raw_path)

    async def fetch_question_papers(self, academic_year: str = "2025-26/Spring", department_value: str = "", subject_query: str = "") -> str:
        """Execute the ASP.NET postback flow to search for question papers on the portal."""
        from app.nitris.constants import CTL_QP_ACADEMIC_YEAR, CTL_QP_DEPARTMENT, CTL_QP_SUBJECT_SEARCH, CTL_QP_SEARCH_BTN
        from app.nitris.aspnet import extract_form_fields, extract_dropdown_options, submit_postback
        from app.nitris.exceptions import AttendanceWorkflowError
        
        # Step 0: Visit Module Default page to initialize the server-side ASP.NET module session context
        url = await self.fetch_question_papers_page_url()
        import urllib.parse
        parsed_url = urllib.parse.urlparse(str(url))
        default_raw_path = f"/nitris/Student/Default.aspx?{parsed_url.query}".encode("ascii")
        default_url = httpx.URL(self.base_url).copy_with(raw_path=default_raw_path)
        
        logger.info("[QP-Step0] Initializing module session context via Default.aspx")
        # Visit default page to seed context (ignore redirects or follow them)
        await self.client.get(default_url, headers={"Referer": f"{self.base_url}/nitris/Student/Home/Home.aspx"})
        
        # Step 1: GET initial QP page via resolved URL
        logger.info("[QP-Step1] GET previous question papers page")
        resp = await self.client.get(url, headers={"Referer": str(default_url)}, follow_redirects=False)
        
        if resp.status_code != 200:
            # Check if it was a 302 redirect. If so, follow it to handle potential context updates or auth drops
            if resp.status_code in (301, 302, 303, 307, 308):
                location = resp.headers.get("Location", "")
                logger.info("[QP-Step1] Page redirected to %s. Retrying with redirected URL.", location)
                if "Login.aspx" in location:
                    from app.nitris.exceptions import SessionExpiredError
                    raise SessionExpiredError("Session expired — redirected to login.")
                if "503.aspx" in location or "Error Pages" in location:
                    raise AttendanceWorkflowError("NITRIS portal returned a 503 Service Unavailable / Error Page. This usually indicates invalid/expired query parameters or lack of session context on the portal.")
                # If redirected to another dynamic URL, let's update url and fetch again!
                if location.startswith("/"):
                    redirect_url = httpx.URL(self.base_url).copy_with(raw_path=location.encode("ascii"))
                else:
                    redirect_url = httpx.URL(location)
                resp = await self.client.get(redirect_url, headers={"Referer": str(default_url)}, follow_redirects=True)
                if resp.status_code != 200:
                    raise AttendanceWorkflowError(f"Failed to load Question Papers page after redirect (status {resp.status_code})")
            else:
                raise AttendanceWorkflowError(f"Failed to load initial Question Papers page (status {resp.status_code})")
            
        html = resp.text
        from app.nitris.aspnet import is_error_page
        if is_error_page(html, resp):
            raise AttendanceWorkflowError("NITRIS portal returned a 503 Service Unavailable / Error Page. This usually indicates invalid/expired query parameters or lack of session context on the portal.")
        
        # Step 2: Select Academic Year dropdown
        form_state = extract_form_fields(html)
        year_options = extract_dropdown_options(html, CTL_QP_ACADEMIC_YEAR)
        
        # Match preferred academic year or fallback
        selected_year_value = self._pick_option(year_options, academic_year, fallback_idx=0)
        if not selected_year_value:
            logger.warning("No valid academic year options found in QP dropdown. Proceeding with form defaults.")
            selected_year_value = ""
            
        logger.info("[QP-Step2] Selecting Academic Year: %s", selected_year_value)
        
        # Postback to update form state for the selected academic year
        html = await submit_postback(
            self.client, url, form_state, CTL_QP_ACADEMIC_YEAR,
            {CTL_QP_ACADEMIC_YEAR: selected_year_value}, "qp_step2_year", self._debug
        )
        
        # Step 3: Populate Subject Search and trigger Search Button
        form_state = extract_form_fields(html)
        
        form_updates = {
            CTL_QP_ACADEMIC_YEAR: selected_year_value,
            CTL_QP_SUBJECT_SEARCH: subject_query,
        }
        
        # If department specified, select it too
        if department_value:
            dept_options = extract_dropdown_options(html, CTL_QP_DEPARTMENT)
            selected_dept = self._pick_option(dept_options, department_value, fallback_idx=-1)
            if selected_dept:
                form_updates[CTL_QP_DEPARTMENT] = selected_dept
                 
        logger.info("[QP-Step3] Triggering subject search postback. Query: '%s'", subject_query)
        
        # Submit postback triggering the search button
        html = await submit_postback(
            self.client, url, form_state, CTL_QP_SEARCH_BTN,
            form_updates, "qp_step3_search", self._debug
        )
        
        return html

    async def download_question_paper_pdf(self, academic_year: str, subject_query: str, event_target: str) -> bytes:
        """Submit postback for the GridView download button and download the PDF file bytes directly from response stream."""
        from app.nitris.aspnet import extract_form_fields
        from app.nitris.exceptions import AttendanceWorkflowError
        
        # 1. Fetch current search page to get fresh __VIEWSTATE and form fields
        search_html = await self.fetch_question_papers(academic_year=academic_year, subject_query=subject_query)
        form_state = extract_form_fields(search_html)
        
        # 2. Build postback payload
        payload = {
            **form_state,
            "__EVENTTARGET": event_target,
            "__EVENTARGUMENT": "",
        }
        
        # Exclude the search button from the postback payload to prevent re-triggering search event
        from app.nitris.constants import CTL_QP_SEARCH_BTN
        payload.pop(CTL_QP_SEARCH_BTN, None)
        
        url = await self.fetch_question_papers_page_url()
        headers = {"Referer": str(url)}
        
        logger.info("[QP-Download] Submitting postback for PDF target: %s", event_target)
        resp = await self.client.post(url, data=payload, headers=headers, follow_redirects=False)
        
        if resp.status_code != 200:
            raise AttendanceWorkflowError(f"Question Paper PDF postback returned status {resp.status_code}")
            
        content_type = resp.headers.get("Content-Type", "").lower()
        logger.info("[QP-Download] Received response: %d bytes, Content-Type: %s", len(resp.content), content_type)
        
        # 3. Check if it returned a direct PDF binary or a window.open redirection HTML
        if "application/pdf" in content_type:
            return resp.content
            
        html = resp.text
        # Search for window.open script tag
        import re
        import urllib.parse
        match = re.search(r"window\.open\(\"([^\"]*)\"", html)
        if not match:
            match = re.search(r"window\.open\(\'([^\']*)\'", html)
            
        if match:
            pdf_relative_path = match.group(1)
            pdf_absolute_url = urllib.parse.urljoin(str(url), pdf_relative_path)
            logger.info("[QP-Download] Resolved window.open PDF URL: %s", pdf_absolute_url)
            
            # Fetch the actual PDF bytes
            pdf_resp = await self.client.get(pdf_absolute_url, headers={"Referer": str(url)})
            if pdf_resp.status_code != 200:
                raise AttendanceWorkflowError(f"Failed to fetch PDF file from resolved URL (status {pdf_resp.status_code})")
                
            pdf_content_type = pdf_resp.headers.get("Content-Type", "").lower()
            if "application/pdf" not in pdf_content_type and b"%PDF" not in pdf_resp.content[:10]:
                raise AttendanceWorkflowError("Resolved URL did not return PDF binary bytes.")
                
            return pdf_resp.content
            
        # Ensure response is actually a PDF file or binary stream, not an HTML error
        if "text/html" in content_type and b"__VIEWSTATE" in resp.content:
            raise AttendanceWorkflowError("Server returned form HTML instead of PDF binary bytes. Postback failed.")
            
        return resp.content

    async def close(self) -> None:
        await self.client.aclose()


