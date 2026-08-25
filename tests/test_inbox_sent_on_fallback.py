"""Guard: a NITRIS notice with an unparseable/sentinel `sent_on` date must
never become invisible in /inbox.

`_parse_inbox_sent_on()` (app/nitris/parser.py) deterministically falls back
to a fixed sentinel (2000-01-01) when NITRIS renders a date string the parser
doesn't recognize (e.g. a relative label like "Today" for same-day notices on
the notification dropdown). That fallback is intentional and must stay
deterministic — see the function's own docstring on why it can never use
datetime.now(): unstable timestamps would churn _content_portal_id hashes and
re-insert duplicate messages / re-notify on every sync.

The storage side must therefore never trust raw `sent_on` for recency
ordering: a sentinel-dated message used to sink below every real message in
ORDER BY sent_on DESC and vanish past the last page of /inbox forever, even
though it was the most recently synced message and had already been
push-notified to the user (incident 2026-08-25: same-day "Hazard and Rescue
Lab" notice from Charan Kumar Ala never appeared in /inbox's top 5 despite a
live push notification minutes earlier).

InboxRepository now orders by an effective-recency key — portal `sent_on`
when parseable, else row `created_at` (insertion time, immune to upstream
date-parse failures). These tests pin both halves:

  1. The parser fallback stays deterministic AND is logged (so the next
     unrecognized format is diagnosable from VM logs instead of silently
     vanishing).
  2. The inbox listing queries sort by the guarded recency expression, not
     bare `sent_on` — verified against the REAL compiled SQL statement.
"""
import ast
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("ENCRYPTION_KEY", "ZmDfcTF7_60GrrY167zsiPd67pEvs0aGOv2oasOM1Pg=")
os.environ.setdefault("BOT_TOKEN", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost:5432/test")

REPO_ROOT = Path(__file__).resolve().parents[1]
PARSER_PATH = REPO_ROOT / "app" / "nitris" / "parser.py"
INBOX_REPO_PATH = REPO_ROOT / "app" / "db" / "repositories" / "inbox_repository.py"


# ── Parser half ──────────────────────────────────────────────────────────────

def test_unparseable_date_still_returns_deterministic_sentinel():
    """The fallback behavior itself must not change — same broken input must
    always return the same sentinel, or portal_message_id hashing for
    historical (token-less) messages would become unstable and spam
    duplicate notifications on every sync."""
    from app.nitris.parser import _parse_inbox_sent_on
    from datetime import datetime

    for bad_input in ("", "   ", "Today", "not-a-real-date"):
        first = _parse_inbox_sent_on(bad_input)
        second = _parse_inbox_sent_on(bad_input)
        assert first == second == datetime(2000, 1, 1, 0, 0, 0)


def test_unparseable_date_is_logged_not_silent(caplog):
    """A date string that falls through every known format must emit a
    warning carrying the raw string, so the next occurrence is diagnosable
    from logs instead of a silent, unexplained sentinel."""
    from app.nitris.parser import _parse_inbox_sent_on

    with caplog.at_level(logging.WARNING, logger="app.nitris.parser"):
        _parse_inbox_sent_on("Today")

    assert any("Today" in rec.message for rec in caplog.records), (
        "Unparseable date strings must be logged with the raw value — "
        "silently returning the sentinel makes future occurrences "
        "undiagnosable (this is exactly what happened on 2026-08-25)."
    )


def test_blank_date_also_logged(caplog):
    """The empty/blank branch must log too — it is the likeliest trigger in
    production (portal dropdown rendering an empty time span)."""
    from app.nitris.parser import _parse_inbox_sent_on

    with caplog.at_level(logging.WARNING, logger="app.nitris.parser"):
        _parse_inbox_sent_on("   ")

    assert len(caplog.records) >= 1


# ── Repository half ──────────────────────────────────────────────────────────

class _CaptureSession:
    """Minimal AsyncSession stand-in that records the statement handed to
    execute() and returns an empty result — lets us compile and inspect the
    REAL query built by InboxRepository without a database."""

    def __init__(self):
        self.captured = None

    async def execute(self, stmt):
        self.captured = stmt

        class _Result:
            def scalars(self):
                return self

            def all(self):
                return []

        return _Result()


def _find_function(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def _order_by_arg_renders(func_node):
    renders = []
    for call in ast.walk(func_node):
        if (
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and call.func.attr == "order_by"
        ):
            for arg in call.args:
                renders.append(ast.unparse(arg))
    return renders


def test_inbox_listing_queries_use_guarded_recency_key():
    """get_latest_messages and search_messages must order by the sentinel-
    guarded recency expression (_effective_recency), never by bare
    InboxMessage.sent_on. Bare sent_on ordering is exactly what buried the
    incident message past the last page; id-only ordering is rejected here
    too because it would reverse page-1 order for freshly-backfilled users
    whenever the portal lists notices newest-first."""
    tree = ast.parse(INBOX_REPO_PATH.read_text(encoding="utf-8"))

    offenders = []
    for fn_name in ("get_latest_messages", "search_messages"):
        node = _find_function(tree, fn_name)
        assert node is not None, f"{fn_name} missing from inbox_repository.py"
        renders = _order_by_arg_renders(node)
        assert renders, f"{fn_name} has no ORDER BY"
        if not any("_effective_recency" in r for r in renders):
            offenders.append(f"{fn_name}: does not sort by _effective_recency: {renders}")
        for rendered in renders:
            if "sent_on" in rendered:
                offenders.append(f"{fn_name}: bare portal-date sort key: {rendered}")

    # The helper itself must actually implement the guard: case(sent_on -> created_at).
    helper = _find_function(tree, "_effective_recency")
    assert helper is not None, "_effective_recency helper missing"
    helper_src = ast.unparse(helper)
    for required in ("case(", "sent_on", "created_at"):
        assert required in helper_src, (
            f"_effective_recency lost its sentinel guard ({required!r} missing): "
            f"a parse failure would bury the newest message again.\n{helper_src}"
        )

    assert not offenders, f"Inbox recency ordering regressed: {offenders}"


async def test_get_latest_messages_compiles_guarded_sql():
    """Behavioral check: the REAL query built by get_latest_messages must
    compile to SQL containing the CASE guard and the id tiebreaker — this
    catches runtime regressions (wrong column, dropped case()) that source
    greps cannot."""
    from app.db.repositories.inbox_repository import InboxRepository

    session = _CaptureSession()
    await InboxRepository(session).get_latest_messages(user_id=42, offset=0, limit=5)

    sql = str(session.captured)
    assert "CASE" in sql.upper(), f"expected CASE guard in compiled SQL:\n{sql}"
    assert "created_at" in sql.lower(), f"sentinel fallback column missing:\n{sql}"
    assert "sent_on" in sql.lower(), f"well-dated portal date key missing:\n{sql}"
    assert "id desc" in sql.lower(), f"id tiebreaker missing:\n{sql}"


async def test_search_messages_compiles_guarded_sql():
    """Same contract for search results — a sentinel-dated notice must still
    be findable at the top of search instead of ranked last."""
    from app.db.repositories.inbox_repository import InboxRepository

    session = _CaptureSession()
    await InboxRepository(session).search_messages(user_id=42, query="hazard", limit=5)

    sql = str(session.captured)
    assert "CASE" in sql.upper(), f"expected CASE guard in compiled SQL:\n{sql}"
    assert "created_at" in sql.lower(), f"sentinel fallback column missing:\n{sql}"
