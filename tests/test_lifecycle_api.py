from conftest import new_asset


def test_register_full_record_and_computed_fields(client, as_op, budget):
    a = new_asset(
        client,
        as_op,
        budget_code=budget,
        category4="14inch",
        category5="法人モデル",
        warranty_months=36,
    )
    assert a["asset_tag"] == "PC-00001"
    assert a["status"] == "在庫"  # has a delivery date
    assert a["categories"] == ["ハードウェア", "PC", "ノート", "14inch", "法人モデル"]
    assert a["budget_code"] == budget
    assert a["asset_item"] == "減価償却資産"
    assert a["warranty_end"] == "2029-04-10" and a["warranty_active"] is True
    assert a["book_value"] == 240_000  # not in use yet


def test_duplicate_serial_and_budget_overrun_are_rejected(client, as_op, budget):
    new_asset(client, as_op, budget_code=budget, price=900_000)
    r = client.post("/api/v1/assets", headers=as_op, json={"serial_number": "SN-0001"})
    assert r.status_code == 409 and "登録済み" in r.json()["detail"]
    r = client.post(
        "/api/v1/assets",
        headers=as_op,
        json={"serial_number": "SN-9", "price": 200_000, "budget_code": budget},
    )
    assert r.status_code == 409 and "予算" in r.json()["detail"]


def test_full_lifecycle_with_history(client, as_op, as_admin):
    tag = new_asset(client, as_op)["asset_tag"]
    r = client.post(
        f"/api/v1/assets/{tag}/assign",
        headers=as_op,
        json={"assignee": "山田 太郎", "department": "営業部"},
    )
    a = r.json()
    assert a["status"] == "稼働中" and a["usage_start"] == "2026-10-30"

    # cannot be disposed or re-assigned while someone is using it
    assert (
        client.post(
            f"/api/v1/assets/{tag}/assign", headers=as_op, json={"assignee": "x"}
        ).status_code
        == 409
    )
    assert (
        client.post(
            f"/api/v1/assets/{tag}/status", headers=as_op, json={"status": "廃棄待ち"}
        ).status_code
        == 409
    )

    a = client.post(f"/api/v1/assets/{tag}/return", headers=as_op, json={"note": "天板に傷"}).json()
    assert a["status"] == "返却済" and a["return_date"] == "2026-10-30" and a["assignee"] == ""

    client.post(f"/api/v1/assets/{tag}/status", headers=as_op, json={"status": "廃棄待ち"})
    # operator cannot dispose; admin must provide data-erasure evidence
    assert (
        client.post(
            f"/api/v1/assets/{tag}/dispose",
            headers=as_op,
            json={"method": "リサイクル", "data_erasure": "x"},
        ).status_code
        == 403
    )
    r = client.post(
        f"/api/v1/assets/{tag}/dispose",
        headers=as_admin,
        json={"method": "リサイクル", "data_erasure": " "},
    )
    assert r.status_code == 409 and "データ消去" in r.json()["detail"]
    a = client.post(
        f"/api/v1/assets/{tag}/dispose",
        headers=as_admin,
        json={
            "method": "リサイクル",
            "data_erasure": "NIST 800-88 Purge / 証明書 E-123",
            "certificate": "R-2026-0042",
            "vendor": "リサイクル業者A",
        },
    ).json()
    assert a["status"] == "廃棄済" and a["disposal_date"] == "2026-10-30"

    # disposed is terminal
    r = client.post(f"/api/v1/assets/{tag}/status", headers=as_admin, json={"status": "在庫"})
    assert r.status_code == 409

    events = [h["event"] for h in client.get(f"/api/v1/assets/{tag}/history", headers=as_op).json()]
    assert events == ["register", "assign", "return", "status", "dispose"]
    actions = [x["action"] for x in client.get("/api/v1/audit", headers=as_admin).json()]
    assert "asset.dispose" in actions and "asset.assign" in actions


def test_patch_only_allows_editable_fields(client, as_op):
    tag = new_asset(client, as_op)["asset_tag"]
    r = client.patch(f"/api/v1/assets/{tag}", headers=as_op, json={"price": 230_000})
    assert r.json()["price"] == 230_000
    r = client.patch(f"/api/v1/assets/{tag}", headers=as_op, json={"status": "廃棄済"})
    assert r.status_code == 422  # lifecycle fields only via dedicated endpoints


def test_lookup_by_serial_or_hostname_and_search(client, as_op, as_viewer):
    new_asset(client, as_op, serial="ABC-777", model_name="Surface Laptop 6")
    assert (
        client.get("/api/v1/assets/abc-777", headers=as_viewer).json()["serial_number"] == "ABC-777"
    )
    assert len(client.get("/api/v1/assets", params={"q": "surface"}, headers=as_viewer).json()) == 1
    assert client.get("/api/v1/assets/NOPE", headers=as_viewer).status_code == 404


def test_roles(client, as_viewer, as_enroll):
    assert client.get("/api/v1/assets").status_code == 401
    assert (
        client.get("/api/v1/assets", headers={"Authorization": "Bearer wrong"}).status_code == 401
    )
    assert (
        client.post("/api/v1/assets", headers=as_viewer, json={"serial_number": "X"}).status_code
        == 403
    )
    # an enroll token baked into a USB must not be able to read the inventory
    assert client.get("/api/v1/assets", headers=as_enroll).status_code == 403
    assert client.get("/api/v1/ansible/inventory", headers=as_enroll).status_code == 403


def test_csv_import_is_all_or_nothing(client, as_op, budget):
    header = "S/N,機種,価格,納品日,カテゴリー1,カテゴリー2,予算コード\n"
    good = (
        header + "CSV-1,ThinkPad,180000,2026/05/01,ハードウェア,PC,IT-2026-PC\n"
        "CSV-2,ThinkPad,180000,2026/05/01,ハードウェア,PC,IT-2026-PC\n"
    )
    r = client.post("/api/v1/assets/import", headers=as_op, content=("﻿" + good).encode())
    assert r.status_code == 200 and len(r.json()["created"]) == 2
    a = client.get("/api/v1/assets/CSV-1", headers=as_op).json()
    assert a["asset_item"] == "一括償却資産" and a["delivery_date"] == "2026-05-01"

    bad = header + "CSV-3,X,100,2026-05-01,,,\nCSV-1,X,abc,2026-05-01,,,\n"
    r = client.post("/api/v1/assets/import", headers=as_op, content=bad.encode())
    assert r.status_code == 422
    assert any("3行目" in e for e in r.json()["errors"])
    assert client.get("/api/v1/assets/CSV-3", headers=as_op).status_code == 404  # rolled back


def test_budget_summary_and_depreciation_report(client, as_op, as_viewer, budget):
    tag = new_asset(client, as_op, budget_code=budget)["asset_tag"]
    client.post(
        f"/api/v1/assets/{tag}/assign", headers=as_op, json={"assignee": "a", "start": "2026-04-01"}
    )
    b = client.get("/api/v1/budgets", headers=as_viewer).json()[0]
    assert (b["spent"], b["remaining"], b["assets"]) == (240_000, 760_000, 1)

    csv_text = client.get("/api/v1/reports/depreciation", headers=as_viewer).text
    rows = [line.split(",") for line in csv_text.lstrip("﻿").strip().splitlines()]
    assert rows[0][0] == "資産番号"
    # FY2026 ends 2027-03-31: Apr 2026..Mar 2027 = 12 months -> 60,000 depreciated
    assert rows[1][6:8] == ["60000", "180000"] and rows[1][-1] == "2027-03-31"
    assert rows[-1][0] == "合計" and rows[-1][7] == "180000"
