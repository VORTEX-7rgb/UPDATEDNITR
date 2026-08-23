"""ENV-as-source-of-truth guarantees: overrides must reach config AND consumers."""
from __future__ import annotations

import os
import subprocess
import sys


def _run_config_probe(env_overrides: dict[str, str], probe: str) -> list[str]:
    env = {**os.environ, **env_overrides}
    proc = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        env=env,
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.split()


def test_config_class_reads_env_overrides():
    out = _run_config_probe(
        {
            "RETENTION_SNAPSHOT_KEEP": "3",
            "NITRIS_SESSION_POOL_MAX": "77",
            "DISPATCH_PACING_SECONDS": "0.2",
        },
        "from app.config import Config; c = Config(); "
        "print(c.RETENTION_SNAPSHOT_KEEP, c.NITRIS_SESSION_POOL_MAX, c.DISPATCH_PACING_SECONDS)",
    )
    assert out == ["3", "77", "0.2"]


def test_consumer_modules_pick_up_env_values():
    out = _run_config_probe(
        {"COOLDOWN_ATTENDANCE_REFRESH": "42"},
        "import app.nitris.rate_limiter as rl; print(rl.COOLDOWN_ATTENDANCE_REFRESH)",
    )
    assert out == ["42"]


def test_defaults_preserved_when_env_unset():
    # No overrides: defaults must match the documented production behavior.
    out = _run_config_probe(
        {},
        "from app.config import Config; c = Config(); "
        "print(c.RETENTION_SNAPSHOT_KEEP, c.RETENTION_EVENT_DAYS, "
        "c.NITRIS_HTTP_TIMEOUT_SECONDS, c.DB_POOL_SIZE)",
    )
    assert out == ["10", "14", "30.0", "10"]
