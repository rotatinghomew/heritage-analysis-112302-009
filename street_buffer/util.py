"""共享的解析、周序和数值辅助函数。"""

from __future__ import annotations

import math
import re
from statistics import median
from typing import Iterable, Sequence


_WEEK_RE = re.compile(r"^\s*([A-Za-z一-鿿]*)\s*[-_]?\s*(\d+)\s*$")
_PERIOD_SPLIT_RE = re.compile(
    r"\s*(?:\.\.\.?|—|–|~|～|至|到|(?<=[A-Za-z0-9])\s*-\s*(?=[A-Za-z0-9]))\s*",
    re.IGNORECASE,
)


def blank(value: object) -> bool:
    return value is None or str(value).strip() == ""


def text(value: object) -> str:
    return "" if blank(value) else str(value).strip()


def as_float(value: object) -> float | None:
    s = text(value).replace(",", "").replace("￥", "").replace("¥", "").replace("$", "")
    if not s:
        return None
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def as_bool(value: object) -> bool | None:
    s = text(value).lower()
    if s in {"yes", "y", "true", "1", "是", "开", "营业", "open", "正常"}:
        return True
    if s in {"no", "n", "false", "0", "否", "关", "闭", "closed", "closure", "停业"}:
        return False
    return None


def week_key(week: str) -> tuple[str, int, str]:
    m = _WEEK_RE.match(week or "")
    if m:
        return (m.group(1).upper(), int(m.group(2)), week)
    return ("", 10**9, week)


def sort_weeks(weeks: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for w in weeks:
        w = text(w)
        if w and w not in seen:
            seen.add(w)
            out.append(w)
    return sorted(out, key=week_key)


def period_range(period: object) -> tuple[str | None, str | None]:
    """把 ``W1`` / ``W1-W3`` / ``W1 至 W3`` 解析成起止周。"""
    s = text(period)
    if not s:
        return None, None
    parts = [p for p in _PERIOD_SPLIT_RE.split(s) if p]
    if len(parts) >= 2:
        return parts[0].strip(), parts[1].strip()
    return s, s


def parse_ratio(value: object, default: float | None = None) -> float | None:
    s = text(value)
    if not s:
        return default
    if s.endswith("%"):
        num = as_float(s[:-1])
        return None if num is None else num / 100.0
    return as_float(s)


def money(value: float | None) -> float | None:
    if value is None:
        return None
    if not math.isfinite(value):
        return None
    return round(float(value), 2)


def mad(values: Sequence[float], center: float | None = None) -> float | None:
    vals = [v for v in values if v is not None and math.isfinite(v)]
    if not vals:
        return None
    c = median(vals) if center is None else center
    return median([abs(v - c) for v in vals])


def inclusive_weeks(start: str | None, end: str | None, universe: Sequence[str]) -> list[str]:
    if not start:
        return []
    if not end:
        end = start
    idx = {w: i for i, w in enumerate(universe)}
    if start not in idx or end not in idx:
        # 不在全局周序中的周仍按自然顺序夹取。
        lo, hi = sorted([start, end], key=week_key)
        return [w for w in universe if week_key(lo) <= week_key(w) <= week_key(hi)]
    a, b = idx[start], idx[end]
    if a > b:
        a, b = b, a
    return list(universe[a:b + 1])
