"""NITR Debar Engine — pure math over scraped attendance records.

SOURCE OF TRUTH
===============
NITRIS's own ClassAttendance.aspx NOTE panel (captured 2026-08-22):

    "A penalty on grade based on the LTP for absence in classes."
    L-T-P | Debarred on Unauthorised Absence | Debarred on Total Absence
    3-1-0 | >= 17                             | > 24
    3-0-0 | >= 13                             | > 18
    0-0-3 | >= 5                              | > 6
    2-0-0 | >= 9                              | > 12
    1-0-2 | >= 5                              | > 6
    0-0-2 | >= 5                              | > 6
    0-0-1 | >= 5                              | > 6

    TC = classes held · UA = unauthorised skip · LE = approved leave
    OA (AB+LE) = total missed = LE + UA

SEMANTICS (exact — do not "simplify"):
  * UA debar fires when UA >= ua_limit      -> max safe UA = ua_limit - 1
  * OA debar fires when OA >  oa_cap        -> max safe OA = oa_cap

This module is PURE: dict-in, dataclass-out. No DB, no Telegram, no portal.
Inputs are snapshot_json["records"] dicts whose values arrive as strings.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Optional


# ── The official table ──────────────────────────────────────────────────────

@dataclass(frozen=True)
class DebarRule:
    ltp: str
    ua_limit: int   # debarred when UA >= this
    oa_cap: int     # debarred when OA >  this


DEBAR_RULES: dict[str, DebarRule] = {
    rule.ltp: rule
    for rule in (
        DebarRule("3-1-0", ua_limit=17, oa_cap=24),
        DebarRule("3-0-0", ua_limit=13, oa_cap=18),
        DebarRule("2-0-0", ua_limit=9,  oa_cap=12),
        DebarRule("0-0-3", ua_limit=5,  oa_cap=6),
        DebarRule("1-0-2", ua_limit=5,  oa_cap=6),
        DebarRule("0-0-2", ua_limit=5,  oa_cap=6),
        DebarRule("0-0-1", ua_limit=5,  oa_cap=6),
    )
}

_LTP_RE = re.compile(r"(\d+)\s*-\s*(\d+)\s*-\s*(\d+)")


def normalize_ltp(raw: str | None) -> Optional[str]:
    """'3 -1- 0' / '3-1-0 ' -> '3-1-0'; anything unparseable -> None."""
    if not raw:
        return None
    m = _LTP_RE.search(str(raw))
    if not m:
        return None
    a, b, c = m.group(1), m.group(2), m.group(3)
    if not all(len(x) == 1 for x in (a, b, c)):
        return None  # NITR patterns are single-digit components
    return f"{a}-{b}-{c}"


def get_rule(ltp_raw: str | None) -> Optional[DebarRule]:
    key = normalize_ltp(ltp_raw)
    return DEBAR_RULES.get(key) if key else None


def _to_int(val) -> int:
    """Snapshot values arrive as strings ('', '05', '-', None...)."""
    if val is None:
        return 0
    s = str(val).strip()
    if not s:
        return 0
    try:
        return int(s)
    except ValueError:
        digits = "".join(ch for ch in s if ch.isdigit())
        return int(digits) if digits else 0


# ── Levels ──────────────────────────────────────────────────────────────────

LEVEL_EMOJI = {
    "no_classes": "⚪",
    "unknown":    "❔",
    "safe":       "🟢",
    "warn":       "🟡",
    "risk":       "🟠",
    "danger":     "🔴",
    "debarred":   "💀",
}
_LEVEL_ORDER = ["debarred", "danger", "risk", "warn", "safe", "unknown", "no_classes"]

WARN_RATIO = 0.5    # half the skip budget gone
RISK_RATIO = 0.8    # grade-penalty territory per NITRIS's warning header
DANGER_LEFT = 2     # two skips from the debar line


# ── Per-subject health ──────────────────────────────────────────────────────

@dataclass
class SubjectHealth:
    code: str
    name: str
    faculty: str
    ltp: str                 # normalized pattern ('' when unknown)
    tc: int
    ua: int
    le: int
    oa: int
    rule: Optional[DebarRule]
    level: str               # key into LEVEL_EMOJI
    # Both counts mean "how many MORE you can take before the axe":
    #   ua_left = ua_limit - ua   (hitting 0 => next skip IS the debar value)
    #   oa_left = oa_cap  - oa    (OA may equal cap; cap+1 debars)
    ua_left: Optional[int] = None
    oa_left: Optional[int] = None
    used_ratio: Optional[float] = None  # ua / ua_limit

    @property
    def emoji(self) -> str:
        return LEVEL_EMOJI.get(self.level, "❔")


def subject_health(record: dict) -> SubjectHealth:
    code = str(record.get("subject_code") or "?")
    name = str(record.get("subject_name") or "")
    faculty = str(record.get("faculty") or "")
    raw_ltp = record.get("ltp")
    ltp = normalize_ltp(raw_ltp) or ""
    rule = get_rule(raw_ltp)

    tc = _to_int(record.get("tc"))
    ua = _to_int(record.get("ua"))
    le = _to_int(record.get("le"))
    oa = _to_int(record.get("oa"))

    h = SubjectHealth(
        code=code, name=name, faculty=faculty, ltp=ltp,
        tc=tc, ua=ua, le=le, oa=oa, rule=rule, level="safe",
    )

    # Semester hasn't started for this course yet (e.g. EA2440 rows).
    if tc == 0 and ua == 0 and le == 0 and oa == 0:
        h.level = "no_classes"
        return h

    if rule is None:
        h.level = "unknown"
        return h

    h.ua_left = rule.ua_limit - ua
    h.oa_left = rule.oa_cap - oa
    h.used_ratio = min(1.0, ua / rule.ua_limit) if rule.ua_limit else 0.0

    # Exact NITRIS semantics: UA >= limit debars; OA > cap debars.
    if ua >= rule.ua_limit or oa > rule.oa_cap:
        h.level = "debarred"
    elif h.ua_left <= DANGER_LEFT or h.oa_left <= 0:
        h.level = "danger"
    elif h.used_ratio >= RISK_RATIO:
        h.level = "risk"
    elif h.used_ratio >= WARN_RATIO:
        h.level = "warn"
    else:
        h.level = "safe"

    return h


# ── Overall summary ─────────────────────────────────────────────────────────

@dataclass
class AttendanceSummary:
    subjects: list[SubjectHealth] = field(default_factory=list)
    level: str = "no_classes"          # worst tracked level
    riskiest: Optional[SubjectHealth] = None  # highest budget burn, alive only
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def emoji(self) -> str:
        return LEVEL_EMOJI.get(self.level, "❔")

    @property
    def has_tracking(self) -> bool:
        return any(s.rule for s in self.subjects)


def summarize(records: Iterable[dict]) -> AttendanceSummary:
    subjects = [subject_health(r) for r in records or []]

    counts: dict[str, int] = {}
    for s in subjects:
        counts[s.level] = counts.get(s.level, 0) + 1

    worst_level = "safe"
    ranked = [lv for lv in _LEVEL_ORDER if counts.get(lv)]
    if ranked:
        worst_level = ranked[0]

    alive = [
        s for s in subjects
        if s.rule is not None and s.level != "no_classes"
    ]
    riskiest = max(alive, key=lambda s: (s.used_ratio or 0.0, -(s.ua_left or 0))) \
        if alive else None

    return AttendanceSummary(
        subjects=subjects,
        level=worst_level if subjects else "no_classes",
        riskiest=riskiest,
        counts=counts,
    )


# ── One-liner for notifications (used by the event dispatcher) ──────────────

def skips_left_line(record: dict) -> Optional[str]:
    """Human line for absence alerts: 'N more skips until debar'. None if we
    can't compute honestly (unknown pattern / no classes / already dead)."""
    h = subject_health(record)
    if h.rule is None or h.level in ("no_classes", "debarred"):
        return None
    if h.ua_left <= 0:
        return (
            f"💀 <b>NEXT SKIP = DEBARRED</b> from {h.code} "
            f"(limit {h.rule.ua_limit}). Do NOT miss this class."
        )
    tone = "🚨" if h.level == "danger" else "📊"
    return (
        f"{tone} {h.code}: {h.ua_left} skip(s) left before the debar line "
        f"(UA limit {h.rule.ua_limit})."
    )
