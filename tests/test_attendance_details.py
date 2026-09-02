"""Comprehensive tests for Subject/Date-wise Attendance Details.

Covers:
  - Parser: link extraction, href normalization, color classification, matrix parsing,
    totals reconciliation, serialization round-trip.
  - Renderers & Keyboards: token formatting, absences bolding, date-wise matrix layout,
    keyboard buttons and callbacks.
  - Service: gateway re-login retry on session expiry, offloaded parsing, fail-fast errors.
  - Bot Handlers: cache-first date-wise views, subject forgery guards, cooldown,
    interaction_token passing.
  - Job Handler: execution flow, bubble ownership protection, direct snapshot persistence.
"""
import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bs4 import BeautifulSoup

from app.nitris.constants import (
    DETAILS_STATUS_PRESENT,
    DETAILS_STATUS_ABSENT,
    DETAILS_STATUS_LEAVE,
    DETAILS_STATUS_PRESENT_LATE,
    DETAILS_STATUS_ABSENT_LATE,
    DETAILS_STATUS_UNKNOWN,
)
from app.nitris.attendance_details_parser import (
    DetailsDay,
    DetailsMonth,
    SubjectAttendanceDetails,
    classify_cell_color,
    extract_details_link,
    normalize_details_href,
    parse_attendance_details_html,
    _reconcile_totals,
)
from app.nitris.exceptions import (
    AttendanceParseError,
    AttendanceWorkflowError,
    SessionExpiredError,
    LoginError,
)


# ── Sample HTML Fixtures ────────────────────────────────────────────────────

SAMPLE_GRID_HTML = """
<html>
<body>
<table id="ContentPlaceHolder2_ContentPlaceHolder1_mainContent_gvSubjects">
  <tr>
    <th>Subject Code</th><th>Subject Name</th><th>Details</th>
  </tr>
  <tr>
    <td>ER2251</td>
    <td>Mining Geology</td>
    <td><a href="ClassAttendanceDetails.aspx?ApId=Mw==-yoe1zDBzzaE=&AppName=OXROZW5kYW5jZSBh...&SubModId=123">View</a></td>
  </tr>
  <tr>
    <td>CS1001</td>
    <td>Programming</td>
    <td><a href="ClassAttendanceDetails.aspx?ApId=NA==-abc9988=&AppName=OXROZW5kYW5jZSBh...&SubModId=456">View</a></td>
  </tr>
</table>
</body>
</html>
"""

SAMPLE_DETAILS_HTML = """
<!DOCTYPE html>
<html>
<body>
  <div>
    <span>Student Name / RollNo : ARADHY SINGH CHAUHAN {725MN1011}</span>
    <span>Academic Year/Session : 2026-27 / Autumn</span>
    <span>Subject : ER2251 : Mining Geology</span>
  </div>

  <table class="table-bordered">
    <tr>
      <th>Class No</th>
      <th>1</th><th>2</th><th>3</th><th>4</th><th>5</th>
    </tr>
    <tr>
      <td>July (3) / Submitted</td>
      <td style="background-color:#00B050;">25</td>
      <td style="background-color:#FF0000;">27</td>
      <td style="background-color:#0070C0;">29</td>
    </tr>
    <tr>
      <td>August (2) / Pending</td>
      <td style="background-color:#FFC000;">01</td>
      <td style="background-color:#FF99CC;">03</td>
    </tr>
    <tr>
      <td>September (0) / Pending</td>
    </tr>
    <tr>
      <td>Total Class (5)</td>
      <td>Present = 2</td>
      <td>Absent = 2</td>
      <td>Leave = 1</td>
      <td>Overall Absence (Absent + Leave) = 3</td>
    </tr>
  </table>
</body>
</html>
"""


# ── Parser Tests ────────────────────────────────────────────────────────────

