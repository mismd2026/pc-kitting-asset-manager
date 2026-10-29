"""Ansible dynamic inventory built from the asset DB.

Groups: ``windows`` (all managed PCs), ``pattern_<name>``, ``status_<x>``,
``dept_<department>``. Disposed / pending-disposal / ordered assets are never
included, so a playbook can't accidentally target hardware that has left service.
"""

from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Asset, AssetStatus

MANAGED = [
    AssetStatus.IN_STOCK,
    AssetStatus.KITTING,
    AssetStatus.IN_USE,
    AssetStatus.REPAIR,
    AssetStatus.RETURNED,
]
_STATUS_SLUG = {
    AssetStatus.IN_STOCK: "in_stock",
    AssetStatus.KITTING: "kitting",
    AssetStatus.IN_USE: "in_use",
    AssetStatus.REPAIR: "repair",
    AssetStatus.RETURNED: "returned",
}


def _slug(text: str) -> str:
    s = re.sub(r"[^0-9a-zA-Z_]+", "_", text).strip("_").lower()
    return s or "other"


def build_inventory(session: Session) -> dict:
    inv: dict = {
        "_meta": {"hostvars": {}},
        "all": {"children": ["windows"]},
        "windows": {"hosts": []},
    }

    def add(group: str, host: str) -> None:
        inv.setdefault(group, {"hosts": []})["hosts"].append(host)

    rows = session.scalars(
        select(Asset)
        .where(Asset.status.in_(MANAGED), Asset.hostname != "")
        .order_by(Asset.hostname)
    )
    for a in rows:
        add("windows", a.hostname)
        if a.kitting_pattern:
            add(f"pattern_{_slug(a.kitting_pattern)}", a.hostname)
        add(f"status_{_STATUS_SLUG[a.status]}", a.hostname)
        if a.assignee_department:
            add(f"dept_{_slug(a.assignee_department)}", a.hostname)
        hv = {
            "asset_tag": a.asset_tag,
            "serial_number": a.serial_number,
            "kitting_pattern": a.kitting_pattern,
            "assignee": a.assignee,
            "asset_status": a.status.value,
        }
        if a.ip_address:
            hv["ansible_host"] = a.ip_address
        inv["_meta"]["hostvars"][a.hostname] = hv
    return inv
