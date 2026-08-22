"""QP post-delivery navigation contract (user report fix).

After ANY paper outcome — delivered, not-available, in-progress,
permanent failure, or error — the terminal bubble MUST carry
Back-to-Papers + Dashboard buttons so the student is never stranded.
"""
import pytest

from app.bot.handlers.papers import _qp_nav_markup, _present_qp_result
from app.services.qpaper_service import QPResult


def test_nav_markup_has_both_targets():
    markup = _qp_nav_markup()
    datas = [btn.callback_data for row in markup.inline_keyboard for btn in row]
    assert "qp_back_subjects" in datas     # 📚 Back to Papers
    assert "inbox_back_dashboard" in datas  # 🏠 Dashboard


class FakeSurf:
    def __init__(self):
        self.calls = []

    async def final(self, text, markup=None):
        self.calls.append((text, markup))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "result",
    [
        QPResult(delivered=True),
        QPResult(delivered=True, file_kind="pdf"),
        QPResult(not_available=True),
        QPResult(in_progress=True),
        QPResult(permanent=True, error="exhausted"),
        QPResult(error="temporary"),
    ],
)
async def test_every_terminal_state_carries_navigation(result):
    surf = FakeSurf()
    await _present_qp_result(surf, result)

    assert len(surf.calls) == 1
    text, markup = surf.calls[0]
    assert text  # some human-readable receipt
    datas = [btn.callback_data for row in markup.inline_keyboard for btn in row]
    assert "qp_back_subjects" in datas
    assert "inbox_back_dashboard" in datas