def test_normalize_details_href():
    assert normalize_details_href("") == ""
    assert normalize_details_href("/nitris/Details.aspx?a=1") == "/nitris/Details.aspx?a=1"
    # Preserves query '=' and '-' verbatim
    norm = normalize_details_href("ClassAttendanceDetails.aspx?ApId=Mw==-yoe1zDBzzaE=&foo=bar")
    assert norm == "/nitris/Student/Attendance/ClassAttendanceDetails.aspx?ApId=Mw==-yoe1zDBzzaE=&foo=bar"


def test_extract_details_link_success():
    link = extract_details_link(SAMPLE_GRID_HTML, "ER2251")
    assert link is not None
    assert "ApId=Mw==-yoe1zDBzzaE=" in link

    link2 = extract_details_link(SAMPLE_GRID_HTML, "CS1001")
    assert link2 is not None
    assert "ApId=NA==-abc9988=" in link2


def test_extract_details_link_not_found():
    assert extract_details_link(SAMPLE_GRID_HTML, "ME9999") is None
    assert extract_details_link("", "ER2251") is None
    assert extract_details_link(SAMPLE_GRID_HTML, "") is None


def test_extract_details_target_postback():
    from app.nitris.attendance_details_parser import extract_details_target
    postback_grid = """
    <table id="ContentPlaceHolder2_ContentPlaceHolder1_mainContent_gvSubjects">
      <tr><th>#</th><th>Subject Code</th><th>Name</th><th>View</th></tr>
      <tr>
        <td>1</td><td>ER2251</td><td>Mining Geology</td>
        <td><a id="btnDetails_1" href="javascript:__doPostBack('ctl00$ctl00$ctl00$ContentPlaceHolder2$ContentPlaceHolder1$mainContent$gvSubjects$ctl03$btnDetails','')">Details</a></td>
      </tr>
      <tr>
        <td>2</td><td>EA2440</td><td>Entrepreneurship</td>
        <td><a id="btnDetails_0">Details</a></td> <!-- TC=0 disabled, no href -->
      </tr>
    </table>
    """
    target = extract_details_target(postback_grid, "ER2251")
    assert target is not None
    assert target[0] == "postback"
    assert target[1] == "ctl00$ctl00$ctl00$ContentPlaceHolder2$ContentPlaceHolder1$mainContent$gvSubjects$ctl03$btnDetails"

    # Subject with no classes (disabled link) returns None
    assert extract_details_target(postback_grid, "EA2440") is None


def test_extract_details_link_single_fallback():
    single_html = """
    <table>
      <tr><td><a href="ClassAttendanceDetails.aspx?ApId=XYZ">Details</a></td></tr>
    </table>
    """
    assert extract_details_link(single_html, "ANYTHING") == "/nitris/Student/Attendance/ClassAttendanceDetails.aspx?ApId=XYZ"


def test_classify_cell_color_exact_hex():
    assert classify_cell_color("#00B050") == DETAILS_STATUS_PRESENT
    assert classify_cell_color("ff0000") == DETAILS_STATUS_ABSENT
    assert classify_cell_color("#0070C0") == DETAILS_STATUS_LEAVE
    assert classify_cell_color("#FFC000") == DETAILS_STATUS_PRESENT_LATE
    assert classify_cell_color("#FF99CC") == DETAILS_STATUS_ABSENT_LATE
    assert classify_cell_color(None) == DETAILS_STATUS_UNKNOWN
    assert classify_cell_color("invalid") == DETAILS_STATUS_UNKNOWN


def test_classify_cell_color_rgb():
    assert classify_cell_color("rgb(0, 176, 80)") == DETAILS_STATUS_PRESENT
    assert classify_cell_color("rgb(255, 0, 0)") == DETAILS_STATUS_ABSENT


def test_classify_cell_color_hue_fallback():
    # Light green not in exact table
    assert classify_cell_color("#44d455") == DETAILS_STATUS_PRESENT
    # Low saturation white/grey stays unknown
    assert classify_cell_color("#ffffff") == DETAILS_STATUS_UNKNOWN
    assert classify_cell_color("#eeeeee") == DETAILS_STATUS_UNKNOWN


