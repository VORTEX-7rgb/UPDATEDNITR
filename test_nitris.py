import asyncio
import os
import httpx
from app.nitris.client import NitrisClient
from app.nitris.constants import ATTENDANCE_PAGE_PATH, ATTENDANCE_RAW_QUERY


async def test():
    print("[TEST 1] URL Construction:")
    client = NitrisClient()
    url = client._build_attendance_url()
    print(f"  URL: {url}")
    assert "Mw==" in url, "FAIL: = signs got encoded!"
    assert "5a3+" in url, "FAIL: + sign got encoded!"
    print("  PASS: No URL encoding corruption.\n")
    
    print("[TEST 2] Attendance Fetch Flow requires valid credentials to test fully.")
    print("  Run 'python -m app.main' and use Telegram bot to test E2E.\n")
    await client.close()


if __name__ == "__main__":
    asyncio.run(test())
