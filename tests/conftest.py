"""Pytest bootstrap — MUST run before any `app.*` import.

Sets environment variables ahead of app.config's load_dotenv(override=False)
so the suite is hermetic:

  * DATABASE_URL points at the disposable ``collegeclaw_test`` Postgres
    database (docker container ``goclaw-dev-postgres-1``), NEVER at the live
    ``collegeclaw`` database.
  * ENCRYPTION_KEY is a dedicated throwaway Fernet key (app.db.crypto
    validates it at import time and raises without one).

Tests themselves are mock-based and do not require a reachable database;
the scratch DB exists so any test that does open a session can never touch
production data. Apply migrations to it manually if ever needed:
    DATABASE_URL=postgresql+asyncpg://goclaw:goclaw@localhost:5432/collegeclaw_test \
        alembic upgrade head
"""
import os

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+asyncpg://goclaw:goclaw@localhost:5432/collegeclaw_test",
)
os.environ.setdefault("ENCRYPTION_KEY", "Y1nnQ2hKQAFSUkVuOGbDIMpU-6Vhxsh5kFmQCYSR40g=")
os.environ.setdefault("BOT_TOKEN", "000:TEST_TOKEN")

# Pin operational TUNABLES to their CODE defaults so the suite always tests
# defaults regardless of the developer's .env. Why "set" instead of "pop":
# app.config runs load_dotenv(override=False) at import — popping a var lets
# dotenv inject .env's value into os.environ before the Config class body
# executes, whereas PRE-SETTING a var makes dotenv respect it (override=False)
# and Config reads exactly what we pinned here.
for _k, _v in {
    "NITRIS_GATEWAY_MAX_CONCURRENT": "8",
    "NITRIS_GATEWAY_MIN_LOGIN_INTERVAL": "1.5",
    "NITRIS_JOB_WORKERS": "15",
    "NITRIS_INTERACTIVE_WORKERS": "4",
    "REGISTRATION_MAX_CONCURRENT": "4",
    "QP_METADATA_MAX_CONCURRENT": "3",
    "JOB_MAX_RETRIES": "3",
    "JOB_RETRY_BASE_DELAY": "2.0",
    "SCHEDULER_MAX_QUEUE_DEPTH": "50",
    "DB_POOL_SIZE": "10",
    "DB_MAX_OVERFLOW": "20",
    "DEBUG": "",
}.items():
    os.environ[_k] = _v
