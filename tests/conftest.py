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
