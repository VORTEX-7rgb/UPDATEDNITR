"""Verify lease boundary invariants for timetable sync."""
from __future__ import annotations

import ast
from pathlib import Path
import pytest


def test_timetable_service_lease_boundary():
    """Verify that timetable_service performs DB replace outside gateway.acquire()."""
    service_path = Path("app/services/timetable_service.py")
    tree = ast.parse(service_path.read_text(encoding="utf-8"))

    # Ensure fetch_timetable_html_via_gateway uses nitris_gateway.acquire
    # but does NOT create database sessions inside it
    func_defs = [node for node in ast.walk(tree) if isinstance(node, ast.AsyncFunctionDef)]
    fetch_func = next((f for f in func_defs if f.name == "fetch_timetable_html_via_gateway"), None)
    assert fetch_func is not None

    sync_func = next((f for f in func_defs if f.name == "sync_user_timetable"), None)
    assert sync_func is not None

    # sync_user_timetable should NOT wrap the whole flow in gateway.acquire()
    # It must call fetch_timetable_html_via_gateway, then get_db_session() separately.
    with_stmts = [node for node in ast.walk(sync_func) if isinstance(node, ast.AsyncWith)]
    for w in with_stmts:
        for item in w.items:
            # Check context expression name
            if isinstance(item.context_expr, ast.Call):
                func_name = getattr(item.context_expr.func, "id", None) or getattr(item.context_expr.func, "attr", None)
                if func_name == "acquire":
                    pytest.fail("sync_user_timetable should not directly call gateway.acquire() — lease boundary broken!")
