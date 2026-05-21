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
ATTENDANCE_PAGE_PATH = "/nitris/Student/Attendance/ClassAttendance.aspx"

# Raw query string for initial attendance page GET.
# MUST stay raw — httpx URL-encodes Base64 '=' and '+' if passed as dict.
ATTENDANCE_RAW_QUERY = "AppId=Mw==-yoe1zDBzzaE=&AppName=QXR0ZW5kYW5jZSBhbmQgTGVhdmU=-eDrmHVOfXZY=&SubModId=MTI=-XTugfjokJls=&ModId=MTA=-5a3+Nygtxr8="

# ASP.NET form control names for attendance postbacks
CTL_SEMESTER = "ctl00$ctl00$ctl00$ContentPlaceHolder2$ContentPlaceHolder1$mainContent$ddlSemesterType"
CTL_ACADEMIC_YEAR = "ctl00$ctl00$ctl00$ContentPlaceHolder2$ContentPlaceHolder1$mainContent$ddlAcYr"
CTL_SESSION = "ctl00$ctl00$ctl00$ContentPlaceHolder2$ContentPlaceHolder1$mainContent$ddlSession"

# Parser element IDs
ATTENDANCE_TABLE_ID = "ContentPlaceHolder2_ContentPlaceHolder1_mainContent_gvSubjects"
STUDENT_INFO_LABEL_ID = "ContentPlaceHolder2_ContentPlaceHolder1_mainContent_lblSnameroll"