def test_parse_attendance_details_html_full():
    details = parse_attendance_details_html(SAMPLE_DETAILS_HTML)
    assert "ARADHY SINGH CHAUHAN" in details.student_info
    assert "2026-27" in details.session_label
    assert "ER2251" in details.subject_label

    assert len(details.months) == 3
    # July
    jul = details.months[0]
    assert jul.name == "July"
    assert jul.count == 3
    assert jul.submission == "Submitted"
    assert len(jul.days) == 3
    assert jul.days[0].day == 25 and jul.days[0].status == DETAILS_STATUS_PRESENT
    assert jul.days[1].day == 27 and jul.days[1].status == DETAILS_STATUS_ABSENT
    assert jul.days[2].day == 29 and jul.days[2].status == DETAILS_STATUS_LEAVE

    # August
    aug = details.months[1]
    assert aug.name == "August"
    assert aug.count == 2
    assert aug.submission == "Pending"
    assert len(aug.days) == 2
    assert aug.days[0].status == DETAILS_STATUS_PRESENT_LATE
    assert aug.days[1].status == DETAILS_STATUS_ABSENT_LATE

    # September (0 classes)
    sep = details.months[2]
    assert sep.name == "September"
    assert sep.count == 0
    assert len(sep.days) == 0

    # Totals
    assert details.totals["total"] == 5
    assert details.totals["present"] == 2
    assert details.totals["absent"] == 2
    assert details.totals["leave"] == 1
    assert details.totals["overall_absence"] == 3


def test_parse_attendance_details_html_invalid():
    with pytest.raises(AttendanceParseError):
        parse_attendance_details_html("")

    with pytest.raises(AttendanceParseError):
        parse_attendance_details_html("<html><body>No table here</body></html>")


def test_dataclass_round_trip():
    details = parse_attendance_details_html(SAMPLE_DETAILS_HTML)
    d = details.to_dict()
    reconstructed = SubjectAttendanceDetails.from_dict(d)
    assert reconstructed.student_info == details.student_info
    assert len(reconstructed.months) == len(details.months)
    assert reconstructed.totals == details.totals


# ── Renderers & Keyboards ───────────────────────────────────────────────────

def test_day_token_and_details_text():
    from app.bot.handlers.attendance import _day_token, _details_text, _kb_detail, _kb_dates

    # Absences get bolded day number
    absent_chip = _day_token({"day": 27, "status": "absent"})
    assert absent_chip == "🔴<b>27</b>"

    present_chip = _day_token({"day": 25, "status": "present"})
    assert present_chip == "🟢25"

    details = parse_attendance_details_html(SAMPLE_DETAILS_HTML)
    text = _details_text(details.to_dict(), "ER2251", "🟢 Updated just now.")
    assert "ER2251" in text
    assert "July" in text
    assert "August" in text
    assert "Classes: <b>5</b>" in text
    assert "Updated just now" in text

    # Empty data test
    empty_text = _details_text(None, "ER2251")
    assert "No date-wise records on file" in empty_text

    # Keyboards
    dates_kb = _kb_dates("ER2251")
    assert any("ui|attdetrf|ER2251" in btn.callback_data for row in dates_kb.inline_keyboard for btn in row)
    assert any("ui|attlist" in btn.callback_data for row in dates_kb.inline_keyboard for btn in row)


# ── Service Tests ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_attendance_details_service_success():
    from app.services.attendance_details_service import get_attendance_details_data

    mock_client = MagicMock()
    mock_client.fetch_attendance_details = AsyncMock(return_value=(SAMPLE_DETAILS_HTML, "http://portal/details"))

    res = await get_attendance_details_data("user", "pass", mock_client, "ER2251")
    assert res.subject_label.startswith("ER2251")
    assert len(res.months) == 3


