"""Tripwire: EVERY lazy (function-local) ImportFrom in app/** must resolve.

This is the test that catches production landmines like:
    from app.nitris.parser import parse_question_papers_html   # name lives
                                                               # elsewhere!
Function-local imports never execute until a handler's specific code path
runs, so mocked unit tests sail past them — and the bot only explodes on the
server when a user taps the unlucky button. This scan executes at import level
on every test run instead.
"""
from __future__ import annotations

import ast
import importlib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
APP_DIR = REPO_ROOT / "app"


def _app_py_files():
    for p in APP_DIR.rglob("*.py"):
        if "__pycache__" in p.parts:
            continue
        yield p


def _iter_importfrom_targets(tree: ast.AST):
    """Yield (module_str, [names], lineno) for every absolute ImportFrom."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        if node.level != 0:  # relative imports — none expected under app/
            continue
        if not node.module or not node.module.startswith("app."):
            continue  # third-party / stdlib resolved implicitly by imports
        yield node.module, [a.name for a in node.names], node.lineno


def test_every_lazy_import_name_resolves():
    failures: list[str] = []

    checked = 0
    for py in _app_py_files():
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        for module, names, lineno in _iter_importfrom_targets(tree):
            try:
                mod = importlib.import_module(module)
            except Exception as exc:  # pragma: no cover - surfaces real bugs
                failures.append(
                    f"{py.relative_to(REPO_ROOT)}:{lineno} cannot import module "
                    f"'{module}': {exc!r}"
                )
                continue
            for name in names:
                if name == "*":
                    continue
                checked += 1
                if not hasattr(mod, name):
                    failures.append(
                        f"{py.relative_to(REPO_ROOT)}:{lineno} "
                        f"cannot import name '{name}' from '{module}'"
                    )

    assert checked > 200, f"scan suspiciously small ({checked}) — glob broken?"
    assert not failures, (
        f"{len(failures)} broken lazy import(s):\n" + "\n".join(failures)
    )
