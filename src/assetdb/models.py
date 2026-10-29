"""Database schema (SQLAlchemy 2.0). PostgreSQL in production, SQLite for tests/demo."""

from __future__ import annotations

import enum
from datetime import date, datetime

from sqlalchemy import (
    JSON,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class AssetStatus(enum.StrEnum):
    ORDERED = "発注済"
    IN_STOCK = "在庫"
    KITTING = "キッティング中"
    IN_USE = "稼働中"
    REPAIR = "修理中"
    RETURNED = "返却済"
    PENDING_DISPOSAL = "廃棄待ち"
    DISPOSED = "廃棄済"


class AssetItem(enum.StrEnum):
    """資産項目 (Japanese accounting treatment of IT equipment)."""

    EXPENSE = "消耗品"  # < 100,000 JPY: expensed on acquisition
    LUMP_SUM = "一括償却資産"  # 100,000 - 199,999: 1/3 per fiscal year for 3 years
    SME_SPECIAL = "少額減価償却資産"  # SME special rule (< 300,000): fully expensed
    DEPRECIABLE = "減価償却資産"  # straight-line over useful life


class JobStatus(enum.StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class Role(enum.StrEnum):
    ADMIN = "admin"  # everything incl. disposal and tokens
    OPERATOR = "operator"  # assign/return/run scripts/remote desktop
    VIEWER = "viewer"  # read only
    ENROLL = "enroll"  # used by freshly imaged PCs to check in; nothing else


class Budget(Base):
    __tablename__ = "budgets"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(40), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    fiscal_year: Mapped[int]
    amount: Mapped[int]  # JPY

    assets: Mapped[list[Asset]] = relationship(back_populates="budget")


class Asset(Base):
    __tablename__ = "assets"

    id: Mapped[int] = mapped_column(primary_key=True)
    asset_tag: Mapped[str] = mapped_column(String(40), unique=True)
    status: Mapped[AssetStatus] = mapped_column(
        Enum(AssetStatus, native_enum=False, length=20), default=AssetStatus.ORDERED
    )

    # --- procurement
    budget_id: Mapped[int | None] = mapped_column(ForeignKey("budgets.id"))
    vendor: Mapped[str] = mapped_column(String(200), default="")  # 購入先
    order_date: Mapped[date | None] = mapped_column(Date)  # 注文日
    delivery_date: Mapped[date | None] = mapped_column(Date)  # 納品日
    price: Mapped[int] = mapped_column(default=0)  # 価格 (JPY, tax excluded)

    # --- classification (商品カテゴリー1〜5, e.g. ハードウェア > PC > ノート > 14inch > 法人)
    category1: Mapped[str] = mapped_column(String(100), default="")
    category2: Mapped[str] = mapped_column(String(100), default="")
    category3: Mapped[str] = mapped_column(String(100), default="")
    category4: Mapped[str] = mapped_column(String(100), default="")
    category5: Mapped[str] = mapped_column(String(100), default="")
    part_number: Mapped[str] = mapped_column(String(100), default="")  # P/N
    serial_number: Mapped[str] = mapped_column(String(100), unique=True)  # S/N
    model_name: Mapped[str] = mapped_column(String(200), default="")

    # --- accounting
    asset_item: Mapped[AssetItem | None] = mapped_column(
        Enum(AssetItem, native_enum=False, length=20)
    )  # 資産項目 (None -> derived from price)
    useful_life_years: Mapped[int] = mapped_column(default=4)  # PC: 4 years
    warranty_months: Mapped[int] = mapped_column(default=36)  # 保証期間

    # --- lifecycle
    assignee: Mapped[str] = mapped_column(String(200), default="")  # 割り当て先
    assignee_department: Mapped[str] = mapped_column(String(200), default="")
    usage_start: Mapped[date | None] = mapped_column(Date)  # 利用開始日
    usage_end: Mapped[date | None] = mapped_column(Date)  # 利用終了日
    return_date: Mapped[date | None] = mapped_column(Date)  # 返却日

    # --- disposal (廃棄処理)
    disposal_method: Mapped[str] = mapped_column(String(100), default="")  # リサイクル/売却/破砕
    disposal_vendor: Mapped[str] = mapped_column(String(200), default="")
    disposal_certificate: Mapped[str] = mapped_column(String(100), default="")  # 証明書番号
    data_erasure: Mapped[str] = mapped_column(String(200), default="")  # 消去方法・証跡
    disposal_date: Mapped[date | None] = mapped_column(Date)  # 廃棄日

    # --- endpoint / kitting
    hostname: Mapped[str] = mapped_column(String(63), default="")
    ip_address: Mapped[str] = mapped_column(String(45), default="")
    mac_address: Mapped[str] = mapped_column(String(17), default="")
    kitting_pattern: Mapped[str] = mapped_column(String(50), default="")
    os_build: Mapped[str] = mapped_column(String(50), default="")
    last_seen: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rdp_port: Mapped[int] = mapped_column(default=3389)
    guac_connection_id: Mapped[str] = mapped_column(String(40), default="")

    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    budget: Mapped[Budget | None] = relationship(back_populates="assets")
    history: Mapped[list[StatusHistory]] = relationship(
        back_populates="asset", order_by="StatusHistory.id", cascade="all, delete-orphan"
    )
    jobs: Mapped[list[Job]] = relationship(
        back_populates="asset", order_by="Job.id.desc()", cascade="all, delete-orphan"
    )


class StatusHistory(Base):
    """状態記録: every status change and lifecycle event, append-only."""

    __tablename__ = "status_history"

    id: Mapped[int] = mapped_column(primary_key=True)
    asset_id: Mapped[int] = mapped_column(ForeignKey("assets.id"), index=True)
    from_status: Mapped[str] = mapped_column(String(20), default="")
    to_status: Mapped[str] = mapped_column(String(20))
    event: Mapped[str] = mapped_column(String(40))  # register/assign/return/kitting/...
    actor: Mapped[str] = mapped_column(String(100))
    note: Mapped[str] = mapped_column(Text, default="")
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    asset: Mapped[Asset] = relationship(back_populates="history")


class Job(Base):
    """Script Running: an Ansible playbook run against one asset."""

    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    asset_id: Mapped[int] = mapped_column(ForeignKey("assets.id"), index=True)
    playbook: Mapped[str] = mapped_column(String(100))
    extra_vars: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[JobStatus] = mapped_column(
        Enum(JobStatus, native_enum=False, length=12), default=JobStatus.QUEUED, index=True
    )
    requested_by: Mapped[str] = mapped_column(String(100))
    return_code: Mapped[int | None] = mapped_column(Integer)
    output: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    asset: Mapped[Asset] = relationship(back_populates="jobs")


class ApiToken(Base):
    __tablename__ = "api_tokens"
    __table_args__ = (UniqueConstraint("token_hash"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))  # who/what uses it
    role: Mapped[Role] = mapped_column(Enum(Role, native_enum=False, length=10))
    token_hash: Mapped[str] = mapped_column(String(64))  # sha256; plaintext is never stored
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    revoked: Mapped[bool] = mapped_column(default=False)


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    actor: Mapped[str] = mapped_column(String(100))
    action: Mapped[str] = mapped_column(String(50))
    target: Mapped[str] = mapped_column(String(100), default="")
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
