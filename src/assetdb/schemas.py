from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field

from .depreciation import book_value
from .models import Asset, AssetItem, AssetStatus


def add_months(d: date, months: int) -> date:
    y, m = divmod(d.month - 1 + months, 12)
    year, month = d.year + y, m + 1
    for day in (d.day, 30, 29, 28):  # clamp e.g. Jan 31 + 1 month
        try:
            return date(year, month, day)
        except ValueError:
            continue
    raise ValueError(d)


class AssetIn(BaseModel):
    asset_tag: str | None = Field(None, max_length=40)
    serial_number: str = Field(min_length=1, max_length=100)
    budget_code: str | None = None
    vendor: str = ""
    order_date: date | None = None
    delivery_date: date | None = None
    price: int = Field(0, ge=0)
    category1: str = ""
    category2: str = ""
    category3: str = ""
    category4: str = ""
    category5: str = ""
    part_number: str = ""
    model_name: str = ""
    asset_item: AssetItem | None = None
    useful_life_years: int = Field(4, ge=1, le=20)
    warranty_months: int = Field(36, ge=0, le=120)
    kitting_pattern: str = ""
    notes: str = ""


class AssetPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    budget_code: str | None = None
    vendor: str | None = None
    order_date: date | None = None
    delivery_date: date | None = None
    price: int | None = Field(None, ge=0)
    category1: str | None = None
    category2: str | None = None
    category3: str | None = None
    category4: str | None = None
    category5: str | None = None
    part_number: str | None = None
    model_name: str | None = None
    asset_item: AssetItem | None = None
    useful_life_years: int | None = Field(None, ge=1, le=20)
    warranty_months: int | None = Field(None, ge=0, le=120)
    kitting_pattern: str | None = None
    hostname: str | None = Field(None, max_length=63)
    rdp_port: int | None = Field(None, ge=1, le=65535)
    notes: str | None = None


class AssetOut(BaseModel):
    id: int
    asset_tag: str
    status: AssetStatus
    budget_code: str | None
    vendor: str
    order_date: date | None
    delivery_date: date | None
    price: int
    categories: list[str]
    part_number: str
    serial_number: str
    model_name: str
    asset_item: AssetItem
    useful_life_years: int
    warranty_months: int
    warranty_end: date | None
    warranty_active: bool | None
    book_value: int
    accumulated_depreciation: int
    assignee: str
    assignee_department: str
    usage_start: date | None
    usage_end: date | None
    return_date: date | None
    disposal_method: str
    disposal_certificate: str
    data_erasure: str
    disposal_date: date | None
    hostname: str
    ip_address: str
    kitting_pattern: str
    os_build: str
    last_seen: datetime | None
    notes: str

    @classmethod
    def build(cls, a: Asset, as_of: date, fy_start_month: int = 4) -> AssetOut:
        bv = book_value(
            a.price, a.usage_start, as_of, a.asset_item, a.useful_life_years, fy_start_month
        )
        base = a.delivery_date or a.order_date
        wend = add_months(base, a.warranty_months) if base else None
        return cls(
            id=a.id,
            asset_tag=a.asset_tag,
            status=a.status,
            budget_code=a.budget.code if a.budget else None,
            vendor=a.vendor,
            order_date=a.order_date,
            delivery_date=a.delivery_date,
            price=a.price,
            categories=[a.category1, a.category2, a.category3, a.category4, a.category5],
            part_number=a.part_number,
            serial_number=a.serial_number,
            model_name=a.model_name,
            asset_item=bv.item,
            useful_life_years=a.useful_life_years,
            warranty_months=a.warranty_months,
            warranty_end=wend,
            warranty_active=(wend >= as_of) if wend else None,
            book_value=bv.book_value,
            accumulated_depreciation=bv.accumulated,
            assignee=a.assignee,
            assignee_department=a.assignee_department,
            usage_start=a.usage_start,
            usage_end=a.usage_end,
            return_date=a.return_date,
            disposal_method=a.disposal_method,
            disposal_certificate=a.disposal_certificate,
            data_erasure=a.data_erasure,
            disposal_date=a.disposal_date,
            hostname=a.hostname,
            ip_address=a.ip_address,
            kitting_pattern=a.kitting_pattern,
            os_build=a.os_build,
            last_seen=a.last_seen,
            notes=a.notes,
        )


class AssignIn(BaseModel):
    assignee: str = Field(min_length=1, max_length=200)
    department: str = ""
    start: date | None = None


class ReturnIn(BaseModel):
    on: date | None = None
    note: str = ""


class StatusIn(BaseModel):
    status: AssetStatus
    note: str = ""


class DisposeIn(BaseModel):
    method: str
    data_erasure: str
    on: date | None = None
    vendor: str = ""
    certificate: str = ""


class JobIn(BaseModel):
    playbook: str
    extra_vars: dict = Field(default_factory=dict)


class JobOut(BaseModel):
    id: int
    asset_tag: str
    playbook: str
    status: str
    requested_by: str
    return_code: int | None
    created_at: datetime | None
    started_at: datetime | None
    finished_at: datetime | None
    output: str | None = None


class CheckinIn(BaseModel):
    serial: str = Field(min_length=1, max_length=100)
    hostname: str = Field(min_length=1, max_length=15, pattern=r"^[A-Za-z0-9-]+$")  # NetBIOS
    ip: str = ""
    mac: str = ""
    pattern: str = ""
    os_build: str = ""


class ReportIn(BaseModel):
    serial: str
    ok: bool
    summary: str = Field("", max_length=2000)


class BudgetIn(BaseModel):
    code: str = Field(min_length=1, max_length=40)
    name: str
    fiscal_year: int
    amount: int = Field(ge=0)


class TokenIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    role: str
