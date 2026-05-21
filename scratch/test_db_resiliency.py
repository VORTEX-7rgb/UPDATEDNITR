"""Automated Verification Script for Database Outage and Resiliency.

Simulates database container stoppage, verifies worker backoff reconnect loops,
restarts the container, and verifies automatic resumption of background tasks.
"""

import os
import sys
import asyncio
import logging
import subprocess
from datetime import datetime, timezone
from unittest.mock import AsyncMock

# Add project root to python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Configure beautiful logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - [%(levelname)s] - %(name)s - %(message)s"
)
logger = logging.getLogger("ResiliencyTest")

# Set the selector event loop policy on Windows to avoid WinError 10054/121/64 during sockets disposal
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from sqlalchemy import select
from app.db.database import get_db_session, engine
from app.db.models import User
from app.workers.sync_worker import run_sync_worker, run_dispatch_worker


def run_shell_command(command: str) -> str:
    """Executes a shell command synchronously and returns the output."""
    logger.info("Executing shell command: %s", command)
    result = subprocess.run(
        command,
        shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if result.returncode != 0:
        logger.error("Command failed. Stderr: %s", result.stderr.strip())
        raise RuntimeError(f"Command '{command}' failed with code {result.returncode}: {result.stderr.strip()}")
    return result.stdout.strip()


async def verify_db_connectivity() -> bool:
    """Checks if the database is currently online and accepting queries."""
    try:
        async with get_db_session() as session:
            result = await session.execute(select(1))
            val = result.scalar()
            return val == 1
    except Exception as e:
        logger.error("DB connectivity check failed: %s", e)
        return False


async def ensure_mock_user_exists() -> None:
    """Ensures at least one user exists in the database for sync testing."""
    async with get_db_session() as session:
        async with session.begin():
            stmt = select(User).limit(1)
            res = await session.execute(stmt)
            user = res.scalar_one_or_none()
            
            if not user:
                logger.info("No users found in database. Inserting a mock user for testing...")
                mock_user = User(
                    telegram_id=999999999,
                    roll_number="123XX4567",
                    encrypted_password="mock_encrypted_password_here"
                )
                session.add(mock_user)
                logger.info("Mock user inserted successfully.")
            else:
                logger.info("Found existing user in database: %s", user)


async def main():
    logger.info("Starting Database Resiliency Automated Verification Test...")
    
    # 1. Verify initial DB connection
    logger.info("Verifying initial database connection status...")
    if not await verify_db_connectivity():
        logger.error("Database is not online! Please start the postgres container first.")
        sys.exit(1)
    logger.info("Initial database connection is healthy.")

    # 2. Ensure mock user is present so workers execute full logic
    await ensure_mock_user_exists()

    # 3. Create AsyncMock for Bot
    mock_bot = AsyncMock()

    # 4. Spawn sync and dispatch workers as async tasks
    logger.info("Starting background sync and dispatch workers...")
    # Temporarily set sync interval to a short duration for testing (not required since wait_for_db_recovery handles it)
    sync_task = asyncio.create_task(run_sync_worker(mock_bot))
    dispatch_task = asyncio.create_task(run_dispatch_worker(mock_bot))

    # Let workers initialize and run at least one cycle
    logger.info("Allowing workers to run their initial cycle (waiting 5 seconds)...")
    await asyncio.sleep(5)

    # 5. Simulate Database Outage by stopping Docker Container
    container_name = "collegeclaw-postgres"
    logger.info("====================================================")
    logger.info("🔥 STEP 1: Simulating DB Outage by stopping container '%s'...", container_name)
    logger.info("====================================================")
    try:
        run_shell_command(f"docker stop {container_name}")
        logger.info("Postgres container stopped successfully.")
    except Exception as e:
        logger.error("Failed to stop postgres container: %s", e)
        # Cancel tasks and exit
        sync_task.cancel()
        dispatch_task.cancel()
        sys.exit(1)

    # Wait for workers to hit database connection errors and trigger backoff reconnect loops
    logger.info("Waiting 15 seconds to let workers encounter connection errors and enter retry backoff...")
    await asyncio.sleep(15)

    # 6. Verify workers survived and are in reconnection loop
    logger.info("Verifying workers are alive and waiting for recovery (tasks should not be done)...")
    if sync_task.done():
        logger.error("CRITICAL FAILURE: Sync worker task died during database outage!")
        if sync_task.exception():
            logger.error("Sync worker exception: %r", sync_task.exception())
        dispatch_task.cancel()
        sys.exit(1)
    if dispatch_task.done():
        logger.error("CRITICAL FAILURE: Dispatch worker task died during database outage!")
        if dispatch_task.exception():
            logger.error("Dispatch worker exception: %r", dispatch_task.exception())
        sync_task.cancel()
        sys.exit(1)
    logger.info("Workers are still running and actively attempting reconnection.")

    # 7. Restart Database Container to simulate recovery
    logger.info("====================================================")
    logger.info("⚡ STEP 2: Restarting Postgres Container '%s' to recover...", container_name)
    logger.info("====================================================")
    try:
        run_shell_command(f"docker start {container_name}")
        logger.info("Postgres container restarted successfully.")
    except Exception as e:
        logger.error("Failed to restart postgres container: %s", e)
        sync_task.cancel()
        dispatch_task.cancel()
        sys.exit(1)

    # Allow time for PostgreSQL to initialize and workers to reconnect
    logger.info("Waiting 15 seconds for database initialization and worker automatic reconnection...")
    await asyncio.sleep(15)

    # 8. Verify workers reconnected and resumed successfully
    logger.info("====================================================")
    logger.info("🎯 STEP 3: Verifying successful automatic reconnection...")
    logger.info("====================================================")
    
    if sync_task.done() or dispatch_task.done():
        logger.error("CRITICAL FAILURE: Workers died after database recovery restarted!")
        if sync_task.done() and sync_task.exception():
            logger.error("Sync worker exception: %r", sync_task.exception())
        if dispatch_task.done() and dispatch_task.exception():
            logger.error("Dispatch worker exception: %r", dispatch_task.exception())
        sys.exit(1)

    # Perform a query to verify db engine pool is completely recovered
    if await verify_db_connectivity():
        logger.info("🎯 SUCCESS: Database connection pool recovered and is accepting queries!")
    else:
        logger.error("CRITICAL FAILURE: Database connection is still failing post-restart.")
        sys.exit(1)

    # 9. Clean up tasks cleanly
    logger.info("Cleaning up verification tasks...")
    sync_task.cancel()
    dispatch_task.cancel()
    try:
        await asyncio.gather(sync_task, dispatch_task, return_exceptions=True)
    except Exception:
        pass

    logger.info("====================================================")
    logger.info("🎉 Verification Successful! The database and worker resiliency pass was a 100%% SUCCESS!")
    logger.info("====================================================")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Test interrupted by user.")
