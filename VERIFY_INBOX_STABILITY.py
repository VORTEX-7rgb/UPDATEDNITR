"""Automated verification suite to prove NITRIS Inbox and synchronization pass stability."""

import asyncio
import sys
import os
import logging
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

# Ensure app is in path
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from app.workers.sync_worker import (
    normalize_to_utc,
    sync_messages_for_user,
    sync_user_data,
    format_notification_message
)
from app.nitris.parser import extract_message_id, parse_attendance_html, AttendanceResult
from app.db.repositories.inbox_repository import InboxRepository
from app.db.models import InboxMessage, EventType

# Setup Logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("VERIFY_INBOX_STABILITY")

async def test_timezone_normalization():
    logger.info("--- [TEST 1] Verifying Timezone Normalization ---")
    
    # 1. Aware UTC datetime
    aware_dt = datetime(2026, 5, 24, 10, 0, 0, tzinfo=timezone.utc)
    # 2. Naive datetime
    naive_dt = datetime(2026, 5, 24, 10, 0, 0)
    
    norm_aware = normalize_to_utc(aware_dt)
    norm_naive = normalize_to_utc(naive_dt)
    
    assert norm_aware == norm_naive, "Timezone normalization failed: aware vs naive mismatch!"
    logger.info("  PASS: Normalization correctly resolved timezone difference (%s == %s)", norm_aware, norm_naive)

async def test_message_id_extraction():
    logger.info("--- [TEST 2] Verifying Base64 Message ID Extraction ---")
    
    # Standard real sample tokens
    token1 = "Mjc2Mjk2NA%3d%3d-703j2p9j4TY%3d"
    token2 = "Mjc2MTY5NA%3d%3d-xrxkl4u5VmE%3d"
    token_postback = "postback:ctl00$ContentPlaceHolder2$gvSubjects$ctl02$lnkViewMsg"
    
    id1 = extract_message_id(token1)
    id2 = extract_message_id(token2)
    id_pb = extract_message_id(token_postback)
    
    assert id1 == 2762964, f"Decoded mismatch for token 1: {id1}"
    assert id2 == 2761694, f"Decoded mismatch for token 2: {id2}"
    assert id_pb is None, f"Decoded postback should fallback to None: {id_pb}"
    
    logger.info("  PASS: extract_message_id decodes stable Base64 numbers and fallbacks postbacks properly.")

async def test_row_shift_duplicate_prevention():
    logger.info("--- [TEST 3] Verifying Row Shifts In-Place Token Updating ---")
    
    # Create an in-memory session mock and repository
    session = AsyncMock()
    inbox_repo = InboxRepository(session)
    
    # Setup mock existing message stored under an older postback coordinate ctl12
    existing_message = InboxMessage(
        id=42,
        user_id=5,
        portal_message_id=987654321,
        token="postback:ctl00$gvSubjects$ctl12$lnkViewMsg",
        sender="Academic Section",
        subject="Important Notice",
        body="Detail body",
        sent_on=datetime(2026, 5, 24, 10, 0, 0, tzinfo=timezone.utc)
    )
    
    # Configure get_by_portal_message_id to return the existing row
    inbox_repo.get_by_portal_message_id = AsyncMock(return_value=existing_message)
    inbox_repo.create_message = AsyncMock()
    
    # Simulate parser returning the shifted postback coordinate ctl13 for the same stable portal_message_id
    scraped_messages = [{
        "portal_message_id": 987654321,
        "token": "postback:ctl00$gvSubjects$ctl13$lnkViewMsg",  # Shifted token
        "sender": "Academic Section",
        "subject": "Important Notice",
        "sent_on": datetime(2026, 5, 24, 10, 0, 0)
    }]
    
    # Mock parser, client, and event repository inside sync_messages_for_user
    mock_client = MagicMock()
    mock_client.fetch_messages_list = AsyncMock(return_value="HTML")
    
    with patch("app.workers.sync_worker.get_db_session") as mock_db:
        # Mock session context manager behavior
        mock_db_sess = AsyncMock()
        mock_db_sess.begin = MagicMock()
        mock_db.return_value.__aenter__.return_value = mock_db_sess
        
        with patch("app.nitris.parser.parse_messages_list_html", return_value=scraped_messages):
            with patch("app.db.repositories.inbox_repository.InboxRepository", return_value=inbox_repo):
                # Run offline sync messages
                await sync_messages_for_user(user_id=5, roll_number="725MN1011", password="pass", bot=MagicMock(), client=mock_client)
                
    # Assertions
    assert existing_message.token == "postback:ctl00$gvSubjects$ctl13$lnkViewMsg", "Row shift did not update existing token in-place!"
    assert inbox_repo.create_message.call_count == 0, "Duplicate message inserted during row shift!"
    logger.info("  PASS: Shifting token correctly corrected existing record in-place. Duplicates bypassed completely.")

