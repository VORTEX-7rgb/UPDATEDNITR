"""Unit tests for holidays service serialization and orchestration."""
import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from app.nitris.holidays_parser import HolidayEntry, HolidaysPage
from app.services.holidays_service import (
    serialize_holidays_page,
    deserialize_holidays_page,
    fetch_user_holidays,
    get_cached_user_page,
    _user_calendar_pages,
)


def test_serialize_deserialize_holidays_page():
    page = HolidaysPage(
        month=9,
        year=2026,
        month_label="September 2026",
        holidays=[
            HolidayEntry(day=4, name="Janmashtami", month=9, year=2026, is_trailing=False),
            HolidayEntry(day=2, name="Mahatma Gandhis Birthday", month=9, year=2026, is_trailing=True),
        ],
        prev_event_argument="V9709",
        next_event_argument="V9770",
        raw_html="<html><body>Calendar</body></html>",
    )

    data = serialize_holidays_page(page)
    assert data["month"] == 9
    assert data["year"] == 2026
    assert data["month_label"] == "September 2026"
    assert len(data["holidays"]) == 2
    assert data["prev_event_argument"] == "V9709"
    assert data["next_event_argument"] == "V9770"

    deserialized = deserialize_holidays_page(data)
    assert deserialized.month == page.month
    assert deserialized.year == page.year
    assert deserialized.month_label == page.month_label
    assert len(deserialized.holidays) == 2
    assert deserialized.holidays[0] == page.holidays[0]
    assert deserialized.holidays[1] == page.holidays[1]
    assert deserialized.prev_event_argument == page.prev_event_argument
    assert deserialized.next_event_argument == page.next_event_argument
    assert deserialized.raw_html == page.raw_html


@pytest.mark.asyncio
async def test_fetch_user_holidays_user_not_found():
    with patch("app.services.holidays_service.async_session_factory") as mock_factory:
        mock_session = AsyncMock()
        mock_factory.return_value.__aenter__.return_value = mock_session
        mock_session.get.return_value = None

        result = await fetch_user_holidays(user_id=999)
        assert not result["success"]
        assert "not found" in result["error"].lower()


@pytest.mark.asyncio
async def test_fetch_user_holidays_invalid_credentials():
    mock_user = MagicMock()
    mock_user.credentials_valid = False

    with patch("app.services.holidays_service.async_session_factory") as mock_factory:
        mock_session = AsyncMock()
        mock_factory.return_value.__aenter__.return_value = mock_session
        mock_session.get.return_value = mock_user

        result = await fetch_user_holidays(user_id=1)
        assert not result["success"]
        assert "invalid" in result["error"].lower()


@pytest.mark.asyncio
async def test_fetch_user_holidays_success():
    mock_user = MagicMock()
    mock_user.id = 1
    mock_user.roll_number = "123CS0001"
    mock_user.encrypted_password = b"secret"
    mock_user.credentials_valid = True

    mock_page = HolidaysPage(
        month=12,
        year=2099,
        month_label="December 2099",
        holidays=[
            HolidayEntry(day=25, name="Christmas", month=12, year=2099, is_trailing=False),
        ],
        prev_event_argument="V9999",
        next_event_argument="V10000",
        raw_html="<html>test</html>",
    )

    with patch("app.services.holidays_service.async_session_factory") as mock_factory, \
         patch("app.services.holidays_service._save_disk_cache"), \
         patch("app.services.holidays_service.with_pooled_session", new_callable=AsyncMock) as mock_pool:
        mock_session = AsyncMock()
        mock_factory.return_value.__aenter__.return_value = mock_session
        mock_session.get.return_value = mock_user
        mock_pool.return_value = mock_page

        result = await fetch_user_holidays(user_id=1, force_refresh=True)

        assert result["success"]
        assert result["month_label"] == "December 2099"
        assert result["prev_available"] is True
        assert result["next_available"] is True
        assert len(result["holidays"]) == 1
        assert result["holidays"][0]["name"] == "Christmas"
        assert get_cached_user_page(1) == mock_page

        # Second call with another user should HIT the global cache without calling mock_pool again!
        mock_pool.reset_mock()
        second_result = await fetch_user_holidays(user_id=2, current_page=mock_page, direction="next")
        # Next from Dec 2099 is Jan 2100 which isn't cached, but if direction is None:
        second_result = await fetch_user_holidays(user_id=2, force_refresh=False)
        assert second_result["success"]


