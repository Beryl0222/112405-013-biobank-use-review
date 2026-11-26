"""日期解析。

合成资料中部分原文没有 ISO 日期（如"八月五日"），解析为绝对日期时必须
标记 ``inferred=True``，让后续规则按保守口径处理并在视图中暴露。
"""
from __future__ import annotations

import re
from datetime import date

from .model import FullDate

_ISO_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
_CN_DATE_RE = re.compile(r"(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]")
_CN_YEAR_MONTH_RE = re.compile(r"(\d{4})\s*年\s*(\d{1,2})\s*月")
_ADULT_PLUS_RE = re.compile(r"成年后\s*(\d+|[一二三四五六七八九十]+)\s*年")
_CN_NUMERALS = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
                "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def today() -> FullDate:
    t = date.today()
    return FullDate(t.isoformat())


def parse_iso(text: str) -> FullDate | None:
    m = _ISO_RE.search(text)
    if not m:
        return None
    y, mo, d = (int(x) for x in m.groups())
    return FullDate(f"{y:04d}-{mo:02d}-{d:02d}")


def parse_cn_date(text: str, default_year: int) -> FullDate | None:
    """解析 '八月五日' 这类无年份日期，年份取自上下文并标记推定。"""
    m = _CN_DATE_RE.search(text)
    if not m:
        return None
    mo, d = (int(x) for x in m.groups())
    return FullDate(f"{default_year:04d}-{mo:02d}-{d:02d}", inferred=True)


def first_date(text: str, default_year: int) -> FullDate | None:
    return parse_iso(text) or parse_cn_date(text, default_year)


def adult_plus_years(adult_on: FullDate, text: str) -> FullDate | None:
    """处理'代签有效至成年后一年'类表述。"""
    m = _ADULT_PLUS_RE.search(text)
    if not m:
        return None
    token = m.group(1)
    years = int(token) if token.isdigit() else _CN_NUMERALS.get(token, 0)
    if years == 0:
        return None
    y, mo, d = adult_on.key()
    try:
        return FullDate(date(y + years, mo, d).isoformat())
    except ValueError:
        return FullDate(date(y + years, mo, d - 1).isoformat())


def is_before(a: FullDate | None, b: FullDate) -> bool:
    """a 早于 b；a 未知时保守返回 False（不臆造先后）。"""
    return a is not None and a < b


def is_after(a: FullDate | None, b: FullDate) -> bool:
    return a is not None and b < a


def is_within(day: FullDate, start: FullDate | None, end: FullDate | None) -> bool:
    if start is not None and day < start:
        return False
    if end is not None and end < day:
        return False
    return True
