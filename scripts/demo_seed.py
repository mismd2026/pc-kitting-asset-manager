"""Load demo data (fictional company) so the UI can be explored without real devices.

ASSETDB_URL=sqlite:///./demo.sqlite python scripts/demo_seed.py
"""

from __future__ import annotations

import os
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from assetdb import services as svc  # noqa: E402
from assetdb.db import Database  # noqa: E402
from assetdb.models import Budget, Job, JobStatus  # noqa: E402
from kitimage.pattern import hostname_for  # noqa: E402

ACTOR = "demo-seed"
PREFIX = {"standard": "PC", "developer": "DEV", "kiosk": "KSK"}
MODELS = [
    ("ThinkPad T14 Gen 5", "21MC-CTO1WW", 238_000, "ノート", "14inch", "standard"),
    (
        "ThinkPad P16s Gen 3",
        "21KS-CTO1WW",
        365_000,
        "モバイルワークステーション",
        "16inch",
        "developer",
    ),
    ("ThinkCentre M70q Gen 5", "12TD-CTO1WW", 128_000, "デスクトップ", "Tiny", "kiosk"),
    ("Surface Laptop 6", "ZJQ-00011", 189_800, "ノート", "13.5inch", "standard"),
]
PEOPLE = [
    ("山田 太郎", "営業部"),
    ("佐藤 花子", "経理部"),
    ("鈴木 一郎", "開発部"),
    ("高橋 美咲", "開発部"),
    ("田中 健", "総務部"),
    ("伊藤 愛", "人事部"),
]


def main() -> None:
    db = Database(os.environ.get("ASSETDB_URL", "sqlite:///./demo.sqlite"))
    db.create_all()
    with db.SessionLocal() as s:
        if s.query(Budget).count():
            print("already seeded")
            return
        s.add_all(
            [
                Budget(
                    code="IT-2025-PC", name="2025年度 PC更新", fiscal_year=2025, amount=3_000_000
                ),
                Budget(
                    code="IT-2026-PC", name="2026年度 PC更新", fiscal_year=2026, amount=4_000_000
                ),
                Budget(
                    code="IT-2026-DEV", name="2026年度 開発環境", fiscal_year=2026, amount=1_500_000
                ),
            ]
        )
        s.flush()
        n = 0
        for i in range(14):
            model, pn, price, cat3, cat4, pattern = MODELS[i % len(MODELS)]
            fy = 2025 if i < 5 else 2026
            order = date(fy, 5 + (i % 6), 1 + i)
            budget = "IT-2026-DEV" if pattern == "developer" and fy == 2026 else f"IT-{fy}-PC"
            serial = f"{pn[:4]}{7300 + i * 37:05d}"
            a = svc.register_asset(
                s,
                ACTOR,
                {
                    "serial_number": serial,
                    "budget_code": budget,
                    "vendor": "株式会社サンプル商事",
                    "order_date": order,
                    "delivery_date": order + timedelta(days=9),
                    "price": price,
                    "category1": "ハードウェア",
                    "category2": "PC",
                    "category3": cat3,
                    "category4": cat4,
                    "category5": "法人モデル",
                    "part_number": pn,
                    "model_name": model,
                    "kitting_pattern": pattern,
                    "warranty_months": 36 if i % 5 else 12,
                },
            )
            if i == 13:
                continue  # stays in stock, not kitted yet
            host = hostname_for(PREFIX[pattern], serial)  # same rule as enroll.ps1
            svc.kitting_checkin(
                s,
                "enroll",
                serial=serial,
                hostname=host,
                ip=f"10.20.{i // 10}.{50 + i}",
                pattern=pattern,
                os_build="26100",
            )
            ok = i != 12
            job = Job(
                asset=a,
                playbook="site",
                requested_by="kitting",
                status=JobStatus.SUCCEEDED if ok else JobStatus.FAILED,
                return_code=0 if ok else 2,
                started_at=datetime.now(UTC) - timedelta(days=30 - i),
                finished_at=datetime.now(UTC) - timedelta(days=30 - i, minutes=-23),
                output=(
                    f"PLAY RECAP ****\n{host} : ok=41 changed=27 unreachable=0 failed=0"
                    if ok
                    else f"TASK [apps : Applications for pattern {pattern}]\n"
                    f"fatal: [{host}]: FAILED! => timeout waiting for package source"
                ),
            )
            s.add(job)
            if not ok:
                svc.kitting_report(s, "ansible", a, False, "apps: package source timeout")
                continue
            svc.kitting_report(s, "ansible", a, True, f"pattern={pattern}")
            if i < len(PEOPLE) + 4 and i not in (7, 9):
                who, dept = PEOPLE[n % len(PEOPLE)]
                n += 1
                svc.assign(s, ACTOR, a, who, dept, order + timedelta(days=20))
        # one returned, one in repair, one disposed
        r = svc.get_asset(s, "PC-00002")
        svc.return_asset(s, ACTOR, r, date(2026, 9, 30), "異動に伴う返却")
        svc.change_status(
            s,
            ACTOR,
            svc.get_asset(s, "PC-00008"),
            svc.AssetStatus.REPAIR,
            "キーボード不良、メーカー修理",
        )
        d = svc.get_asset(s, "PC-00010")
        svc.change_status(s, ACTOR, d, svc.AssetStatus.PENDING_DISPOSAL, "落下破損")
        svc.dispose(
            s,
            ACTOR,
            d,
            method="リサイクル（小型家電）",
            data_erasure="NIST 800-88 Purge（ATA Secure Erase）",
            on=date(2026, 10, 15),
            vendor="サンプルリサイクル",
            certificate="RC-2026-0123",
        )
        s.commit()
    print("seeded demo data")


if __name__ == "__main__":
    main()
