"""Tests for the undergraduate-only programme support gate in registration."""

import ast
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("ENCRYPTION_KEY", "ZmDfcTF7_60GrrY167zsiPd67pEvs0aGOv2oasOM1Pg=")
os.environ.setdefault("BOT_TOKEN", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost:5432/test")

from app.bot.fsm import Registration
from app.bot.handlers.registration import (
    SUPPORTED_PROGRAMME_DIGITS,
    process_roll,
)
from app.ui.copy import POSTGRAD_UNSUPPORTED_NOTICE


REGISTRATION_PATH = (
    Path(__file__).resolve().parents[1] / "app" / "bot" / "handlers" / "registration.py"
)


def _make_msg_and_state(roll_text: str):
    message = MagicMock()
    message.text = roll_text
    message.answer = AsyncMock()

    state = MagicMock()
    state.update_data = AsyncMock()
    state.set_state = AsyncMock()

    return message, state


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "valid_roll",
    [
        "725MN1011",
        "125MM0058",
        "125EC0063",
        "123AI0001",
        "525CS2001",
        "625ME3001",
        "725MN4011",
    ],
)
async def test_undergrad_rolls_pass_through_gate(valid_roll: str):
    """Undergrad rolls ('0','1','2','3','4' at index 5) must pass gate and prompt for password."""
    message, state = _make_msg_and_state(valid_roll)

    await process_roll(message, state)

    # State updated with roll
    state.update_data.assert_awaited_once_with(roll=valid_roll)
    # Transitions to waiting_for_password
    state.set_state.assert_awaited_once_with(Registration.waiting_for_password)
    # Sends accepted message
    message.answer.assert_awaited_once()
    assert "Roll Number Accepted" in message.answer.call_args[0][0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "postgrad_roll",
    [
        "225ME9002",
        "225ME7001",
        "225ME8001",
        "525CH5001",
        "625PH6001",
    ],
)
async def test_postgrad_rolls_soft_blocked(postgrad_roll: str):
    """Postgrad rolls ('5','6','7','8','9' at index 5) must be soft-blocked without state transition."""
    message, state = _make_msg_and_state(postgrad_roll)

    await process_roll(message, state)

    # State NOT updated
    state.update_data.assert_not_called()
    # State NOT advanced
    state.set_state.assert_not_called()
    # Sent friendly notice
    message.answer.assert_awaited_once()
    sent_text = message.answer.call_args[0][0]
    assert "Hey! We're not ready for your programme just yet" in sent_text
    assert postgrad_roll in sent_text


@pytest.mark.asyncio
async def test_postgrad_block_does_not_touch_nitris():
    """Zero NITRIS gateway or client calls on a soft-blocked roll."""
    message, state = _make_msg_and_state("225ME9002")

    with patch("app.nitris.gateway.nitris_gateway.acquire") as mock_gw, \
         patch("app.nitris.client.NitrisClient") as mock_client:
        await process_roll(message, state)
        mock_gw.assert_not_called()
        mock_client.assert_not_called()


@pytest.mark.asyncio
async def test_retry_after_block():
    """A user who gets blocked can immediately retry with a valid roll."""
    # First attempt: postgrad roll (blocked)
    msg1, state = _make_msg_and_state("225ME9002")
    await process_roll(msg1, state)
    assert state.update_data.call_count == 0

    # Second attempt: valid BTech roll (passes)
    msg2, _ = _make_msg_and_state("125FP0024")
    await process_roll(msg2, state)
    state.update_data.assert_awaited_once_with(roll="125FP0024")
    state.set_state.assert_awaited_once_with(Registration.waiting_for_password)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_input",
    [
        "225ME9OO2",  # Letter O
        "22ME9002",   # 8 chars
        "2255ME9002", # 10 chars
        "ABC123456",  # Bad prefix
        "125990001",  # No dept code
        "",           # Empty
        "   ",        # Spaces
    ],
)
async def test_invalid_format_still_rejected_before_gate(invalid_input: str):
    """Regex format check must reject invalid syntax before the programme gate runs."""
    message, state = _make_msg_and_state(invalid_input)

    await process_roll(message, state)

    state.update_data.assert_not_called()
    state.set_state.assert_not_called()
    message.answer.assert_awaited_once()
    assert "Invalid Roll Number format" in message.answer.call_args[0][0]


def test_gate_placed_before_state_update_in_source():
    """AST guard: programme gate check must be placed before state.update_data in process_roll."""
    tree = ast.parse(REGISTRATION_PATH.read_text(encoding="utf-8"))

    process_roll_node = None
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "process_roll":
            process_roll_node = node
            break

    assert process_roll_node is not None, "process_roll function not found"

    gate_line = None
    update_data_line = None

    for node in ast.walk(process_roll_node):
        if isinstance(node, ast.If):
            # Check for roll[5] in test condition
            test_src = ast.unparse(node.test)
            if "SUPPORTED_PROGRAMME_DIGITS" in test_src:
                gate_line = node.lineno
        elif isinstance(node, ast.Call):
            call_src = ast.unparse(node.func)
            if "state.update_data" in call_src:
                update_data_line = node.lineno

    assert gate_line is not None, "Programme gate check not found in process_roll"
    assert update_data_line is not None, "state.update_data call not found in process_roll"
    assert gate_line < update_data_line, f"Gate ({gate_line}) must be before update_data ({update_data_line})"


def test_supported_digits_constant_present_and_conservative():
    """Guard: SUPPORTED_PROGRAMME_DIGITS must be exactly {'0','1','2','3','4'}."""
    assert SUPPORTED_PROGRAMME_DIGITS == frozenset({"0", "1", "2", "3", "4"})
