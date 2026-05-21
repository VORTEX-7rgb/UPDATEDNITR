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
        self.client = httpx.AsyncClient(
            base_url=self.base_url,
            headers=DEFAULT_HEADERS,
            timeout=30.0,
            follow_redirects=True,
        )

    # ── Login Flow ──────────────────────────────────────────────

    async def login(self, username: str, password: str) -> None:
        """Authenticate: init session → transform password → login → visit home."""
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
        for value, text in options:
            if preferred.lower() in text.lower():
                return value
        if options:
            return options[fallback_idx][0]
        return ""

    async def close(self) -> None:
        await self.client.aclose()
