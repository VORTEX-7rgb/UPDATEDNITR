"""AST check to ensure no bare datetime.now() calls exist in timetable modules."""
from __future__ import annotations

import ast
from pathlib import Path
import pytest

TIMETABLE_MODULES = [
    Path("app/services/now_next_service.py"),
    Path("app/services/timetable_service.py"),
    Path("app/db/repositories/timetable_repository.py"),
    Path("app/bot/handlers/timetable.py"),
]


@pytest.mark.parametrize("file_path", TIMETABLE_MODULES)
def test_no_naive_datetime_now_in_timetable(file_path: Path):
    """Enforce that all datetime.now() calls in timetable code pass an explicit tz argument."""
    if not file_path.exists():
        pytest.skip(f"{file_path} does not exist")

    tree = ast.parse(file_path.read_text(encoding="utf-8"))

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            # Check for datetime.now() or now()
            if isinstance(node.func, ast.Attribute) and node.func.attr == "now":
                # Check arguments
                if len(node.args) == 0 and len(node.keywords) == 0:
                    pytest.fail(
                        f"Found bare datetime.now() without tz in {file_path} at line {node.lineno}! "
                        f"Always use datetime.now(IST) or datetime.now(timezone.utc)."
                    )
