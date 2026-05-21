import asyncio
import os
import sys
import time
import getpass

from app.nitris.client import NitrisClient
from app.nitris.parser import parse_attendance_html

async def test_cycle(i: int, username: str, password: str) -> bool:
    client = NitrisClient()
    success = False
    try:
        print(f"[{i:03d}] Initializing session & logging in...")
        await client.login(username, password)
        
        print(f"[{i:03d}] Navigating ASP.NET workflow & fetching attendance...")
        html = await client.fetch_attendance()
        
        print(f"[{i:03d}] Parsing attendance table...")
        data = parse_attendance_html(html)
        
        print(f"[{i:03d}] ✅ Success! Retrieved {len(data.records)} records for {data.student_info}.")
        success = True
    except Exception as e:
        print(f"[{i:03d}] ❌ Error: {e}")
    finally:
        # Closing the client effectively destroys the local session state (cookies/connections)
        await client.close()
        print(f"[{i:03d}] Session closed (logged out locally).")
        
    return success

async def main():
    print("=== NITRIS Attendance Stress Test ===")
    
    # Try reading from environment first
    username = os.getenv("NITRIS_USER")
    password = os.getenv("NITRIS_PASS")
    
    if not username or not password:
        print("\nCredentials not found in environment variables (NITRIS_USER, NITRIS_PASS).")
        username = input("Enter NITRIS Roll Number: ").strip()
        password = getpass.getpass("Enter NITRIS Password: ").strip()
        
    if not username or not password:
        print("Username or password cannot be empty.")
        return

    iterations = 100
    print(f"\n🚀 Starting stress test for {iterations} iterations...")
    print("Testing pipeline: Initialize Client -> Login -> Fetch Attendance -> Parse -> Close Client (Logout)")
    
    success_count = 0
    start_time = time.time()
    
    for i in range(1, iterations + 1):
        print(f"\n--- Iteration {i}/{iterations} ---")
        ok = await test_cycle(i, username, password)
        if ok:
            success_count += 1
            
        if i < iterations:
            # Small delay to prevent being rate-limited or IP-blocked by NITRIS
            print(f"[{i:03d}] Waiting 2 seconds before next iteration...")
            await asyncio.sleep(2)

    end_time = time.time()
    print(f"\n=====================================")
    print(f"        STRESS TEST COMPLETED        ")
    print(f"=====================================")
    print(f"Total Iterations: {iterations}")
    print(f"Successful:       {success_count}")
    print(f"Failed:           {iterations - success_count}")
    print(f"Success Rate:     {(success_count / iterations) * 100:.1f}%")
    print(f"Total Time:       {end_time - start_time:.2f} seconds")

if __name__ == "__main__":
    if sys.platform == "win32":
        # Fix for Windows Event Loop Issue (WinError 10054 / 121 / 64)
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
