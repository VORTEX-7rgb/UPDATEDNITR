"""Tests for Phase 2: Worker pool lane isolation (interactive vs background) and bounds."""
import asyncio
import os
import sys
import time
import pytest

# Ensure app is importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["ENCRYPTION_KEY"] = "ZmDfcTF7_60GrrY167zsiPd67pEvs0aGOv2oasOM1Pg="
os.environ["BOT_TOKEN"] = "test"
os.environ["DATABASE_URL"] = "postgresql+asyncpg://postgres:postgres@localhost:5432/test"


@pytest.mark.asyncio
async def test_lane_isolation_high_priority_routes_to_interactive():
    """HIGH priority jobs go to the interactive queue, MEDIUM/LOW to background queue."""
    from app.nitris.job_queue import NitrisJobQueue, Priority

    queue = NitrisJobQueue(num_workers=0)  # No active workers, just inspect queues

    fut_high = await queue.enqueue("high_job", user_id=1, priority=Priority.HIGH)
    fut_med = await queue.enqueue("med_job", user_id=2, priority=Priority.MEDIUM)
    fut_low = await queue.enqueue("low_job", user_id=3, priority=Priority.LOW)

    assert queue.get_interactive_queue_depth() == 1
    assert queue.get_background_queue_depth() == 2
    assert queue.get_queue_depth() == 3

    # Inspect interactive queue
    job_high = await queue._interactive_queue.get()
    assert job_high.job_type == "high_job"
    assert job_high.priority == Priority.HIGH

    # Inspect background queue
    job_med = await queue._background_queue.get()
    assert job_med.job_type == "med_job"
    job_low = await queue._background_queue.get()
    assert job_low.job_type == "low_job"


@pytest.mark.asyncio
async def test_lane_concurrency_interactive_unblocked_by_slow_background():
    """Interactive lane workers process HIGH priority jobs even while background lane is fully busy."""
    from app.nitris.job_queue import NitrisJobQueue, Priority

    # 1 interactive worker, 1 background worker
    queue = NitrisJobQueue(num_workers=2, interactive_workers=1)

    events = []

    @queue.handler("slow_bg_job")
    async def handle_slow_bg(job):
        events.append("bg_started")
        await asyncio.sleep(0.3)
        events.append("bg_finished")
        return {"success": True}

    @queue.handler("fast_interactive_job")
    async def handle_fast_interactive(job):
        events.append("interactive_started")
        await asyncio.sleep(0.05)
        events.append("interactive_finished")
        return {"success": True}

    await queue.start()
    try:
        # Enqueue slow background job
        bg_fut = await queue.enqueue("slow_bg_job", user_id=10, priority=Priority.LOW)
        await asyncio.sleep(0.05)  # Let bg worker start

        # Enqueue fast interactive job while background job is running
        interactive_fut = await queue.enqueue("fast_interactive_job", user_id=20, priority=Priority.HIGH)

        # Interactive job should complete BEFORE background job finishes!
        res_interactive = await interactive_fut
        assert res_interactive["success"] is True
        assert "interactive_finished" in events
        assert "bg_finished" not in events  # Background is still running!

        res_bg = await bg_fut
        assert res_bg["success"] is True
        assert "bg_finished" in events
    finally:
        await queue.stop()


@pytest.mark.asyncio
async def test_queue_depth_bound_rejection():
    """Queue rejects enqueue when max_queue_depth is exceeded."""
    from app.nitris.job_queue import NitrisJobQueue, Priority

    queue = NitrisJobQueue(num_workers=0, max_queue_depth=2)

    await queue.enqueue("job1", user_id=1, priority=Priority.HIGH)
    await queue.enqueue("job2", user_id=2, priority=Priority.MEDIUM)

    with pytest.raises(RuntimeError) as exc_info:
        await queue.enqueue("job3", user_id=3, priority=Priority.LOW)

    assert "queue full" in str(exc_info.value).lower() or "limit reached" in str(exc_info.value).lower()
