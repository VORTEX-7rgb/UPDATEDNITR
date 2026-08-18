"""NITRIS client — login + ASP.NET postback attendance workflow."""

import logging
import os
import re
import urllib.parse
from datetime import datetime
from typing import Optional

import httpx
from bs4 import BeautifulSoup

from app.config import config
from app.nitris.constants import (
    DEFAULT_HEADERS,
    AJAX_HEADERS,
    LOGIN_PAGE_URL,
    GET_PASSWORD_ENDPOINT,
    LOGIN_USER_ENDPOINT,
    HOME_PAGE_URL,
    ATTENDANCE_PAGE_PATH,
    ATTENDANCE_MODULE_NAME,
    ATTENDANCE_SIDEBAR_LINK_KEYWORD,
    CTL_SEMESTER,
    CTL_ACADEMIC_YEAR,
    CTL_SESSION,
    ATTENDANCE_TABLE_ID,
    QUESTION_PAPERS_PATH,
    QP_MODULE_NAME,
    QP_SIDEBAR_LINK_KEYWORD,
    CTL_QP_ACADEMIC_YEAR,
    CTL_QP_DEPARTMENT,
    CTL_QP_SUBJECT_SEARCH,
    CTL_QP_SEARCH_BTN,
    MESSAGES_PAGE_PATH,
    MESSAGE_DETAIL_PATH,
)
from app.nitris.exceptions import (
    LoginError,
    SessionExpiredError,
    AttendanceWorkflowError,
    AttendanceTableMissingError,
    HiddenFieldExtractionError,
    InvalidContextError,
)
from app.nitris.aspnet import (
    extract_form_fields,
    extract_dropdown_options,
    submit_postback,
    is_error_page,
    is_login_page,
    has_session_dropdown,
    has_attendance_table,
    _save_debug_snapshot,
)

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
                    resp = await self.client.post(
                        GET_PASSWORD_ENDPOINT,
                        json={"password": password},
                        headers=AJAX_HEADERS,
                    )
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
                    attempt,
                    max_attempts,
                    username,
                    e,
                )
                if attempt == max_attempts:
                    raise LoginError(f"Login failed after {max_attempts} attempts.") from e
                await asyncio.sleep(backoff)
                backoff *= 2.0  # 1s, 2s, 4s backoff

    # ── Self-Healing URL Resolver ───────────────────────────────

    async def _resolve_module_subpage_url(
        self,
        module_name: str,
        subpage_keyword: str,
    ) -> httpx.URL:
        """Visit Home.aspx → find module launcher URL → visit launcher → find sub-page URL.

        NITRIS rotates the trailing `-<random bytes>` security tokens on every
        AppId/AppName/SubModId/ModId parameter periodically. Hardcoding them
        guarantees breakage when NITRIS rotates. This resolver fetches the
        CURRENT valid URL from the sidebar HTML at runtime, so it works
        permanently regardless of token rotation.

        Args:
            module_name: The module display name as shown in the sidebar
                (e.g. "Attendance and Leave", "Examination").
            subpage_keyword: Lowercase substring to search for in the module's
                sidebar hrefs (e.g. "classattendance.aspx",
                "previousyear_questions.aspx").

        Returns:
            Absolute httpx.URL with the current valid tokens.
        """
        # Step 1: GET Home.aspx — the dashboard has all module launcher URLs in
        # the sidebar.
        home_resp = await self.client.get(
            HOME_PAGE_URL,
            headers={"Referer": f"{self.base_url}/nitris/Student/Home/Home.aspx"},
        )
        if home_resp.status_code != 200:
            raise AttendanceWorkflowError(
                f"Failed to load Home.aspx (status {home_resp.status_code}) — "
                f"cannot resolve module launcher URL."
            )
        home_html = home_resp.text
        if is_login_page(home_html):
            raise SessionExpiredError(
                "Session expired — Home.aspx returned login page."
            )

        # Step 2: Find the module launcher URL in the home sidebar by display name.
        # The sidebar shows module names as link text and launcher URLs as href.
        # The launcher URL is: /nitris/Student/Default.aspx?AppID=<...>&AppName=<...>
        launcher_url = self._find_launcher_by_module_name(home_html, module_name)
        if not launcher_url:
            raise AttendanceWorkflowError(
                f"Could not find module launcher URL for '{module_name}' in Home.aspx sidebar. "
                f"The portal UI may have changed."
            )
        logger.info(
            "Resolved %s module launcher URL: %s",
            module_name,
            launcher_url[:90],
        )

        # Step 3: Visit the launcher to set server-side Session['CurrentModule'].
        # Without this step, the sub-page returns 503.
        launcher_resp = await self.client.get(
            launcher_url,
            headers={"Referer": f"{self.base_url}{HOME_PAGE_URL}"},
            follow_redirects=True,
        )
        if launcher_resp.status_code != 200:
            raise AttendanceWorkflowError(
                f"Module launcher returned status {launcher_resp.status_code} for {module_name}"
            )
        launcher_html = launcher_resp.text
        if is_login_page(launcher_html):
            raise SessionExpiredError(
                "Session expired — launcher page returned login form."
            )

        # Step 4: Find the sub-page URL in the module launcher's sidebar.
        subpage_url = self._find_subpage_link(launcher_html, subpage_keyword)
        if not subpage_url:
            raise AttendanceWorkflowError(
                f"Could not find sub-page URL containing '{subpage_keyword}' "
                f"in {module_name} module sidebar."
            )
        logger.info(
            "Resolved %s sub-page URL: %s",
            module_name,
            subpage_url[:90],
        )

        # Return as absolute httpx.URL with the raw query string preserved.
        parsed = urllib.parse.urlparse(subpage_url)
        raw_path = f"{parsed.path}?{parsed.query}".encode("ascii")
        return httpx.URL(self.base_url).copy_with(raw_path=raw_path)

    @staticmethod
    def _find_launcher_by_module_name(home_html: str, module_name: str) -> Optional[str]:
        """Find the module launcher URL in Home.aspx sidebar by display name.

        The sidebar link text matches the module name (e.g. "Attendance and Leave").
        Returns the href (relative URL with full query string) or None.
        """
        soup = BeautifulSoup(home_html, "html.parser")
        for a in soup.find_all("a", href=True):
            text = a.get_text(strip=True)
            if not text:
                continue
            # Normalize whitespace for comparison
            text_clean = re.sub(r"\s+", " ", text).strip()
            if text_clean.lower() == module_name.lower():
                href = a["href"]
                # Resolve relative URL + unescape HTML entities
                href = BeautifulSoup(href, "html.parser").get_text()
                if href.startswith("/"):
                    return href
                if href.startswith("../../"):
                    return "/nitris/Student/" + href.split("../../Student/")[-1]
                if not href.startswith("http"):
                    return f"/nitris/Student/{href}"
                return href
        return None

    @staticmethod
    def _find_subpage_link(module_html: str, keyword: str) -> Optional[str]:
        """Find the first sub-page href containing the keyword (case-insensitive)
        in the module launcher's sidebar HTML.
        """
        soup = BeautifulSoup(module_html, "html.parser")
        kw_lower = keyword.lower()
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if kw_lower in href.lower():
                # Unescape HTML entities like &amp; -> &
                href = BeautifulSoup(href, "html.parser").get_text()
                if href.startswith("/"):
                    return href
                if href.startswith("../../"):
                    return "/nitris/Student/" + href.split("../../Student/")[-1]
                if not href.startswith("http"):
                    return f"/nitris/Student/{href}"
                return href
        return None

    # ── Attendance Workflow ─────────────────────────────────────

    async def fetch_attendance(self, semester: Optional[str] = None) -> str:
        """Execute the full ASP.NET postback workflow to get the attendance table HTML.

        Permanent fix: the attendance sub-page URL is resolved DYNAMICALLY from
        the Attendance module's sidebar at runtime, so it survives NITRIS's
        periodic token rotation. The module launcher is visited FIRST to set
        Session['CurrentModule'], which prevents 503 errors.

        Args:
            semester: Optional preferred semester type ("Spring" or "Autumn").
                If None (default), the current semester is auto-detected from
                today's date — Jul-Dec = Autumn, Jan-Jun = Spring. This
                prevents the bot from returning last-year's attendance data.

        Returns:
            Final HTML page containing the attendance table.
        """
        # Step 0: Resolve the attendance URL dynamically from the module sidebar.
        # This visits Home.aspx → Attendance module launcher → finds ClassAttendance
        # link with current valid tokens. Permanent fix for stale-URL 503s.
        url = await self._resolve_module_subpage_url(
            ATTENDANCE_MODULE_NAME,
            ATTENDANCE_SIDEBAR_LINK_KEYWORD,
        )
        url_str = str(url)

        # Determine the preferred semester based on today's date if not specified.
        if semester is None:
            semester = self._current_semester_type()
        logger.info("Using semester preference: %s", semester)

        # Step 1: GET initial attendance page
        logger.info("[step1] GET attendance page")
        headers = {"Referer": f"{self.base_url}/nitris/Student/Home/Home.aspx"}
        # follow_redirects=False to catch auth drops and 503 errors explicitly
        resp = await self.client.get(url, headers=headers, follow_redirects=False)

        if resp.status_code in (301, 302, 303, 307, 308):
            loc = resp.headers.get("Location", "")
            if "Login.aspx" in loc:
                raise SessionExpiredError("Session expired — redirected to login.")
            if "503.aspx" in loc:
                raise InvalidContextError(
                    f"Attendance page redirected to 503 — module context not set. "
                    f"Location: {loc}"
                )
            raise AttendanceWorkflowError(f"Initial GET redirected to {loc}")

        if resp.status_code != 200:
            raise AttendanceWorkflowError(
                f"Initial GET returned {resp.status_code}"
            )

        html = resp.text
        logger.info("[step1] Got %d bytes", len(html))

        if is_login_page(html):
            raise SessionExpiredError("Session expired — login form detected.")
        if is_error_page(html, resp):
            raise InvalidContextError(
                "Attendance page returned 503 error page after launcher visit. "
                "The portal may have rotated tokens — retry sync."
            )

        if self._debug:
            _save_debug_snapshot(
                html,
                {
                    "step": "step1_initial",
                    "status": 200,
                    "url": url_str,
                    "response_size": len(html),
                    "viewstate_present": "__VIEWSTATE" in html,
                    "table_found": ATTENDANCE_TABLE_ID in html,
                },
                "step1_initial",
            )

        # Step 2: POST semester selection
        form_state = extract_form_fields(html)
        sem_options = extract_dropdown_options(html, CTL_SEMESTER)
        sem_value = self._pick_option(sem_options, semester, fallback_idx=-1)
        logger.info("[step2] Selecting semester: %s", sem_value)

        html = await submit_postback(
            self.client,
            url,
            form_state,
            CTL_SEMESTER,
            {CTL_SEMESTER: sem_value},
            "step2_semester",
            self._debug,
        )

        # Step 3: Iterate through academic years (latest first)
        form_state = extract_form_fields(html)
        year_options = extract_dropdown_options(html, CTL_ACADEMIC_YEAR)
        if not year_options:
            raise AttendanceWorkflowError(
                "Academic year dropdown not populated after step 2."
            )

        valid_years = self._get_sorted_academic_years(year_options)
        if not valid_years:
            raise AttendanceWorkflowError(
                f"No valid academic years found. Options: {year_options}"
            )

        logger.info("[step3] All year options: %s", year_options)

        valid_year_html = None
        for year_value, year_text in valid_years:
            logger.info("[step3] Probing year: %s (%s)", year_value, year_text)
            try:
                temp_html = await submit_postback(
                    self.client,
                    url,
                    form_state,
                    CTL_ACADEMIC_YEAR,
                    {CTL_ACADEMIC_YEAR: year_value},
                    f"step3_year_{year_value}",
                    self._debug,
                )

                if not has_session_dropdown(temp_html):
                    logger.warning(
                        "[step3] Year %s response missing session dropdown. Skipping.",
                        year_text,
                    )
                    continue

                valid_year_html = temp_html
                logger.info("[step3] Successfully selected year: %s", year_text)
                break

            except InvalidContextError:
                logger.warning(
                    "[step3] Year %s invalid context (503 redirect). Skipping.",
                    year_text,
                )
                continue

        if not valid_year_html:
            raise AttendanceWorkflowError("All academic year fallbacks failed.")

        html = valid_year_html

        # Step 4: Iterate through sessions — prefer the current semester type, then
        # sort remaining by recency (latest first) to avoid picking stale sessions.
        form_state = extract_form_fields(html)
        session_options = extract_dropdown_options(html, CTL_SESSION)
        if not session_options:
            raise AttendanceWorkflowError(
                "Session dropdown not populated after valid year selected."
            )

        prioritized_sessions = self._get_prioritized_sessions(
            session_options, preferred_semester=semester
        )
        logger.info("[step4] Prioritized sessions: %s", prioritized_sessions)

        from app.nitris.parser import parse_attendance_html
        from app.nitris.exceptions import AttendanceParseError

        final_html = None
        for session_value, session_text in prioritized_sessions:
            logger.info("[step4] Probing session: %s (%s)", session_value, session_text)
            try:
                temp_html = await submit_postback(
                    self.client,
                    url,
                    form_state,
                    CTL_SESSION,
                    {CTL_SESSION: session_value},
                    f"step4_session_{session_value}",
                    self._debug,
                )

                if not has_attendance_table(temp_html):
                    logger.warning(
                        "[step4] Session %s response missing attendance table. Skipping.",
                        session_text,
                    )
                    if self._debug:
                        _save_debug_snapshot(
                            temp_html,
                            {"step": "failed_session", "session": session_text},
                            f"failed_session_{session_value}",
                        )
                    continue

                # Try parsing the table to ensure valid rows exist
                try:
                    parse_attendance_html(temp_html)
                    final_html = temp_html
                    logger.info(
                        "[step4] Successfully selected session: %s", session_text
                    )
                    break
                except AttendanceParseError as e:
                    logger.warning(
                        "[step4] Session %s has table but parse failed: %s. Skipping.",
                        session_text,
                        e,
                    )
                    if self._debug:
                        _save_debug_snapshot(
                            temp_html,
                            {
                                "step": "failed_session_parse",
                                "session": session_text,
                                "error": str(e),
                            },
                            f"failed_session_parse_{session_value}",
                        )
                    continue

            except InvalidContextError:
                logger.warning(
                    "[step4] Session %s invalid context (503 redirect). Skipping.",
                    session_text,
                )
                continue

        if not final_html:
            raise AttendanceTableMissingError(
                "All session fallbacks failed to produce a valid attendance table."
            )

        # Save final snapshot ONLY when debugging — unconditional saves waste disk
        # I/O on every successful sync.
        if self._debug:
            _save_debug_snapshot(
                final_html,
                {
                    "step": "step4_final",
                    "status": 200,
                    "url": url_str,
                    "response_size": len(final_html),
                    "viewstate_present": "__VIEWSTATE" in final_html,
                    "table_found": True,
                },
                "step4_final",
            )

        logger.info("Attendance workflow complete — valid table and rows found")
        return final_html

    # ── Helpers ─────────────────────────────────────────────────

    @staticmethod
    def _current_semester_type() -> str:
        """Determine the current NITRIS semester type based on today's date.

        NIT Rourkela academic calendar:
          - Autumn semester: July — December (months 7 to 12)
          - Spring semester: January — June (months 1 to 6)

        Returns 'Autumn' or 'Spring'.
        """
        month = datetime.now().month  # 1-12
        if month in (7, 8, 9, 10, 11, 12):
            return "Autumn"
        return "Spring"

    @staticmethod
    def _get_sorted_academic_years(
        options: list[tuple[str, str]],
    ) -> list[tuple[str, str]]:
        """Extract and sort all valid 4-digit academic years from options.

        Returns list sorted by year DESCENDING (latest first) so the most recent
        academic year is tried first.
        """
        valid_years = []
        for value, text in options:
            match = re.search(r"(\d{4})", text)
            if match:
                valid_years.append((int(match.group(1)), value, text))

        valid_years.sort(key=lambda x: x[0], reverse=True)
        return [(item[1], item[2]) for item in valid_years]

    @staticmethod
    def _get_prioritized_sessions(
        options: list[tuple[str, str]],
        preferred_semester: Optional[str] = None,
    ) -> list[tuple[str, str]]:
        """Order sessions so the most relevant one is tried first.

        - If preferred_semester is given ('Autumn' or 'Spring'), sessions matching
          that type come first, then the other type, then any non-matching ones.
        - Within each group, sort by the latest year found in the text DESCENDING
          so the most recent session wins. This prevents the bot from picking
          last year's Spring when Autumn is current.
        """
        pref = (preferred_semester or "").lower()
        pref_match = []
        other_match = []
        others = []

        def _sort_key(item: tuple[str, str]) -> int:
            # Extract the latest 4-digit year from the text — bigger = more recent
            years = re.findall(r"(\d{4})", item[1])
            return max(int(y) for y in years) if years else 0

        for value, text in options:
            lower_text = text.lower()
            if pref and pref in lower_text:
                pref_match.append((value, text))
            elif "spring" in lower_text or "autumn" in lower_text:
                other_match.append((value, text))
            else:
                others.append((value, text))

        pref_match.sort(key=_sort_key, reverse=True)
        other_match.sort(key=_sort_key, reverse=True)
        others.sort(key=_sort_key, reverse=True)
        return pref_match + other_match + others

    @staticmethod
    def _pick_option(
        options: list[tuple[str, str]], preferred: str, fallback_idx: int = -1
    ) -> str:
        """Pick a dropdown option by text match, falling back to index."""
        # Normalize preferred string by removing all whitespace, slashes, and dashes
        pref_clean = (
            preferred.lower().replace(" ", "").replace("/", "").replace("-", "")
        )
        for value, text in options:
            text_clean = text.lower().replace(" ", "").replace("/", "").replace("-", "")
            if pref_clean in text_clean or text_clean in pref_clean:
                return value
        for value, text in options:
            if preferred.lower() in text.lower() or text.lower() in preferred.lower():
                return value
        if options:
            # Avoid picking `--Select--` (value '0' or empty) if fallback_idx is 0
            # and valid options exist
            if fallback_idx == 0 and len(options) > 1 and options[0][0] in ("0", ""):
                return options[1][0]
            return options[fallback_idx][0]
        return ""

    # ── Messages Workflow ───────────────────────────────────────

    async def fetch_messages_list(self) -> str:
        """Fetch the AllMessages.aspx page.

        AllMessages.aspx lives under /nitris/Student/Home/ and does NOT require
        a module launcher visit — it's a global student page.
        """
        logger.info("Fetching all messages list...")
        headers = {"Referer": f"{self.base_url}/nitris/Student/Home/Home.aspx"}
        resp = await self.client.get(
            MESSAGES_PAGE_PATH, headers=headers, follow_redirects=False
        )

        if resp.status_code in (301, 302, 303, 307, 308):
            loc = resp.headers.get("Location", "")
            if "Login.aspx" in loc:
                raise SessionExpiredError("Session expired — redirected to login.")
            raise AttendanceWorkflowError(f"Messages GET redirected to {loc}")

        if resp.status_code != 200:
            raise AttendanceWorkflowError(
                f"Messages GET returned {resp.status_code}"
            )

        if is_login_page(resp.text):
            raise SessionExpiredError("Session expired — login form detected.")

        return resp.text

    async def fetch_message_detail(self, token: str) -> str:
        """Fetch details of a single message by token."""
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
            raise AttendanceWorkflowError(
                f"Message Detail GET returned {resp.status_code}"
            )

        if is_login_page(resp.text):
            raise SessionExpiredError("Session expired — login form detected.")

        return resp.text

    async def submit_message_postback(self, event_target: str) -> tuple[str, str]:
        """Submit an ASP.NET postback for the message list and follow redirect to detail page.

        Returns tuple of (real_token, detail_html).
        """
        # 1. Fetch current list page to get fresh __VIEWSTATE and form fields
        list_html = await self.fetch_messages_list()
        form_state = extract_form_fields(list_html)

        # 2. Build postback payload
        payload = {
            **form_state,
            "__EVENTTARGET": event_target,
            "__EVENTARGUMENT": "",
        }

        # 3. Post to AllMessages.aspx and follow redirects to detail page
        url = MESSAGES_PAGE_PATH
        headers = {"Referer": f"{self.base_url}{MESSAGES_PAGE_PATH}"}
        resp = await self.client.post(
            url, data=payload, headers=headers, follow_redirects=True
        )

        if resp.status_code != 200:
            raise AttendanceWorkflowError(
                f"Message Postback returned status {resp.status_code}"
            )

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
        """Download binary attachment files with active session cookies and SSRF protection."""
        import urllib.parse
        import ipaddress

        logger.info("Downloading attachment path: %s", attachment_path)

        # SSRF Validation: Only allow valid relative paths or matching NITRIS portal hosts
        parsed = urllib.parse.urlparse(attachment_path)
        if parsed.scheme and parsed.scheme not in ("http", "https"):
            raise AttendanceWorkflowError(f"Disallowed URL scheme in attachment path: {parsed.scheme}")

        if parsed.netloc:
            base_parsed = urllib.parse.urlparse(self.base_url)
            if parsed.hostname != base_parsed.hostname:
                raise AttendanceWorkflowError(
                    f"SSRF violation: attachment host '{parsed.hostname}' does not match '{base_parsed.hostname}'"
                )
            try:
                ip = ipaddress.ip_address(parsed.hostname)
                if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                    raise AttendanceWorkflowError(f"SSRF violation: attachment IP '{ip}' is private/restricted")
            except ValueError:
                pass  # Domain name, which matched base_url host

        headers = {"Referer": f"{self.base_url}/nitris/Student/Home/AllMessages.aspx"}
        resp = await self.client.get(attachment_path, headers=headers)

        if resp.status_code != 200:
            raise AttendanceWorkflowError(
                f"Failed to download attachment (status {resp.status_code})"
            )

        return resp.content

    # ── Question Papers Workflow ────────────────────────────────

    async def fetch_question_papers_page_url(self) -> httpx.URL:
        """Resolve the PreviousYear_Questions.aspx URL dynamically.

        PERMANENT FIX: The original implementation searched Home.aspx for the QP
        link, but Home.aspx only contains module launcher URLs — sub-page URLs
        live on the Examination MODULE's sidebar. The old code always fell back
        to a hardcoded URL with stale tokens, causing 503 failures when NITRIS
        rotated them. This implementation uses the same self-healing resolver
        pattern as attendance: Home → Examination module launcher → find QP
        link in module sidebar → use that URL.
        """
        return await self._resolve_module_subpage_url(
            QP_MODULE_NAME,
            QP_SIDEBAR_LINK_KEYWORD,
        )

    async def fetch_question_papers(
        self,
        academic_year: str = "2025-26/Spring",
        department_value: str = "",
        subject_query: str = "",
    ) -> str:
        """Execute the ASP.NET postback flow to search for question papers on the portal."""
        # Step 0: Resolve the QP URL dynamically (sets module context as a side
        # effect via the launcher visit).
        url = await self.fetch_question_papers_page_url()
        parsed_url = urllib.parse.urlparse(str(url))
        default_raw_path = (
            f"/nitris/Student/Default.aspx?{parsed_url.query}".encode("ascii")
        )
        default_url = httpx.URL(self.base_url).copy_with(raw_path=default_raw_path)

        logger.info("[QP-Step0] Initializing module session context via Default.aspx")
        # Visit default page to seed context (the launcher visit in
        # _resolve_module_subpage_url already did this, but we re-visit the
        # launcher here too for safety in case the resolver's launcher URL
        # differs from the sub-page's launcher URL).
        await self.client.get(
            default_url,
            headers={"Referer": f"{self.base_url}/nitris/Student/Home/Home.aspx"},
        )

        # Step 1: GET initial QP page via resolved URL
        logger.info("[QP-Step1] GET previous question papers page")
        resp = await self.client.get(
            url, headers={"Referer": str(default_url)}, follow_redirects=False
        )

        if resp.status_code != 200:
            if resp.status_code in (301, 302, 303, 307, 308):
                location = resp.headers.get("Location", "")
                logger.info(
                    "[QP-Step1] Page redirected to %s. Retrying with redirected URL.",
                    location,
                )
                if "Login.aspx" in location:
                    raise SessionExpiredError(
                        "Session expired — redirected to login."
                    )
                if "503.aspx" in location or "Error Pages" in location:
                    raise AttendanceWorkflowError(
                        "NITRIS portal returned a 503 Service Unavailable / Error Page. "
                        "This usually indicates invalid/expired query parameters or lack "
                        "of session context on the portal."
                    )
                if location.startswith("/"):
                    redirect_url = httpx.URL(self.base_url).copy_with(
                        raw_path=location.encode("ascii")
                    )
                else:
                    redirect_url = httpx.URL(location)
                resp = await self.client.get(
                    redirect_url,
                    headers={"Referer": str(default_url)},
                    follow_redirects=True,
                )
                if resp.status_code != 200:
                    raise AttendanceWorkflowError(
                        f"Failed to load Question Papers page after redirect (status {resp.status_code})"
                    )
            else:
                raise AttendanceWorkflowError(
                    f"Failed to load initial Question Papers page (status {resp.status_code})"
                )

        html = resp.text
        if is_error_page(html, resp):
            raise AttendanceWorkflowError(
                "NITRIS portal returned a 503 Service Unavailable / Error Page. "
                "This usually indicates invalid/expired query parameters or lack "
                "of session context on the portal."
            )

        # Step 2: Select Academic Year dropdown
        form_state = extract_form_fields(html)
        year_options = extract_dropdown_options(html, CTL_QP_ACADEMIC_YEAR)

        selected_year_value = self._pick_option(
            year_options, academic_year, fallback_idx=0
        )
        if not selected_year_value:
            logger.warning(
                "No valid academic year options found in QP dropdown. "
                "Proceeding with form defaults."
            )
            selected_year_value = ""

        logger.info("[QP-Step2] Selecting Academic Year: %s", selected_year_value)

        html = await submit_postback(
            self.client,
            url,
            form_state,
            CTL_QP_ACADEMIC_YEAR,
            {CTL_QP_ACADEMIC_YEAR: selected_year_value},
            "qp_step2_year",
            self._debug,
        )

        # Step 3: Populate Subject Search and trigger Search Button
        form_state = extract_form_fields(html)

        form_updates = {
            CTL_QP_ACADEMIC_YEAR: selected_year_value,
            CTL_QP_SUBJECT_SEARCH: subject_query,
        }

        if department_value:
            dept_options = extract_dropdown_options(html, CTL_QP_DEPARTMENT)
            selected_dept = self._pick_option(
                dept_options, department_value, fallback_idx=-1
            )
            if selected_dept:
                form_updates[CTL_QP_DEPARTMENT] = selected_dept

        logger.info(
            "[QP-Step3] Triggering subject search postback. Query: '%s'",
            subject_query,
        )

        html = await submit_postback(
            self.client,
            url,
            form_state,
            CTL_QP_SEARCH_BTN,
            form_updates,
            "qp_step3_search",
            self._debug,
        )

        return html

    async def download_question_paper_bytes(
        self, academic_year: str, subject_query: str, event_target: str
    ) -> bytes:
        """Submit postback for the GridView download button and download the paper
        bytes directly from response stream.

        Handles BOTH PDF and ZIP — NITRIS returns ZIP archives for some lab /
        multi-paper subjects. The format is detected by signature bytes (magic
        numbers), not by Content-Type, because NITRIS sometimes serves a PDF
        with Content-Type: application/octet-stream or application/zip.

        Returns: raw bytes (PDF or ZIP). The caller sniffs the format from the
        first 4 bytes (b"%PDF" for PDF, b"PK\\x03\\x04" for ZIP).
        """
        # 1. Fetch current search page to get fresh __VIEWSTATE and form fields
        search_html = await self.fetch_question_papers(
            academic_year=academic_year, subject_query=subject_query
        )
        form_state = extract_form_fields(search_html)

        # 2. Build postback payload
        payload = {
            **form_state,
            "__EVENTTARGET": event_target,
            "__EVENTARGUMENT": "",
        }

        # Exclude the search button from the postback payload to prevent
        # re-triggering search event
        payload.pop(CTL_QP_SEARCH_BTN, None)

        url = await self.fetch_question_papers_page_url()
        headers = {"Referer": str(url)}

        logger.info("[QP-Download] Submitting postback for paper target: %s", event_target)
        resp = await self.client.post(
            url, data=payload, headers=headers, follow_redirects=False
        )

        if resp.status_code != 200:
            raise AttendanceWorkflowError(
                f"Question Paper postback returned status {resp.status_code}"
            )

        content_type = resp.headers.get("Content-Type", "").lower()
        logger.info(
            "[QP-Download] Received response: %d bytes, Content-Type: %s",
            len(resp.content),
            content_type,
        )

        # 3. Direct binary: PDF or ZIP — sniff from signature bytes, not Content-Type.
        #    NITRIS often misreports Content-Type; trust the magic numbers.
        head = resp.content[:4]
        if head == b"%PDF" or head == b"PK\x03\x04":
            return resp.content

        # 4. window.open(...) redirect — NITRIS sometimes returns an HTML page with
        #    a JavaScript window.open() call pointing to the actual file URL.
        html = resp.text
        match = re.search(r'window\.open\("([^"]*)"', html)
        if not match:
            match = re.search(r"window\.open\(\'([^\']*)\'", html)

        if match:
            file_relative_path = match.group(1)
            file_absolute_url = urllib.parse.urljoin(str(url), file_relative_path)
            logger.info("[QP-Download] Resolved window.open file URL: %s", file_absolute_url)

            file_resp = await self.client.get(
                file_absolute_url, headers={"Referer": str(url)}
            )
            if file_resp.status_code != 200:
                raise AttendanceWorkflowError(
                    f"Failed to fetch paper from resolved URL (status {file_resp.status_code})"
                )

            # Sniff from bytes — accept PDF or ZIP
            head2 = file_resp.content[:4]
            if head2 == b"%PDF" or head2 == b"PK\x03\x04":
                return file_resp.content

            raise AttendanceWorkflowError(
                f"Resolved URL did not return PDF or ZIP binary bytes (got signature {head2!r})."
            )

        # 5. Server returned form HTML instead of binary — postback failed.
        #    This typically means the postback target is invalid or expired.
        if "text/html" in content_type and b"__VIEWSTATE" in resp.content:
            raise AttendanceWorkflowError(
                "Server returned form HTML instead of paper bytes. Postback failed."
            )

        # 6. Last-resort: return whatever bytes we got (could be octet-stream with
        #    valid PDF/ZIP signature deeper in the content). Caller sniffs again.
        return resp.content

    # Backward-compat alias
    download_question_paper_pdf = download_question_paper_bytes

    async def close(self) -> None:
        await self.client.aclose()
