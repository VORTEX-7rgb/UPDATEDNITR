"""Regression tests for PERF fixes:

P2 — Interactive logins jump ahead of queued background logins in the global
     pacing gate (the min-interval portal protection itself is UNCHANGED).
P5 — Background callers admit only up to (cap − RESERVED_INTERACTIVE_SLOTS),
     so a scheduler sync storm can never occupy every gateway slot.
P3 — Resolved module URLs are cached for a short TTL; a cache hit skips the
     Home.aspx discovery round-trip but STILL visits the launcher (which is
     what sets the per-session module context).
"""
import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.nitris.client import NitrisClient, _resolved_url_cache
from app.nitris.gateway import NitrisGateway


def _bg(coro, i: int = 0):
    return asyncio.create_task(coro, name=f"nitris-bg-{i}")


# ── P5: reserved interactive slots ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_p5_background_leaves_reserved_slots_for_taps():
    gw = NitrisGateway(max_concurrent=4, min_login_interval=0.0)
    admitted: list[int] = []
    release = asyncio.Event()

    async def bg_hold(i: int):
        async with gw.acquire():
            admitted.append(i)
            await release.wait()

    # 4 background tasks against cap=4 — but background limit = max(1, 4-2)=2.
    tasks = [_bg(bg_hold(i), i) for i in range(4)]
    await asyncio.sleep(0.08)
    assert len(admitted) == 2, (
        f"background admitted {len(admitted)} — reserved slots were not honored"
    )

    # An interactive tap uses the reserved headroom immediately.
    async with gw.acquire():
        assert len(admitted) == 2

    release.set()
    await asyncio.gather(*tasks)
    assert gw.metrics.active_requests == 0


@pytest.mark.asyncio
async def test_p5_interactive_uses_full_cap():
    gw = NitrisGateway(max_concurrent=4, min_login_interval=0.0)
    entered = 0

    async def tap():
        nonlocal entered
        async with gw.acquire():
            entered += 1
            await asyncio.sleep(0.05)

    # 4 concurrent interactive callers fill the entire cap (no reservation
    # applies to interactive work).
    await asyncio.gather(*(tap() for _ in range(4)))
    assert entered == 4
    assert gw.metrics.active_requests == 0


# ── P2: interactive-priority login pacing ────────────────────────────────────


@pytest.mark.asyncio
async def test_p2_interactive_logins_jump_queued_background():
    gw = NitrisGateway(max_concurrent=6, min_login_interval=0.35)
    client = SimpleNamespace(login=AsyncMock())
    finished: list[str] = []
    uid = {"prime": 1, "bg1": 2, "bg2": 3, "tap": 4}

    async def do_login(name: str):
        async with gw.acquire():
            await gw.login_through_gateway(client, "r", "p", user_id=uid[name])
        finished.append(name)

    async def prime():
        async with gw.acquire():
            await gw.login_through_gateway(client, "r", "p", user_id=uid["prime"])

    await prime()  # first login never paces — primes last_login_time

    t_bg1 = _bg(do_login("bg1"))
    await asyncio.sleep(0.02)
    t_bg2 = _bg(do_login("bg2"))          # queued behind bg1 as background
    await asyncio.sleep(0.03)
    t_tap = asyncio.create_task(do_login("tap"))   # default task name → interactive

    await asyncio.gather(t_bg1, t_bg2, t_tap)

    assert finished[0] == "bg1"
    assert finished[1] == "tap", (
        f"interactive tap must jump the queued background login, got {finished}"
    )
    assert finished[2] == "bg2"
    assert client.login.await_count == 4
    assert gw.metrics.active_requests == 0


# ── P3: resolved-URL cache fast-path ─────────────────────────────────────────

HOME_HTML = (
    '<html><body>'
    '<a href="/nitris/Student/Default.aspx?AppID=A1&AppName=att">Attendance and Leave</a>'
    '</body></html>'
)
MODULE_HTML = (
    '<html><body>'
    '<a href="/nitris/Student/Attendance/ClassAttendance.aspx?AppId=z9&SubModId=q">go</a>'
    '</body></html>'
)


