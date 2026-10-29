"""Lifecycle rules. Every state change goes through here so that the
状態記録 (status history) and the audit log can never be skipped."""

from __future__ import annotations

import csv
import io
from datetime import UTC, date, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import Asset, AssetItem, AssetStatus, AuditLog, Budget, StatusHistory

S = AssetStatus

# Allowed transitions. Anything else is rejected, e.g. disposing a PC that is
# still assigned to someone, or putting a disposed PC back into service.
TRANSITIONS: dict[AssetStatus, set[AssetStatus]] = {
    S.ORDERED: {S.IN_STOCK, S.KITTING},
    S.IN_STOCK: {S.KITTING, S.IN_USE, S.REPAIR, S.PENDING_DISPOSAL},
    S.KITTING: {S.IN_STOCK, S.IN_USE, S.REPAIR},
    S.IN_USE: {S.REPAIR, S.RETURNED, S.KITTING},
    S.REPAIR: {S.IN_STOCK, S.IN_USE, S.PENDING_DISPOSAL},
    S.RETURNED: {S.KITTING, S.IN_STOCK, S.REPAIR, S.PENDING_DISPOSAL},
    S.PENDING_DISPOSAL: {S.DISPOSED, S.IN_STOCK},
    S.DISPOSED: set(),
}


class RuleError(ValueError):
    """A business rule was violated; message is safe to show to users."""


def audit(session: Session, actor: str, action: str, target: str = "", **detail) -> None:
    session.add(AuditLog(actor=actor, action=action, target=target, detail=detail))


def _transition(
    session: Session, asset: Asset, to: AssetStatus, actor: str, event: str, note: str = ""
) -> None:
    if to != asset.status and to not in TRANSITIONS[asset.status]:
        raise RuleError(
            f"{asset.asset_tag}: 「{asset.status.value}」から「{to.value}」には変更できません"
        )
    session.add(
        StatusHistory(
            asset_id=asset.id,
            from_status=asset.status.value if asset.id else "",
            to_status=to.value,
            event=event,
            actor=actor,
            note=note,
        )
    )
    asset.status = to


def get_asset(session: Session, key: str | int) -> Asset:
    """Look up by id, asset tag, serial number or hostname."""
    if isinstance(key, int) or str(key).isdigit():
        asset = session.get(Asset, int(key))
        if asset:
            return asset
    k = str(key).strip()
    asset = session.scalar(
        select(Asset).where(
            (func.upper(Asset.asset_tag) == k.upper())
            | (func.upper(Asset.serial_number) == k.upper())
            | (func.upper(Asset.hostname) == k.upper())
        )
    )
    if not asset:
        raise LookupError(f"資産が見つかりません: {key}")
    return asset


# ------------------------------------------------------------------ register / update

EDITABLE = {
    "vendor",
    "order_date",
    "delivery_date",
    "price",
    "category1",
    "category2",
    "category3",
    "category4",
    "category5",
    "part_number",
    "model_name",
    "asset_item",
    "useful_life_years",
    "warranty_months",
    "kitting_pattern",
    "notes",
    "hostname",
    "rdp_port",
}


def register_asset(session: Session, actor: str, data: dict) -> Asset:
    if not data.get("serial_number"):
        raise RuleError("S/N は必須です")
    if session.scalar(select(Asset).where(Asset.serial_number == data["serial_number"])):
        raise RuleError(f"S/N {data['serial_number']} は登録済みです")
    budget_code = data.pop("budget_code", None)
    budget = None
    if budget_code:  # check before the new row exists, so autoflush can't half-insert it
        budget = _budget(session, budget_code)
        check_budget(session, budget, extra=data.get("price") or 0)
    tag = data.pop("asset_tag", None) or next_asset_tag(session)
    asset = Asset(asset_tag=tag, budget=budget, **{k: v for k, v in data.items() if v is not None})
    initial = AssetStatus.IN_STOCK if asset.delivery_date else AssetStatus.ORDERED
    asset.status = initial
    session.add(asset)
    session.flush()
    session.add(
        StatusHistory(asset_id=asset.id, to_status=initial.value, event="register", actor=actor)
    )
    audit(session, actor, "asset.register", asset.asset_tag, serial=asset.serial_number)
    return asset


def update_asset(session: Session, actor: str, asset: Asset, data: dict) -> Asset:
    unknown = set(data) - EDITABLE - {"budget_code"}
    if unknown:
        raise RuleError(f"変更できない項目です: {', '.join(sorted(unknown))}")
    changes = {}
    if "budget_code" in data:
        code = data.pop("budget_code")
        asset.budget = _budget(session, code) if code else None
        changes["budget"] = code
    for k, v in data.items():
        if getattr(asset, k) != v:
            changes[k] = str(v)
            setattr(asset, k, v)
    if changes:
        audit(session, actor, "asset.update", asset.asset_tag, **changes)
    return asset


