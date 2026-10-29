"""償却残価 (book value) calculation.

Management-accounting estimate following common Japanese practice for IT equipment.
Thresholds, useful life and fiscal-year start are parameters, not hard-coded truths:
confirm the treatment with your accounting team / tax advisor.

* 消耗品 (< 100,000 JPY)           expensed when use starts -> book value 0
* 少額減価償却資産 (SME, < 300,000)  expensed when use starts -> book value 0
* 一括償却資産 (100,000-199,999)    1/3 per fiscal year for 3 years, regardless of months
* 減価償却資産                      straight-line (定額法), monthly proration from the month
                                   use started, down to a 1-yen memorandum value (備忘価額)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .models import AssetItem

EXPENSE_LIMIT = 100_000
LUMP_SUM_LIMIT = 200_000
MEMO_VALUE = 1


def default_item(price: int) -> AssetItem:
    if price < EXPENSE_LIMIT:
        return AssetItem.EXPENSE
    if price < LUMP_SUM_LIMIT:
        return AssetItem.LUMP_SUM
    return AssetItem.DEPRECIABLE


def fiscal_year(d: date, start_month: int = 4) -> int:
    """FY label = calendar year in which the FY starts (Apr 2026 - Mar 2027 -> 2026)."""
    return d.year if d.month >= start_month else d.year - 1


def _months_inclusive(start: date, end: date) -> int:
    return (end.year - start.year) * 12 + (end.month - start.month) + 1


@dataclass(frozen=True)
class BookValue:
    item: AssetItem
    cost: int
    accumulated: int
    book_value: int
    fully_depreciated: bool


def book_value(
    price: int,
    usage_start: date | None,
    as_of: date,
    item: AssetItem | None = None,
    useful_life_years: int = 4,
    fy_start_month: int = 4,
) -> BookValue:
    item = item or default_item(price)
    if price <= 0:
        return BookValue(item, 0, 0, 0, True)
    if usage_start is None or as_of < usage_start:
        # not yet in service: nothing depreciated
        return BookValue(item, price, 0, price, False)

    if item in (AssetItem.EXPENSE, AssetItem.SME_SPECIAL):
        return BookValue(item, price, price, 0, True)

    if item is AssetItem.LUMP_SUM:
        years = fiscal_year(as_of, fy_start_month) - fiscal_year(usage_start, fy_start_month) + 1
        years = min(years, 3)
        accumulated = price if years >= 3 else price * years // 3
        return BookValue(item, price, accumulated, price - accumulated, years >= 3)

    # 定額法: annual = cost / life ; prorated per month in service
    months = _months_inclusive(usage_start, as_of)
    total_months = useful_life_years * 12
    accumulated = min(price - MEMO_VALUE, price * months // total_months)
    return BookValue(
        item, price, accumulated, price - accumulated, accumulated >= price - MEMO_VALUE
    )