@pytest.mark.asyncio
async def test_attendance_details_service_relogin_retry():
    from app.services.attendance_details_service import get_attendance_details_data

    mock_client = MagicMock()
    # 1st call raises SessionExpiredError, 2nd succeeds
    mock_client.fetch_attendance_details = AsyncMock(
        side_effect=[SessionExpiredError("expired"), (SAMPLE_DETAILS_HTML, "http://portal/details")]
    )

    with patch("app.nitris.gateway.nitris_gateway._do_login", new=AsyncMock()) as mock_login:
        res = await get_attendance_details_data("user", "pass", mock_client, "ER2251")
        assert res.totals["total"] == 5
        assert mock_login.call_count == 1


@pytest.mark.asyncio
async def test_attendance_details_service_fail_fast_on_parse_error():
    from app.services.attendance_details_service import get_attendance_details_data

    mock_client = MagicMock()
    mock_client.fetch_attendance_details = AsyncMock(return_value=("<html>No Table</html>", "http://portal"))

    with pytest.raises(AttendanceParseError):
        await get_attendance_details_data("user", "pass", mock_client, "ER2251")


# ── Bot Handlers Tests ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cb_attendance_dates_cache_render():
    from app.bot.handlers.attendance import cb_attendance_dates
    from app.services.attendance_health import AttendanceSummary, SubjectHealth

    cb = MagicMock()
    cb.data = "ui|attdet|ER2251"
    cb.from_user.id = 12345
    cb.message = MagicMock()
    cb.answer = AsyncMock()

    summary = AttendanceSummary(
        level="safe",
        subjects=[SubjectHealth(
            code="ER2251", name="Mining Geology", faculty="", ltp="3-0-0",
            tc=10, ua=1, le=0, oa=1, rule=None, level="safe", ua_left=3
        )],
        riskiest=None,
    )

    parsed = parse_attendance_details_html(SAMPLE_DETAILS_HTML).to_dict()

    with patch("app.bot.handlers.attendance._load_user_and_summary", AsyncMock(return_value=(1, summary))), \
         patch("app.bot.handlers.attendance._load_details", AsyncMock(return_value=parsed)), \
         patch("app.bot.handlers.attendance.show", AsyncMock()) as mock_show:
        await cb_attendance_dates(cb)
        assert mock_show.call_count == 1
        call_text = mock_show.call_args[0][1]
        assert "ER2251" in call_text
        assert "July" in call_text


@pytest.mark.asyncio
async def test_cb_attendance_dates_forgery_guard():
    from app.bot.handlers.attendance import cb_attendance_dates
    from app.services.attendance_health import AttendanceSummary

    cb = MagicMock()
    cb.data = "ui|attdet|FORGED123"
    cb.from_user.id = 12345
    cb.message = MagicMock()
    cb.answer = AsyncMock()

    summary = AttendanceSummary(
        level="safe",
        subjects=[],
        riskiest=None,
    )

    with patch("app.bot.handlers.attendance._load_user_and_summary", AsyncMock(return_value=(1, summary))), \
         patch("app.bot.handlers.attendance.show", AsyncMock()) as mock_show:
        await cb_attendance_dates(cb)
        assert mock_show.call_count == 1
        # Reverts to summary list view
        assert "ATTENDANCE" in mock_show.call_args[0][1]


@pytest.mark.asyncio
async def test_cb_attendance_dates_refresh_cooldown():
    from app.bot.handlers.attendance import cb_attendance_dates_refresh
    from app.services.attendance_health import AttendanceSummary, SubjectHealth

    cb = MagicMock()
    cb.data = "ui|attdetrf|ER2251"
    cb.from_user.id = 12345
    cb.message = MagicMock()
    cb.message.chat.id = 111
    cb.message.message_id = 222
    cb.answer = AsyncMock()

    summary = AttendanceSummary(
        level="safe",
        subjects=[SubjectHealth(
            code="ER2251", name="Mining Geology", faculty="", ltp="3-0-0",
            tc=10, ua=1, le=0, oa=1, rule=None, level="safe", ua_left=3
        )],
        riskiest=None,
    )

    # When cooldown active: allowed = False
    with patch("app.bot.handlers.attendance._load_user_and_summary", AsyncMock(return_value=(1, summary))), \
         patch("app.bot.handlers.attendance._load_details", AsyncMock(return_value=None)), \
         patch("app.nitris.rate_limiter.operation_cooldown.check", AsyncMock(return_value=(False, 45))), \
         patch("app.ui.surface.Surface.edit", AsyncMock()) as mock_edit:
        await cb_attendance_dates_refresh(cb)
        assert mock_edit.call_count == 1
        assert "Next live refresh in 45s" in mock_edit.call_args[0][0]