def next_asset_tag(session: Session, prefix: str = "PC") -> str:
    n = session.scalar(select(func.count(Asset.id))) or 0
    while True:
        n += 1
        tag = f"{prefix}-{n:05d}"
        if not session.scalar(select(Asset.id).where(Asset.asset_tag == tag)):
            return tag


# ------------------------------------------------------------------ lifecycle


def receive(session: Session, actor: str, asset: Asset, delivery_date: date) -> Asset:
    asset.delivery_date = delivery_date
    _transition(session, asset, AssetStatus.IN_STOCK, actor, "receive", f"納品 {delivery_date}")
    audit(session, actor, "asset.receive", asset.asset_tag)
    return asset


def assign(
    session: Session,
    actor: str,
    asset: Asset,
    assignee: str,
    department: str = "",
    start: date | None = None,
) -> Asset:
    if asset.assignee:
        raise RuleError(
            f"{asset.asset_tag} は {asset.assignee} に割り当て済みです。先に返却してください"
        )
    if asset.status not in (AssetStatus.IN_STOCK, AssetStatus.KITTING):
        raise RuleError(f"{asset.asset_tag} は「{asset.status.value}」のため割り当てできません")
    asset.assignee, asset.assignee_department = assignee, department
    asset.usage_start = asset.usage_start or start or date.today()  # depreciation starts here
    asset.usage_end = None
    asset.return_date = None
    _transition(session, asset, AssetStatus.IN_USE, actor, "assign", f"{assignee} {department}")
    audit(session, actor, "asset.assign", asset.asset_tag, assignee=assignee)
    return asset


def return_asset(
    session: Session, actor: str, asset: Asset, on: date | None = None, note: str = ""
) -> Asset:
    if not asset.assignee:
        raise RuleError(f"{asset.asset_tag} は割り当てられていません")
    on = on or date.today()
    prev = asset.assignee
    asset.return_date = on
    asset.usage_end = on
    asset.assignee = ""
    asset.assignee_department = ""
    _transition(session, asset, AssetStatus.RETURNED, actor, "return", f"{prev} から返却 {note}")
    audit(session, actor, "asset.return", asset.asset_tag, previous=prev)
    return asset


def change_status(
    session: Session, actor: str, asset: Asset, to: AssetStatus, note: str = ""
) -> Asset:
    if to in (AssetStatus.IN_USE, AssetStatus.DISPOSED, AssetStatus.RETURNED):
        raise RuleError("この状態へは専用の操作（割り当て・返却・廃棄）を使ってください")
    if asset.assignee and to in (AssetStatus.IN_STOCK, AssetStatus.PENDING_DISPOSAL):
        raise RuleError(
            f"{asset.asset_tag} は {asset.assignee} に割り当て中です。先に返却してください"
        )
    _transition(session, asset, to, actor, "status", note)
    audit(session, actor, "asset.status", asset.asset_tag, to=to.value)
    return asset


def dispose(
    session: Session,
    actor: str,
    asset: Asset,
    *,
    method: str,
    data_erasure: str,
    on: date | None = None,
    vendor: str = "",
    certificate: str = "",
) -> Asset:
    """廃棄処理. Data erasure evidence is mandatory: a PC must never leave the
    company with recoverable data on it."""
    if asset.status != AssetStatus.PENDING_DISPOSAL:
        raise RuleError(f"{asset.asset_tag} は「廃棄待ち」にしてから廃棄してください")
    if not data_erasure.strip():
        raise RuleError("データ消去の方法・証跡（例: NIST 800-88 Purge／消去証明書番号）が必要です")
    if not method.strip():
        raise RuleError("廃棄方法を入力してください")
    asset.disposal_method = method
    asset.disposal_vendor = vendor
    asset.disposal_certificate = certificate
    asset.data_erasure = data_erasure
    asset.disposal_date = on or date.today()
    asset.usage_end = asset.usage_end or asset.disposal_date
    _transition(
        session,
        asset,
        AssetStatus.DISPOSED,
        actor,
        "dispose",
        f"{method} / {data_erasure} / {certificate}",
    )
    audit(session, actor, "asset.dispose", asset.asset_tag, method=method, certificate=certificate)
    return asset


# ------------------------------------------------------------------ kitting


