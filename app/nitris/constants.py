# Browser-like request headers
DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

AJAX_HEADERS = {
    **DEFAULT_HEADERS,
    "Content-Type": "application/json; charset=UTF-8",
    "X-Requested-With": "XMLHttpRequest",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Referer": "https://eapplication.nitrkl.ac.in/nitris/Login.aspx",
}

# Endpoints
LOGIN_PAGE_URL = "/nitris/Login.aspx"
GET_PASSWORD_ENDPOINT = "/nitris/Login.aspx/GetPassword"
LOGIN_USER_ENDPOINT = "/nitris/Login.aspx/LoginUser"
HOME_PAGE_URL = "/nitris/Student/Home/Home.aspx"
ALLMESSAGES_PAGE_URL = "/nitris/Student/Home/AllMessages.aspx"

# Attendance module — paths and form control names
ATTENDANCE_PAGE_PATH = "/nitris/Student/Attendance/ClassAttendance.aspx"
# Sub-page link keyword used to find the dynamically-rotated attendance URL in the
# Attendance module's sidebar HTML. NITRIS rotates the trailing -<random bytes>
# security tokens on every AppId/AppName/SubModId/ModId parameter periodically;
# hardcoding them guarantees breakage. We resolve them at runtime instead.
ATTENDANCE_MODULE_NAME = "Attendance and Leave"
ATTENDANCE_SIDEBAR_LINK_KEYWORD = "ClassAttendance.aspx"

# ASP.NET form control names for attendance postbacks
CTL_SEMESTER = "ctl00$ctl00$ctl00$ContentPlaceHolder2$ContentPlaceHolder1$mainContent$ddlSemesterType"
CTL_ACADEMIC_YEAR = "ctl00$ctl00$ctl00$ContentPlaceHolder2$ContentPlaceHolder1$mainContent$ddlAcYr"
CTL_SESSION = "ctl00$ctl00$ctl00$ContentPlaceHolder2$ContentPlaceHolder1$mainContent$ddlSession"

# Parser element IDs
ATTENDANCE_TABLE_ID = "ContentPlaceHolder2_ContentPlaceHolder1_mainContent_gvSubjects"
STUDENT_INFO_LABEL_ID = "ContentPlaceHolder2_ContentPlaceHolder1_mainContent_lblSnameroll"

# Messages Endpoints
MESSAGES_PAGE_PATH = "/nitris/Student/Home/AllMessages.aspx"
MESSAGE_DETAIL_PATH = "/nitris/Student/Home/Message.aspx"

# Messages Parser element IDs
MESSAGES_TABLE_ID = "ContentPlaceHolder2_gvSubjects"
MSG_FROM_LABEL_ID = "ContentPlaceHolder2_lblFrom"
MSG_SENTON_LABEL_ID = "ContentPlaceHolder2_lblSenton"
MSG_SUBJECT_LABEL_ID = "ContentPlaceHolder2_lblSubject"
MSG_BODY_LABEL_ID = "ContentPlaceHolder2_lblBody"

# Question Papers module — paths and form control names
QUESTION_PAPERS_PATH = "/nitris/Student/Examination/QuestionPaperUpload/PreviousYear_Questions.aspx"
# Sub-page link keyword for self-healing URL resolution (mirrors attendance).
QP_MODULE_NAME = "Examination"
QP_SIDEBAR_LINK_KEYWORD = "previousyear_questions.aspx"

# Form Control Selectors
CTL_QP_ACADEMIC_YEAR = "ctl00$ctl00$ctl00$ContentPlaceHolder2$ContentPlaceHolder1$mainContent$ddlAcYrSession"
CTL_QP_DEPARTMENT = "ctl00$ctl00$ctl00$ContentPlaceHolder2$ContentPlaceHolder1$mainContent$ddldepartment"
CTL_QP_SUBJECT_SEARCH = "ctl00$ctl00$ctl00$ContentPlaceHolder2$ContentPlaceHolder1$mainContent$txtsearch"
CTL_QP_SEARCH_BTN = "ctl00$ctl00$ctl00$ContentPlaceHolder2$ContentPlaceHolder1$mainContent$btnSearch"

# Grid ID
QUESTION_TABLE_ID = "ContentPlaceHolder2_ContentPlaceHolder1_mainContent_gvSubjects"

# NOTE: ATTENDANCE_RAW_QUERY and the hardcoded QP fallback query have been REMOVED.
# Both were using stale navigation tokens that NITRIS rotates periodically, which
# caused 503 errors and random failures. URLs are now resolved dynamically from
# the module sidebar HTML at runtime — see NitrisClient._resolve_module_subpage_url().