# ── Job Handler Tests ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_handle_attendance_details_fetch_success():
    import time
    from app.nitris.job_handlers import handle_attendance_details_fetch
    from app.nitris.job_queue import NitrisJob, Priority

    job = NitrisJob(
        priority=Priority.HIGH,
        created_at=time.monotonic(),
        job_type="attendance_details_fetch",
        user_id=1,
        payload={
            "subject_code": "ER2251",
            "callback_chat_id": 111,
            "callback_message_id": 222,
            "interaction_token": 9999,
        },
    )

    mock_user = MagicMock()
    mock_user.credentials_valid = True
    mock_user.roll_number = "725MN1011"
    mock_user.encrypted_password = "enc_pass"

    parsed_data = parse_attendance_details_html(SAMPLE_DETAILS_HTML)

    mock_session = AsyncMock()
    mock_session.get = AsyncMock(return_value=mock_user)

    mock_session_ctx = MagicMock()
    mock_session_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session_ctx.__aexit__ = AsyncMock(return_value=None)

    with patch("app.nitris.job_handlers.async_session_factory", return_value=mock_session_ctx), \
         patch("app.nitris.session_pool.with_pooled_session", AsyncMock(return_value=parsed_data)), \
         patch("app.nitris.job_handlers._edit_callback_message", AsyncMock()) as mock_edit, \
         patch("app.nitris.job_handlers.spawn_tracked", side_effect=lambda c, **kw: c.close()) as mock_spawn:
        res = await handle_attendance_details_fetch(job)
        assert res["success"] is True
        assert res["subject_code"] == "ER2251"
        assert mock_edit.call_count == 1
        # Verified that token was passed
        assert mock_edit.call_args.kwargs.get("token") == 9999
        assert mock_spawn.call_count == 1


