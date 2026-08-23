"""Single chokepoint gateway for all outbound NITRIS portal interactions.

Features:
  - Dynamic concurrency limit (adaptive admission controller)
  - Downward-only adaptation on failure (reduces concurrency, widens login interval)
  - Strict login pacing (minimum interval between sequential login requests)
  - Circuit breaker (trips to OPEN after consecutive errors to protect portal & avoid 503 bans)
  - Diagnostic metrics for admin /status command
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, AsyncGenerator
import httpx

from app.config import config
from app.nitris.exceptions import NitrisError, LoginError, SessionExpiredError, CredentialsQuarantinedError, InboxParseError

logger = logging.getLogger(__name__)

CREDENTIAL_COOLDOWN_SECONDS = config.NITRIS_CREDENTIAL_COOLDOWN_SECONDS


class CircuitState(str, Enum):
    CLOSED = "closed"        # Normal operation: all traffic permitted
    OPEN = "open"            # Portal outage detected: reject fast for recovery window
    HALF_OPEN = "half_open"  # Trial probe: testing single request for recovery


class CircuitBreakerOpenError(NitrisError):
    """Raised when request is rejected because the NITRIS gateway circuit is OPEN."""
    pass


# PERF P5: background work (scheduler syncs etc.) leaves this many gateway
# slots free so an interactive tap never queues behind a sync storm.
RESERVED_INTERACTIVE_SLOTS = config.NITRIS_RESERVED_INTERACTIVE_SLOTS


# Alias for backward and test compatibility
NitrisCircuitOpenError = CircuitBreakerOpenError


@dataclass
class GatewayMetrics:
    total_requests: int = 0
    total_logins: int = 0
    total_errors: int = 0
    consecutive_errors: int = 0
    consecutive_successes: int = 0
    active_requests: int = 0
    active_logins: int = 0
    circuit_trips: int = 0
    circuit_state: CircuitState = CircuitState.CLOSED
    last_error: Optional[str] = None
    last_error_time: Optional[float] = None
    last_login_time: float = 0.0


class NitrisGateway:
    """Controls and paces all outbound requests to NITRIS."""

    def __init__(
        self,
        max_concurrent: Optional[int] = None,
        min_login_interval: Optional[float] = None,
        circuit_error_threshold: Optional[int] = None,
        circuit_recovery_seconds: Optional[int] = None,
    ):
        self.configured_max_concurrent = max_concurrent or config.NITRIS_GATEWAY_MAX_CONCURRENT
        self.current_max_concurrent = self.configured_max_concurrent
        self.min_login_interval = min_login_interval or config.NITRIS_GATEWAY_MIN_LOGIN_INTERVAL
        self.current_login_interval = self.min_login_interval
        self.circuit_error_threshold = circuit_error_threshold or config.NITRIS_GATEWAY_CIRCUIT_ERROR_THRESHOLD
        self.circuit_recovery_seconds = circuit_recovery_seconds or config.NITRIS_GATEWAY_CIRCUIT_RECOVERY_SECONDS

        self._login_burst = max(1, int(config.NITRIS_LOGIN_BURST))
        self._login_tokens = float(self._login_burst)
        self._login_last_refill = time.monotonic()
        # PERF P2: deterministic grant queue — (interactive?, event) pairs.
        # Tokens are handed to the EARLIEST interactive waiter first; only
        # when none are waiting does the earliest background waiter get one.
        self._pace_queue: list[tuple[bool, asyncio.Event]] = []
        # In-memory quarantine guard (defense-in-depth). The DB `credentials_valid`
        # column is the source of truth; this set lets login_through_gateway()
        # refuse a quarantined user in O(1) WITHOUT touching the DB inside the
        # gateway lock. Seeded on startup from the DB and updated on every
        # quarantine/unquarantine transition.
        self._quarantined: set[int] = set()
        # asyncio.Condition doubles as the metrics mutex AND the admission
        # controller. Unlike a fixed asyncio.Semaphore (sized once and never
        # resized), the Condition enforces the *current* effective cap, so
        # downward/upward adaptation actually throttles NITRIS for real.
        self._state_lock = asyncio.Condition()

        self.circuit_state = CircuitState.CLOSED
        self.circuit_opened_at: float = 0.0
        # M2 fix: exactly ONE in-flight request may probe a HALF-OPEN circuit.
        self._probe_in_flight: bool = False
        self.metrics = GatewayMetrics()

        logger.info(
            "NITRIS Gateway initialized: max_concurrent=%d, login_interval=%.1fs, circuit_threshold=%d",
            self.configured_max_concurrent,
            self.min_login_interval,
            self.circuit_error_threshold,
        )

    @property
    def max_concurrent(self) -> int:
        return self.current_max_concurrent

    @property
    def _metrics(self) -> GatewayMetrics:
        self.metrics.circuit_state = self.circuit_state
        return self.metrics

    def _reset_metrics_for_testing(self) -> None:
        """Reset gateway state for pytest isolation."""
        self.current_max_concurrent = self.configured_max_concurrent
        self.circuit_state = CircuitState.CLOSED
        self.circuit_opened_at = 0.0
        self.metrics = GatewayMetrics()

    def is_circuit_open(self) -> bool:
        """Pure predicate: True while NEW traffic should be rejected.

        M2 fix: this method NO LONGER mutates state. The old version flipped
        OPEN→HALF_OPEN as a side effect of a read — every concurrent caller
        then saw False and stampeded the recovering portal simultaneously,
        re-tripping the circuit in a stutter loop. State transitions now
        happen only inside acquire()'s locked admission section, which admits
        exactly ONE recovery probe into HALF_OPEN and rejects everyone else.

        HALF_OPEN counts as open here so background callers (the scheduler)
        skip claiming work while a recovery probe is in flight.
        """
        if self.circuit_state == CircuitState.HALF_OPEN:
            return True
        if self.circuit_state == CircuitState.OPEN:
            elapsed = time.monotonic() - self.circuit_opened_at
            return elapsed < self.circuit_recovery_seconds
        return False

    @asynccontextmanager
    async def acquire(self, is_login: bool = False) -> AsyncGenerator[None, None]:
        """Async context manager guarding all NITRIS portal interactions.

        Admission (M2 fix): circuit-state transitions AND capacity checks run
        inside ONE locked section. A HALF-OPEN circuit admits exactly ONE
        recovery probe; every other caller fails fast with
        NitrisCircuitOpenError instead of stampeding a recovering portal.

        Usage:
            async with nitris_gateway.acquire():
                await nitris_gateway.login_through_gateway(client, roll, pw, user_id=uid)
        """
        is_probe = False
        # PERF P5: classify the caller once — background work leaves a couple
        # of slots free so interactive taps always have headroom.
        interactive = self._caller_is_interactive()
        # ── Admission: ONE locked section owns circuit transitions + capacity ──
        async with self._state_lock:
            while True:
                # Circuit gate — atomic OPEN → HALF_OPEN → single-probe machine.
                if self.circuit_state == CircuitState.OPEN:
                    elapsed = time.monotonic() - self.circuit_opened_at
                    if elapsed >= self.circuit_recovery_seconds:
                        self.circuit_state = CircuitState.HALF_OPEN
                        logger.info(
                            "NITRIS Gateway circuit OPEN -> HALF_OPEN "
                            "(single recovery probe armed)"
                        )
                    else:
                        raise NitrisCircuitOpenError(
                            f"NITRIS portal circuit is OPEN (outage detected). Retry in "
                            f"{int(self.circuit_recovery_seconds - elapsed)}s"
                        )
                if self.circuit_state == CircuitState.HALF_OPEN:
                    if self._probe_in_flight:
                        raise NitrisCircuitOpenError(
                            "NITRIS circuit is HALF-OPEN: one recovery probe is "
                            "already in flight — retry in a few seconds."
                        )
                    self._probe_in_flight = True
                    is_probe = True

                # Capacity wait — re-checks the circuit after every wake, so a
                # queued caller can never jump across a transition that happened
                # while it waited.
                #
                # PERF P5: background callers admit only up to
                # (cap - RESERVED_INTERACTIVE_SLOTS); interactive callers may
                # use the full cap. The limit is recomputed each iteration
                # because the adaptive cap shrinks/grows at runtime.
                limit = self.current_max_concurrent
                if not interactive:
                    limit = max(1, limit - RESERVED_INTERACTIVE_SLOTS)
                if self.metrics.active_requests >= limit:
                    await self._state_lock.wait()
                    continue
                self.metrics.active_requests += 1
                if is_login:
                    self.metrics.active_logins += 1
                break

        try:
            # Pacing hook for explicit is_login=True callers only. Production
            # logins pace slot-free inside _do_login() (M3 fix), with
            # interactive priority (PERF P2).
            if is_login:
                await self._paced_wait(interactive=True)
                self.metrics.total_logins += 1

            start_time = time.monotonic()
            self.metrics.total_requests += 1
            yield

            # Success path
            duration = time.monotonic() - start_time
            await self._record_success(is_login, duration)
            try:
                from app.observability import metrics as _metrics
                await _metrics.record_gateway_op(duration, is_login=is_login)
            except Exception:
                pass

        except (LoginError, SessionExpiredError, CredentialsQuarantinedError, InboxParseError):
            # Per-user faults (bad credentials, one student's expired ASP.NET
            # session, a quarantined credential) must NOT trip the global
            # circuit breaker. One student's bad state must not take the whole
            # bot down for everyone.
            #
            # NOTE: LoginUnavailableError is deliberately NOT in this tuple.
            # It signals the PORTAL is down/misbehaving during login, so it
            # falls through to the generic NitrisError arm below and counts
            # toward the circuit breaker — protecting the portal and every
            # other user during an outage (H1 fix).
            #
            # M2: a per-user verdict means the PORTAL responded — recovery
            # evidence. Close a HALF-OPEN probe so waiters are released.
            await self._note_portal_responded()
            raise

        except NitrisError as exc:
            # NITRIS workflow/portal errors (503 error pages, postback failures,
            # invalid context) DO trip the circuit breaker — they signal the
            # portal itself is erroring, not a single user's session.
            await self._record_error(exc)
            raise

        except (httpx.TransportError, httpx.TimeoutException) as exc:
            # Network-level failures to reach NITRIS — trip the circuit
            await self._record_error(exc)
            raise

        except Exception:
            # Non-NITRIS errors (DB failures, Telegram errors, bugs)
            # These do NOT trip the NITRIS circuit breaker
            raise

        finally:
            async with self._state_lock:
                self.metrics.active_requests -= 1
                if is_login:
                    self.metrics.active_logins -= 1
                # Probe finished — whichever way it went, clear the flag so a
                # HALF-OPEN circuit can never deadlock on a lost probe.
                if is_probe:
                    self._probe_in_flight = False
                self._state_lock.notify_all()

    async def _note_portal_responded(self) -> None:
        """Per-user faults carry a PORTAL response (a user-level verdict), which
        is recovery evidence for the circuit breaker (M2)."""
        async with self._state_lock:
            if self.circuit_state == CircuitState.HALF_OPEN:
                self.circuit_state = CircuitState.CLOSED
                logger.info(
                    "NITRIS Gateway probe received a portal response: Circuit CLOSED"
                )

    def quarantine(self, user_id: int) -> None:
        """Add a user to the in-memory quarantine guard."""
        self._quarantined.add(user_id)

    def unquarantine(self, user_id: int) -> None:
        """Remove a user from the in-memory quarantine guard."""
        self._quarantined.discard(user_id)

    def is_quarantined(self, user_id: int) -> bool:
        """True if the user is currently blocked from automatic logins."""
        return user_id in self._quarantined

    @property
    def quarantined_user_count(self) -> int:
        return len(self._quarantined)

    @staticmethod
    def _caller_is_interactive() -> bool:
        """PERF P2/P5 classification of the current caller.

        The job queue names its workers 'nitris-bg-*' / 'nitris-shared-*';
        those run background syncs and cold acquisitions. Everything else —
        aiogram handler tasks (button taps), registration, direct calls,
        tests — is user-facing INTERACTIVE work.
        """
        task = asyncio.current_task()
        name = task.get_name() if task is not None else ""
        return not (name.startswith("nitris-bg") or name.startswith("nitris-shared"))

    def _refill_tokens_locked(self) -> None:
        """Refill the login token bucket. No lock needed — bucket state is
        only touched from the caller's own flow (single-waiter loop)."""
        now = time.monotonic()
        elapsed = now - self._login_last_refill
        if elapsed > 0:
            self._login_tokens = min(
                float(self._login_burst),
                self._login_tokens + elapsed / max(self.current_login_interval, 0.001),
            )
            self._login_last_refill = now

    def _dispatch_tokens(self) -> None:
        """Hand available tokens to queued waiters: earliest INTERACTIVE
        waiter first, then earliest background (PERF P2, deterministic FIFO
        within each class)."""
        while self._login_tokens >= 1.0 and self._pace_queue:
            idx = next(
                (i for i, (inter, _) in enumerate(self._pace_queue) if inter),
                0,
            )
            _, ev = self._pace_queue.pop(idx)
            self._login_tokens -= 1.0
            ev.set()

    @asynccontextmanager
    async def _pacing_turn(self, interactive: bool):
        """Acquire one login token from the bucket (PERF: burst-capable).

        Portal protection is rate-based, not gap-based: the AVERAGE login
        rate never exceeds 1/current_login_interval per second — bursts only
        consume tokens that accrued while the bucket sat idle.

        PERF P2 ordering preserved deterministically via the grant queue:
        an interactive caller takes the next refilled token ahead of any
        background waiter that joined after it.
        """
        ev = asyncio.Event()
        self._refill_tokens_locked()
        self._pace_queue.append((interactive, ev))
        self._dispatch_tokens()
        try:
            while True:
                if ev.is_set():
                    break
                deficit = max(1.0 - self._login_tokens, 0.0)
                delay = min(deficit * self.current_login_interval,
                            self.current_login_interval)
                # Timed wake guarantees refill progress even when we are the
                # only waiter; grants from others also set our event.
                try:
                    await asyncio.wait_for(ev.wait(), timeout=max(delay, 0.005))
                except asyncio.TimeoutError:
                    pass
                self._refill_tokens_locked()
                self._dispatch_tokens()
            yield
        finally:
            # Cancelled-before-grant cleanup.
            try:
                self._pace_queue.remove((interactive, ev))
            except ValueError:
                pass

    async def _paced_wait(self, interactive: bool) -> None:
        """Take a pacing token (waiting for refill/priority as needed)."""
        async with self._pacing_turn(interactive):
            self.metrics.last_login_time = time.monotonic()

    async def _release_slot(self) -> None:
        """Temporarily hand back the caller's portal concurrency slot.

        M3 fix: the paced-login wait used to run while HOLDING a slot, so N
        queued logins could occupy every slot while merely sleeping — starving
        interactive taps of capacity even when the portal was idle. The slot is
        released for the wait and reacquired before any portal I/O.
        """
        async with self._state_lock:
            if self.metrics.active_requests > 0:
                self.metrics.active_requests -= 1
            self._state_lock.notify_all()

    async def _reacquire_slot(self) -> None:
        """Re-take a portal concurrency slot (counterpart to _release_slot)."""
        async with self._state_lock:
            while self.metrics.active_requests >= self.current_max_concurrent:
                await self._state_lock.wait()
            self.metrics.active_requests += 1

    async def _do_login(self, client, username: str, password: str) -> None:
        """Paced login with metrics tracking. Shared by the automatic login
        path (login_through_gateway), the explicit verification path
        (verify_credentials), and the mid-workflow re-login path.

        M3 fix: the paced wait runs WITHOUT occupying a portal concurrency
        slot — the caller's slot is released around the sleep and reacquired
        before the login request is sent. Queued logins therefore cannot
        starve interactive taps of gateway capacity.
        """
        # ── Paced wait, slot-free, interactive-priority (M3 + PERF P2) ──
        await self._release_slot()
        try:
            await self._paced_wait(self._caller_is_interactive())
        finally:
            await self._reacquire_slot()

        async with self._state_lock:
            self.metrics.total_logins += 1
            self.metrics.active_logins += 1

        try:
            await client.login(username, password)
        finally:
            async with self._state_lock:
                self.metrics.active_logins -= 1

    async def login_through_gateway(self, client, username: str, password: str, *, user_id: int) -> None:
        """Automatic login path. MUST be called inside an acquire() block.

        ``user_id`` is REQUIRED — this is the credential-quarantine enforcement
        point. A quarantined user is refused in O(1) (in-memory, no DB access
        inside the gateway lock) before any NITRIS login attempt. On a confirmed
        LoginError (the portal explicitly rejected the credentials) the user is
        added to the in-memory guard so even a future handler that forgets the
        pre-check cannot re-attempt.

        LoginUnavailableError (portal down/misbehaving — H1 fix) propagates
        WITHOUT quarantining: an unreachable portal says nothing about the
        user's credentials.
        """
        if user_id in self._quarantined:
            raise CredentialsQuarantinedError(
                f"Credentials quarantined for user_id={user_id}; automatic login refused."
            )
        try:
            await self._do_login(client, username, password)
        except LoginError:
            self._quarantined.add(user_id)
            raise

    async def verify_credentials(self, client, username: str, password: str) -> None:
        """Explicit user-initiated credential verification (registration / re-registration).

        This is the ONLY login path allowed while credentials are quarantined.
        It intentionally has no user_id (the user is being created/updated) and
        does NOT consult the quarantine guard. On failure it raises LoginError;
        the caller decides whether to retry the user-typed password.
        """
        await self._do_login(client, username, password)

    async def _record_success(self, is_login: bool, latency: float) -> None:
        async with self._state_lock:
            self.metrics.consecutive_errors = 0
            self.metrics.consecutive_successes += 1

            if self.circuit_state == CircuitState.HALF_OPEN:
                self.circuit_state = CircuitState.CLOSED
                # M2: release the waiting crowd — the portal has recovered.
                self._probe_in_flight = False
                logger.info("NITRIS Gateway trial probe succeeded: Circuit CLOSED")

            # Slow recovery: after 10 consecutive successes, step concurrency back up
            if self.metrics.consecutive_successes >= 10:
                self.metrics.consecutive_successes = 0
                if self.current_max_concurrent < self.configured_max_concurrent:
                    self.current_max_concurrent += 1
                    logger.info("NITRIS Gateway recovered: concurrency increased to %d", self.current_max_concurrent)
                if self.current_login_interval > self.min_login_interval:
                    self.current_login_interval = max(self.min_login_interval, self.current_login_interval - 0.25)

    async def _record_error(self, exc: Exception) -> None:
        # Ignore client-level credential error from tripping global circuit
        is_client_fault = isinstance(exc, LoginError)

        async with self._state_lock:
            self.metrics.total_errors += 1
            self.metrics.last_error = str(exc)
            self.metrics.last_error_time = time.time()

            if not is_client_fault:
                # M2: a PORTAL fault during the recovery probe re-trips the
                # circuit immediately — no need to wait for a threshold.
                if self.circuit_state == CircuitState.HALF_OPEN:
                    self.circuit_state = CircuitState.OPEN
                    self.circuit_opened_at = time.monotonic()
                    self.metrics.circuit_trips += 1
                    logger.error(
                        "NITRIS Gateway recovery probe FAILED — Circuit re-TRIPPED to OPEN for %ds.",
                        self.circuit_recovery_seconds,
                    )
                    return

                self.metrics.consecutive_errors += 1
                self.metrics.consecutive_successes = 0

                # Downward adaptation on 3 consecutive errors
                if self.metrics.consecutive_errors == 3:
                    if self.current_max_concurrent > 2:
                        self.current_max_concurrent -= 1
                        logger.warning("NITRIS Gateway adapted downward: concurrency reduced to %d", self.current_max_concurrent)
                    if self.current_login_interval < 5.0:
                        self.current_login_interval = min(5.0, self.current_login_interval + 0.5)

                # Trip Circuit Breaker on threshold consecutive errors
                if self.metrics.consecutive_errors >= self.circuit_error_threshold:
                    if self.circuit_state != CircuitState.OPEN:
                        self.circuit_state = CircuitState.OPEN
                        self.circuit_opened_at = time.monotonic()
                        self.metrics.circuit_trips += 1
                        logger.error(
                            "NITRIS Gateway Circuit TRIPPED to OPEN after %d consecutive errors. Rejecting traffic for %ds.",
                            self.metrics.consecutive_errors,
                            self.circuit_recovery_seconds,
                        )

    def get_metrics(self) -> dict:
        """Snapshot of current gateway health for admin /status dashboard."""
        return {
            "circuit_state": self.circuit_state.value,
            "current_max_concurrent": self.current_max_concurrent,
            "configured_max_concurrent": self.configured_max_concurrent,
            "current_login_interval": round(self.current_login_interval, 2),
            "configured_login_interval": round(self.min_login_interval, 2),
            "circuit_threshold": self.circuit_error_threshold,
            "active_requests": self.metrics.active_requests,
            "active_logins": self.metrics.active_logins,
            "total_requests": self.metrics.total_requests,
            "total_logins": self.metrics.total_logins,
            "total_errors": self.metrics.total_errors,
            "consecutive_errors": self.metrics.consecutive_errors,
            "circuit_trips": self.metrics.circuit_trips,
            "last_error": self.metrics.last_error,
        }


# Singleton gateway instance
nitris_gateway = NitrisGateway()