def kitting_checkin(
    session: Session,
    actor: str,
    *,
    serial: str,
    hostname: str,
    ip: str = "",
    mac: str = "",
    pattern: str = "",
    os_build: str = "",
) -> Asset:
    """Called by a freshly imaged PC on first boot. Unknown serials are refused:
    only hardware that went through procurement may join the managed fleet."""
    asset = session.scalar(select(Asset).where(func.upper(Asset.serial_number) == serial.upper()))
    if asset is None:
        audit(session, actor, "kitting.unknown_device", serial, hostname=hostname, ip=ip)
        session.commit()
        raise LookupError(f"未登録のS/Nです: {serial}")
    if asset.status == AssetStatus.DISPOSED:
        raise RuleError(f"{asset.asset_tag} は廃棄済みです")
    asset.hostname = hostname
    asset.ip_address = ip
    asset.mac_address = mac
    asset.os_build = os_build
    asset.kitting_pattern = pattern or asset.kitting_pattern
    asset.last_seen = datetime.now(UTC)
    if asset.status != AssetStatus.KITTING:
        _transition(
            session,
            asset,
            AssetStatus.KITTING,
            actor,
            "kitting",
            f"イメージ展開完了 pattern={asset.kitting_pattern} host={hostname}",
        )
    audit(session, actor, "kitting.checkin", asset.asset_tag, hostname=hostname, ip=ip)
    return asset


def kitting_report(session: Session, actor: str, asset: Asset, ok: bool, summary: str) -> Asset:
    asset.last_seen = datetime.now(UTC)
    if ok and asset.status == AssetStatus.KITTING:
        to = AssetStatus.IN_USE if asset.assignee else AssetStatus.IN_STOCK
        _transition(session, asset, to, actor, "kitting_done", summary)
    else:
        session.add(
            StatusHistory(
                asset_id=asset.id,
                from_status=asset.status.value,
                to_status=asset.status.value,
                event="kitting_failed" if not ok else "kitting_report",
                actor=actor,
                note=summary,
            )
        )
    audit(session, actor, "kitting.report", asset.asset_tag, ok=ok)
    return asset


# ------------------------------------------------------------------ budgets


def _budget(session: Session, code: str) -> Budget:
    b = session.scalar(select(Budget).where(Budget.code == code))
    if not b:
        raise RuleError(f"予算コードが見つかりません: {code}")
    return b


def budget_spent(session: Session, budget: Budget) -> int:
    return (
        session.scalar(
            select(func.coalesce(func.sum(Asset.price), 0)).where(Asset.budget_id == budget.id)
        )
        or 0
    )


def check_budget(session: Session, budget: Budget, extra: int) -> None:
    spent = budget_spent(session, budget)
    if spent + extra > budget.amount:
        raise RuleError(
            f"予算 {budget.code} を超過します（残 {budget.amount - spent:,}円 / 今回 {extra:,}円）"
        )


# ------------------------------------------------------------------ CSV import

CSV_COLUMNS = {
    "資産番号": "asset_tag",
    "予算コード": "budget_code",
    "購入先": "vendor",
    "注文日": "order_date",
    "納品日": "delivery_date",
    "カテゴリー1": "category1",
    "カテゴリー2": "category2",
    "カテゴリー3": "category3",
    "カテゴリー4": "category4",
    "カテゴリー5": "category5",
    "P/N": "part_number",
    "S/N": "serial_number",
    "機種": "model_name",
    "価格": "price",
    "保証期間(月)": "warranty_months",
    "資産項目": "asset_item",
    "耐用年数": "useful_life_years",
    "キッティングパターン": "kitting_pattern",
    "備考": "notes",
}
_INT = {"price", "warranty_months", "useful_life_years"}
_DATE = {"order_date", "delivery_date"}


def import_csv(session: Session, actor: str, text: str) -> dict:
    """Register assets from a purchase CSV (Japanese headers). All-or-nothing:
    one bad row rolls back the whole file and reports every error with its line."""
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    created, errors = [], []
    for line, row in enumerate(reader, start=2):
        data: dict = {}
        try:
            for jp, key in CSV_COLUMNS.items():
                val = (row.get(jp) or "").strip()
                if not val:
                    continue
                if key in _INT:
                    data[key] = int(val.replace(",", ""))
                elif key in _DATE:
                    data[key] = date.fromisoformat(val.replace("/", "-"))
                elif key == "asset_item":
                    data[key] = AssetItem(val)
                else:
                    data[key] = val
            with session.begin_nested():
                created.append(register_asset(session, actor, data).asset_tag)
        except (ValueError, RuleError) as e:
            errors.append(f"{line}行目: {e}")
    if errors:
        session.rollback()
        return {"created": [], "errors": errors}
    return {"created": created, "errors": []}
