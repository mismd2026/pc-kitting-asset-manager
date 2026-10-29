from datetime import date

import pytest

from assetdb.depreciation import book_value, default_item, fiscal_year
from assetdb.models import AssetItem


@pytest.mark.parametrize(
    "price,item",
    [
        (99_999, AssetItem.EXPENSE),
        (100_000, AssetItem.LUMP_SUM),
        (199_999, AssetItem.LUMP_SUM),
        (200_000, AssetItem.DEPRECIABLE),
    ],
)
def test_default_item_thresholds(price, item):
    assert default_item(price) is item


def test_fiscal_year_starts_in_april():
    assert fiscal_year(date(2026, 3, 31)) == 2025
    assert fiscal_year(date(2026, 4, 1)) == 2026
    assert fiscal_year(date(2026, 12, 1), start_month=1) == 2026


def test_not_in_service_keeps_full_value():
    bv = book_value(240_000, None, date(2026, 10, 30))
    assert bv.book_value == 240_000 and bv.accumulated == 0


def test_straight_line_monthly_proration():
    # 240,000 over 4 years = 5,000/month. Apr..Oct inclusive = 7 months.
    bv = book_value(240_000, date(2026, 4, 15), date(2026, 10, 30))
    assert bv.item is AssetItem.DEPRECIABLE
    assert bv.accumulated == 35_000
    assert bv.book_value == 205_000


def test_straight_line_stops_at_memorandum_value():
    bv = book_value(240_000, date(2020, 4, 1), date(2026, 10, 30))
    assert bv.book_value == 1  # 備忘価額
    assert bv.fully_depreciated


def test_lump_sum_one_third_per_fiscal_year_regardless_of_month():
    start = date(2027, 3, 1)  # last month of FY2026 still counts as a full third
    assert book_value(150_000, start, date(2027, 3, 31)).book_value == 100_000
    assert book_value(150_000, start, date(2027, 4, 1)).book_value == 50_000
    assert book_value(150_000, start, date(2029, 4, 1)).book_value == 0


@pytest.mark.parametrize("item", [AssetItem.EXPENSE, AssetItem.SME_SPECIAL])
def test_expensed_items_have_no_book_value(item):
    assert book_value(250_000, date(2026, 5, 1), date(2026, 5, 2), item).book_value == 0