@pytest.mark.asyncio
async def test_p3_resolver_cache_skips_home_discovery():
    _resolved_url_cache.clear()
    c = NitrisClient()
    calls: list[str] = []

    def _resp(text: str):
        return SimpleNamespace(status_code=200, text=text, headers={})

    async def fake_get(url, headers=None, follow_redirects=False, **kw):
        u = str(url)
        calls.append(u.split("?")[0])
        if "Home.aspx" in u:
            return _resp(HOME_HTML)
        return _resp(MODULE_HTML)

    c.client.get = fake_get

    url1 = await c._resolve_module_subpage_url("Attendance and Leave", "ClassAttendance.aspx")
    n_first = len(calls)
    assert n_first >= 2                       # full path: Home + launcher
    assert "ClassAttendance.aspx" in str(url1)

    url2 = await c._resolve_module_subpage_url("Attendance and Leave", "ClassAttendance.aspx")
    assert str(url1) == str(url2)
    assert len(calls) == n_first + 1, (
        "cache hit must skip the Home discovery GET "
        f"(expected +1 launcher call, total delta {len(calls) - n_first})"
    )

    _resolved_url_cache.clear()


def test_p3_probe_hint_prioritization():
    """Year/session hints reorder probe candidates: hinted value first,
    everything else keeps its relative order."""
    from app.nitris.client import _prioritize

    opts = [("v2", "2025-26"), ("v1", "2024-25"), ("v3", "2026-27")]
    assert [v for v, _ in _prioritize(opts, "v3")] == ["v3", "v2", "v1"]
    assert _prioritize(opts, None) is opts                      # no hint → untouched
    assert [v for v, _ in _prioritize(opts, "nope")] == ["v2", "v1", "v3"]


# ── Smart Academic Year Targeting ────────────────────────────────────────────


def test_smart_year_active_year_beats_future_placeholder():
    """A future placeholder year listed above the active one must NOT be
    probed first — computed current AY jumps the descending sort."""
    from datetime import datetime
    from app.nitris.client import NitrisClient

    opts = [("9", "2027-28"), ("8", "2026-27"), ("7", "2025-26")]
    out = NitrisClient._get_sorted_academic_years(opts, current_start_year=2026)
    assert out[0][1] == "2026-27"
    # Remaining years keep their newest-first fallback order.
    assert [t for _, t in out] == ["2026-27", "2027-28", "2025-26"]


def test_smart_year_calendar_rule():
    """Jul–Dec → Y-(Y+1); Jan–Jun → (Y-1)-Y."""
    from datetime import datetime
    from app.nitris.client import NitrisClient as C

    assert C._current_ay_start_year(datetime(2026, 8, 22)) == 2026   # Autumn 2026
    assert C._current_ay_start_year(datetime(2026, 12, 31)) == 2026
    assert C._current_ay_start_year(datetime(2026, 3, 15)) == 2025   # Spring of 25-26
    assert C._current_ay_start_year(datetime(2027, 1, 1)) == 2026    # Jan flips cleanly
    assert C._current_ay_start_year(datetime(2026, 6, 30)) == 2025
    assert C._current_ay_start_year(datetime(2026, 7, 1)) == 2026    # July boundary


def test_smart_year_composes_with_hint_cache():
    """Hint cache still wins overall: hint reorders AFTER smart targeting."""
    from datetime import datetime
    from app.nitris.client import NitrisClient, _prioritize

    opts = [("9", "2027-28"), ("8", "2026-27"), ("7", "2025-26")]
    ordered = NitrisClient._get_sorted_academic_years(opts, current_start_year=2026)
    final = _prioritize(ordered, "7")          # stored hint for this student
    assert [t for _, t in final] == ["2025-26", "2026-27", "2027-28"]
