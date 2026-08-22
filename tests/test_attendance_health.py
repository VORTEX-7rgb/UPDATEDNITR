"""Debar Engine tests — pins NITRIS's official L-T-P table semantics exactly.

Reference (NITRIS NOTE panel): UA debars at >= limit; OA debars when > cap.
"""
from __future__ import annotations

import pytest

from app.services.attendance_health import (
    get_rule,
    normalize_ltp,
    skips_left_line,
    subject_health,
    summarize,
)


def rec(code="MN2105", name="Underground Coal Mining", ltp="3-1-0",
        tc=20, ua=2, le=0, oa=2, **kw) -> dict:
    base = dict(subject_code=code, subject_name=name, faculty="F",
                tc=str(tc), ua=str(ua), le=str(le), oa=str(oa), ltp=ltp)
    base.update(kw)
    return base


# ── LTP normalization + rule lookup ─────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("3-1-0", "3-1-0"),
    (" 3 - 1 - 0 ", "3-1-0"),
    ("3-1-0 ", "3-1-0"),
    ("L-T-P: 0-0-2", "0-0-2"),
    ("", None), (None, None), ("lab", None), ("10-20-30", None),
])
def test_normalize_ltp(raw, expected):
    assert normalize_ltp(raw) == expected


def test_every_screenshot_row_is_encoded():
    # The seven rows visible on the portal NOTE panel.
    expected = {
        "3-1-0": (17, 24), "3-0-0": (13, 18), "2-0-0": (9, 12),
        "0-0-3": (5, 6), "1-0-2": (5, 6), "0-0-2": (5, 6), "0-0-1": (5, 6),
    }
    for ltp, (ua_lim, oa_cap) in expected.items():
        rule = get_rule(ltp)
        assert rule is not None, ltp
        assert rule.ua_limit == ua_lim and rule.oa_cap == oa_cap, ltp


# ── Per-subject health ──────────────────────────────────────────────────────

def test_safe_zone():
    h = subject_health(rec(ua=2))
    assert h.level == "safe" and h.ua_left == 15 and h.oa_left == 22


def test_warn_at_half_budget():
    h = subject_health(rec(ltp="3-0-0", tc=10, ua=7, le=0, oa=7))  # 7/13 ≈ .54
    assert h.level == "warn"


def test_risk_at_eighty_percent():
    # 14/17 ≈ .82 AND ua_left=3 (above the danger<=2 band) -> pure risk tier.
    h = subject_health(rec(ltp="3-1-0", tc=20, ua=14, le=0, oa=14))
    assert h.level == "risk"


def test_danger_two_from_line():
    h = subject_health(rec(ltp="3-1-0", ua=15))  # left=2
    assert h.level == "danger" and h.ua_left == 2


def test_next_skip_debars_when_left_one():
    h = subject_health(rec(ltp="3-1-0", ua=16))  # 16 -> one more reaches 17
    assert h.level == "danger" and h.ua_left == 1


def test_debarred_on_ua_hitting_limit_exactly():
    # Portal says >= 17 => at exactly 17 you are debarred.
    h = subject_health(rec(ltp="3-1-0", ua=17))
    assert h.level == "debarred"


def test_debarred_on_oa_exceeding_cap():
    # OA cap 24 fires only ABOVE 24; exactly 24 is still alive.
    assert subject_health(rec(ltp="3-1-0", ua=5, le=19, oa=24)).level != "debarred"
    assert subject_health(rec(ltp="3-1-0", ua=5, le=20, oa=25)).level == "debarred"


def test_lab_five_skip_ladder():
    # Budget 5: the warn band (>=50%) overlaps danger (left<=2), so labs
    # jump straight from safe -> danger -> debarred. That IS the product truth.
    for ua, level in ((0, "safe"), (2, "safe"), (3, "danger"), (4, "danger"), (5, "debarred")):
        h = subject_health(rec(code="ER2271", name="Mining Geology Lab",
                               ltp="0-0-3", tc=4, ua=ua, le=0, oa=ua))
        assert h.level == level, f"UA={ua}"


def test_unknown_pattern_is_honest():
    h = subject_health(rec(ltp="4-0-0"))  # not on the published panel
    assert h.rule is None and h.level == "unknown"
    assert h.ua_left is None


def test_missing_ltp_field_old_snapshots():
    r = rec()
    del r["ltp"]
    h = subject_health(r)
    assert h.level == "unknown"


def test_no_classes_yet():
    h = subject_health(rec(code="EA2440", ltp="0-0-1", tc=0, ua=0, le=0, oa=0))
    assert h.level == "no_classes"


def test_string_coercion_padded_numbers():
    h = subject_health(rec(tc="05", ua=" 2 ", le="", oa=None))
    assert (h.tc, h.ua, h.le, h.oa) == (5, 2, 0, 0)


# ── Summary / riskiest ──────────────────────────────────────────────────────

def test_summarize_picks_worst_and_riskiest():
    records = [
        rec(code="MN2105", ltp="3-1-0", tc=20, ua=2, le=0, oa=2),      # safe (.12)
        rec(code="CS2011", ltp="2-0-0", tc=10, ua=5, le=0, oa=5),      # warn (.55)
        rec(code="ER2271", ltp="0-0-3", tc=4, ua=4, le=0, oa=4),       # danger (left=1)
        rec(code="EA2440", ltp="0-0-1", tc=0, ua=0, le=0, oa=0),       # no classes
    ]
    s = summarize(records)
    assert s.level == "danger"
    assert s.riskiest is not None and s.riskiest.code == "ER2271"
    assert s.counts.get("no_classes") == 1


def test_skips_left_lines():
    line = skips_left_line(rec(ua=2))
    assert line and "15 skip(s)" in line and "MN2105" in line  # 17 - 2
    hot = skips_left_line(rec(ltp="0-0-3", tc=4, ua=4, oa=4))
    assert hot and "NEXT SKIP" in hot.upper() or "1 skip(s)" in hot
    assert skips_left_line(rec(code="EA2440", ltp="0-0-1", tc=0, ua=0, le=0, oa=0)) is None
    assert skips_left_line(rec(ua=17)) is None  # already dead — no false hope


# ── Parser integration: L-T-P cell lands in records ─────────────────────────

def test_parser_captures_ltp_column():
    from app.nitris.parser import parse_attendance_html
    from app.nitris.constants import (
        ATTENDANCE_TABLE_ID as TID, STUDENT_INFO_LABEL_ID as SID,
    )
    html = f"""
    <html><body>
      <span id="{SID}">TEST STUDENT</span>
      <table id="{TID}">
        <tr>
          <th>#</th><th>Subject Code</th><th>Subject Name</th><th>L-T-P</th>
          <th>Credit</th><th>Section</th><th>Faculty</th><th>TC</th>
          <th>UA</th><th>LE</th><th>OA</th><th>View</th>
        </tr>
        <tr>
          <td>1</td><td>MN2105</td><td>Underground Coal Mining</td><td>3-1-0</td>
          <td>4</td><td>S1</td><td>Sahendra Ram</td><td>20</td>
          <td>2</td><td>0</td><td>2</td><td><a>Details</a></td>
        </tr>
      </table>
    </body></html>
    """
    res = parse_attendance_html(html)
    assert len(res.records) == 1
    r = res.records[0]
    assert r.ltp == "3-1-0"
    assert r.tc == "20" and r.ua == "2"
    h = subject_health(r.__dict__)
    assert h.level == "safe" and h.ua_left == 15