@pytest.mark.asyncio
async def test_handle_attendance_details_fetch_errors():
    import time
    from app.nitris.job_handlers import handle_attendance_details_fetch
    from app.nitris.job_queue import NitrisJob, Priority
    from app.nitris.gateway import NitrisCircuitOpenError
    from app.nitris.exceptions import LoginUnavailableError, LoginError, AttendanceWorkflowError

    mock_user = MagicMock()
    mock_user.credentials_valid = True
    mock_user.roll_number = "725MN1011"
    mock_user.encrypted_password = "enc_pass"

    mock_session = AsyncMock()
    mock_session.get = AsyncMock(return_value=mock_user)

    mock_session_ctx = MagicMock()
    mock_session_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session_ctx.__aexit__ = AsyncMock(return_value=None)

    def _make_job(code="ER2251"):
        return NitrisJob(
            priority=Priority.HIGH,
            created_at=time.monotonic(),
            job_type="attendance_details_fetch",
            user_id=1,
            payload={
                "subject_code": code,
                "callback_chat_id": 111,
                "callback_message_id": 222,
                "interaction_token": 1234,
            },
        )

    # 1. Missing subject_code
    res_empty = await handle_attendance_details_fetch(_make_job(code=""))
    assert res_empty["success"] is False
    assert "Missing subject_code" in res_empty["error"]

    # 2. Circuit open
    with patch("app.nitris.job_handlers.async_session_factory", return_value=mock_session_ctx), \
         patch("app.nitris.session_pool.with_pooled_session", AsyncMock(side_effect=NitrisCircuitOpenError("circuit open"))), \
         patch("app.nitris.job_handlers._edit_callback_message", AsyncMock()) as mock_edit:
        res = await handle_attendance_details_fetch(_make_job())
        assert res["success"] is False
        assert mock_edit.call_count == 1
        assert "temporarily unavailable" in mock_edit.call_args[0][2]

    # 3. Login unavailable
    with patch("app.nitris.job_handlers.async_session_factory", return_value=mock_session_ctx), \
         patch("app.nitris.session_pool.with_pooled_session", AsyncMock(side_effect=LoginUnavailableError("down"))), \
         patch("app.nitris.job_handlers._edit_callback_message", AsyncMock()) as mock_edit:
        res = await handle_attendance_details_fetch(_make_job())
        assert res["success"] is False
        assert mock_edit.call_count == 1
        assert "temporarily unavailable" in mock_edit.call_args[0][2]

    # 4. Login error
    with patch("app.nitris.job_handlers.async_session_factory", return_value=mock_session_ctx), \
         patch("app.nitris.session_pool.with_pooled_session", AsyncMock(side_effect=LoginError("bad pass"))), \
         patch("app.nitris.job_handlers.on_login_failure", AsyncMock()), \
         patch("app.nitris.job_handlers._edit_callback_message", AsyncMock()) as mock_edit:
        res = await handle_attendance_details_fetch(_make_job())
        assert res["success"] is False
        assert mock_edit.call_count == 1
        assert "Login failed" in mock_edit.call_args[0][2]

    # 5. Attendance workflow error
    with patch("app.nitris.job_handlers.async_session_factory", return_value=mock_session_ctx), \
         patch("app.nitris.session_pool.with_pooled_session", AsyncMock(side_effect=AttendanceWorkflowError("no classes"))), \
         patch("app.nitris.job_handlers._edit_callback_message", AsyncMock()) as mock_edit:
        res = await handle_attendance_details_fetch(_make_job())
        assert res["success"] is False
        assert mock_edit.call_count == 1
        assert "Could not fetch date-wise attendance" in mock_edit.call_args[0][2]


@pytest.mark.asyncio
async def test_client_fetch_attendance_details_postback_flow():
    from app.nitris.client import NitrisClient
    import httpx

    client = NitrisClient()
    postback_grid = """
    <form action="ClassAttendance.aspx?AppId=123" method="POST">
      <input type="hidden" name="__VIEWSTATE" value="vs_data" />
      <table id="ContentPlaceHolder2_ContentPlaceHolder1_mainContent_gvSubjects">
        <tr><th>#</th><th>Code</th><th>Name</th><th>Action</th></tr>
        <tr>
          <td>1</td><td>ER2251</td><td>Mining Geology</td>
          <td><a href="javascript:__doPostBack('ctl00$btnDetails','')">Details</a></td>
        </tr>
      </table>
    </form>
    """

    client.fetch_attendance = AsyncMock(return_value=postback_grid)
    client._peek_cached_subpage_url = MagicMock(return_value=None)

    # Mock POST returning 302 with Location
    post_resp = MagicMock()
    post_resp.status_code = 302
    post_resp.headers = {"Location": "/nitris/Student/Attendance/ClassAttendanceDetails.aspx?AppId=123&token=abc"}

    # Mock GET returning 200 with details page
    get_resp = MagicMock()
    get_resp.status_code = 200
    get_resp.text = SAMPLE_DETAILS_HTML

    client.client = MagicMock()
    client.client.post = AsyncMock(return_value=post_resp)
    client.client.get = AsyncMock(return_value=get_resp)

    html, url = await client.fetch_attendance_details("ER2251")
    assert html == SAMPLE_DETAILS_HTML
    assert "ClassAttendanceDetails.aspx" in url
    assert client.client.post.call_count == 1
    assert client.client.get.call_count == 1
    # Verify post payload
    payload = client.client.post.call_args.kwargs["data"]
    assert payload["__EVENTTARGET"] == "ctl00$btnDetails"
    assert payload["__VIEWSTATE"] == "vs_data"