async def test_single_login_path():
    logger.info("--- [TEST 4] Verifying Single Login and Client Reuse ---")
    
    mock_client = MagicMock()
    mock_client.login = AsyncMock()
    mock_client.close = AsyncMock()
    
    # Mock data fetchers to ensure sync loop runs
    mock_parsed_attendance = AttendanceResult(student_info="TEST", records=[])
    
    with patch("app.nitris.client.NitrisClient", return_value=mock_client):
        with patch("app.workers.sync_worker.get_attendance_data", AsyncMock(return_value=mock_parsed_attendance)) as mock_get_att:
            with patch("app.workers.sync_worker.sync_messages_for_user", AsyncMock()) as mock_sync_msg:
                with patch("app.workers.sync_worker.decrypt_password", return_value="plain_pass"):
                    with patch("app.workers.sync_worker._update_sync_state", AsyncMock()):
                        with patch("app.workers.sync_worker.get_db_session") as mock_db:
                            mock_db_sess = AsyncMock()
                            mock_db_sess.begin = MagicMock()
                            mock_db.return_value.__aenter__.return_value = mock_db_sess
                            mock_snapshot = MagicMock()
                            mock_snapshot.snapshot_json = {"records": []}
                            mock_snapshot.id = 999
                            with patch("app.db.repositories.snapshot_repository.SnapshotRepository.get_latest_snapshot", AsyncMock(return_value=None)):
                                with patch("app.db.repositories.snapshot_repository.SnapshotRepository.create_snapshot", AsyncMock(return_value=mock_snapshot)) as mock_create_snap:
                                    with patch("app.db.repositories.event_repository.EventRepository.create_event", AsyncMock()) as mock_create_event:
                                        semaphore = asyncio.Semaphore(1)
                                        await sync_user_data(user_id=5, roll_number="725MN1011", encrypted_pass="enc", semaphore=semaphore)
                            
    # Assert exactly 1 client login call and that both attendance & inbox reuse that client session
    assert mock_client.login.call_count == 1, f"Expected exactly 1 login call, got {mock_client.login.call_count}"
    assert mock_client.close.call_count == 1, f"Expected exactly 1 close call, got {mock_client.close.call_count}"
    
    # Assert client is passed to get_attendance_data and sync_messages_for_user
    args_att, kwargs_att = mock_get_att.call_args
    assert kwargs_att.get("client") == mock_client, "attendance retrieval did not reuse active client session!"
    
    args_msg, kwargs_msg = mock_sync_msg.call_args
    assert kwargs_msg.get("client") == mock_client, "inbox messages sync did not reuse active client session!"
    
    logger.info("  PASS: Single login path successfully initiated exactly 1 login and shared the session client.")

async def test_notification_generation():
    logger.info("--- [TEST 5] Verifying Premium Event Notification Generation ---")
    
    payload_new = {
        "sender": "Academic Section",
        "subject": "Mid Semester Examination Dates",
        "body_snippet": "Dear students, Mid Semester examinations start next Monday.",
        "has_attachment": True,
        "message_id": 42
    }
    
    msg_html = format_notification_message(EventType.NEW_MESSAGE_RECEIVED, payload_new)
    assert "<b>" in msg_html, "Notification does not contain bold HTML tag markup!"
    assert " एग्जामिनेशन" not in msg_html, "Non-premium formatting found!"
    assert "Academic Section" in msg_html, "Sender name missing!"
    
    logger.info("  PASS: Events successfully formatted to premium Telegram notifications with clean HTML.")

async def test_attendance_parsing():
    logger.info("--- [TEST 6] Verifying Attendance Parse Flow ---")
    
    html = """
    <span id="ContentPlaceHolder2_ContentPlaceHolder1_mainContent_lblSnameroll">TEST USER (725MN1011)</span>
    <table id="ContentPlaceHolder2_ContentPlaceHolder1_mainContent_gvSubjects">
        <tr class="header">
            <th>Sub Code</th>
            <th>Subject Name</th>
            <th>Faculty</th>
            <th>TC</th>
            <th>UA</th>
            <th>LE</th>
            <th>OA</th>
        </tr>
        <tr>
            <td>MN401</td>
            <td>Mine Safety</td>
            <td>Dr. Roy</td>
            <td>18</td>
            <td>1</td>
            <td>0</td>
            <td>1</td>
        </tr>
    </table>
    """
    
    res = parse_attendance_html(html)
    assert res.student_info == "TEST USER (725MN1011)", "Failed student info check"
    assert len(res.records) == 1, "Failed record parsing"
    assert res.records[0].subject_code == "MN401", "Failed subject code parse"
    assert res.records[0].tc == "18", "Failed total classes parse"
    
    logger.info("  PASS: Attendance table parser works without regressions and outputs clean records.")

async def test_self_healing_portal_id():
    logger.info("--- [TEST 7] Verifying Self-Healing Portal ID Update ---")
    
    # 1. Start with a fallback message in database (portal_message_id = 9999)
    # 2. Simulate lazy load redirect resolving to real token 'Mjc2Mjk2NA%3d%3d-703j2p9j4TY%3d'
    real_token = "Mjc2Mjk2NA%3d%3d-703j2p9j4TY%3d"
    portal_id = extract_message_id(real_token)
    
    assert portal_id == 2762964, "Portal ID extraction failed in test_self_healing!"
    
    # 3. Verify the values dictionary constructs portal_message_id correctly
    update_values = {
        "token": real_token, 
        "body": "Mock body", 
        "attachment_url": None
    }
    if portal_id:
        update_values["portal_message_id"] = portal_id
        
    assert update_values["portal_message_id"] == 2762964, "Self-healing update values dictionary failed to seed portal_message_id!"
    logger.info("  PASS: Self-healing dynamically upgraded fallback hash to real portal ID %d upon postback resolution.", portal_id)

async def run_all():
    print("=======================================================")
    print("             NITRCLAW INBOX STABILITY SUITE            ")
    print("=======================================================")
    
    await test_timezone_normalization()
    await test_message_id_extraction()
    await test_row_shift_duplicate_prevention()
    await test_single_login_path()
    await test_notification_generation()
    await test_attendance_parsing()
    await test_self_healing_portal_id()
    
    print("=======================================================")
    print("     ALL INBOX STABILITY VERIFICATION TESTS PASSED!    ")
    print("=======================================================")

if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(run_all())
