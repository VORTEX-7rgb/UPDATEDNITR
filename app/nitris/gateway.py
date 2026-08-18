"""Single chokepoint gateway for all outbound NITRIS portal interactions.

Features:
  - Hard concurrency limit (Semaphore)
  - Downward-only adaptation on failure (reduces concurrency, widens login interval)
  - Strict login pacing (minimum interval between sequential login requests)
  - Circuit breaker (trips to OPEN after consecutive errors to protect portal & avoid 503 bans)
  - Diagnostic metrics for admin /status command
"""
from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, AsyncGenerator

from app.config import config
from app.nitris.exceptions import NitrisError, LoginError, SessionExpiredError

logger = logging.getLogger(__name__)

CREDENTIAL_COOLDOWN_SECONDS = 3600
CREDENTIAL_INVALID_THRESHOLD = 3


class CircuitState(str, Enum):
    CLOSED = "closed"        # Normal operation: all traffic permitted
    OPEN = "open"            # Portal outage detected: reject fast for recovery window
    HALF_OPEN = "half_open"  # Trial probe: testing single request for recovery


class CircuitBreakerOpenError(NitrisError):
    """Raised when request is rejected because the NITRIS gateway circuit is OPEN."""
    pass


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

        self._sem = asyncio.Semaphore(self.current_max_concurrent)
        self._login_lock = asyncio.Lock()
        self._state_lock = asyncio.Lock()

        self.circuit_state = CircuitState.CLOSED
        self.circuit_opened_at: float = 0.0
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
        self._sem = asyncio.Semaphore(self.current_max_concurrent)
        self.circuit_state = CircuitState.CLOSED
        self.circuit_opened_at = 0.0
        self.metrics = GatewayMetrics()

    def is_circuit_open(self) -> bool:
        """Check if circuit breaker is currently open."""
        if self.circuit_state == CircuitState.OPEN:
            elapsed = time.monotonic() - self.circuit_opened_at
            if elapsed >= self.circuit_recovery_seconds:
                self.circuit_state = CircuitState.HALF_OPEN
                logger.info("NITRIS Gateway circuit transitioned from OPEN to HALF_OPEN (trial probe)")
                return False
            return True
        return False

    @asynccontextmanager
    async def acquire(self, is_login: bool = False) -> AsyncGenerator[None, None]:
        """Async context manager guarding all NITRIS portal interactions.

        Usage:
            async with nitris_gateway.acquire(is_login=True):
                await client.login(roll, password)
        """
        # 1. Check Circuit Breaker
        if self.is_circuit_open():
            raise NitrisCircuitOpenError(
                f"NITRIS portal circuit is OPEN (outage detected). Retry in "
                f"{int(self.circuit_recovery_seconds - (time.monotonic() - self.circuit_opened_at))}s"
            )

        # 2. Acquire Concurrency Slot
        await self._sem.acquire()
        async with self._state_lock:
            self.metrics.active_requests += 1
            if is_login:
                self.metrics.active_logins += 1

        try:
            # 3. If login, enforce pacing interval
            if is_login:
                async with self._login_lock:
                    now = time.monotonic()
                    elapsed = now - self.metrics.last_login_time
                    if elapsed < self.current_login_interval:
                        delay = self.current_login_interval - elapsed
                        logger.debug("Pacing login by %.2fs", delay)
                        await asyncio.sleep(delay)
                    self.metrics.last_login_time = time.monotonic()
                    self.metrics.total_logins += 1

            start_time = time.monotonic()
            self.metrics.total_requests += 1
            yield

            # Success path
            await self._record_success(is_login, time.monotonic() - start_time)

        except Exception as exc:
            # Error path
            await self._record_error(exc)
            raise

        finally:
            async with self._state_lock:
                self.metrics.active_requests -= 1
                if is_login:
                    self.metrics.active_logins -= 1
            self._sem.release()

    async def login_through_gateway(self, client, username: str, password: str) -> None:
        """Pace a NITRIS login. MUST be called inside an acquire() block.

        Eliminates nested semaphore acquisition. Enforces minimum interval
        between logins and tracks login metrics without re-acquiring the semaphore.
        """
        # Enforce minimum interval between logins (pacing)
        async with self._login_lock:
            now = time.monotonic()
            elapsed = now - self.metrics.last_login_time
            if elapsed < self.current_login_interval:
                delay = self.current_login_interval - elapsed
                logger.debug("Pacing login by %.2fs", delay)
                await asyncio.sleep(delay)
            self.metrics.last_login_time = time.monotonic()

        async with self._state_lock:
            self.metrics.total_logins += 1
            self.metrics.active_logins += 1

        try:
            await client.login(username, password)
        finally:
            async with self._state_lock:
                self.metrics.active_logins -= 1

    async def _record_success(self, is_login: bool, latency: float) -> None:
        async with self._state_lock:
            self.metrics.consecutive_errors = 0
            self.metrics.consecutive_successes += 1

            if self.circuit_state == CircuitState.HALF_OPEN:
                self.circuit_state = CircuitState.CLOSED
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
