"""`kit` — command line tool for the asset manager.

    export KIT_URL=https://assets.example.local  KIT_TOKEN=...
    kit ls --status 稼働中
    kit show PC-00012
    kit import purchase.csv
    kit assign PC-00012 "山田 太郎" --dept 営業部
    kit run PC-00012 security --wait
    kit rdp PC-00012 --open
    kit report depreciation > fy2026.csv

Every command is a thin wrapper over the REST API, so the CLI can never do
anything the API (and its permission checks / audit log) would not allow.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import unicodedata
from pathlib import Path

import httpx


class CliError(Exception):
    pass


class Api:
    def __init__(self, base_url: str, token: str, client: httpx.Client | None = None) -> None:
        self.c = client or httpx.Client(base_url=base_url, timeout=30)
        self.headers = {"Authorization": f"Bearer {token}"}

    def req(self, method: str, path: str, headers: dict | None = None, **kw):
        r = self.c.request(
            method, f"/api/v1{path}", headers={**self.headers, **(headers or {})}, **kw
        )
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail") or r.json().get("errors")
            except ValueError:
                detail = r.text
            raise CliError(f"{r.status_code}: {detail}")
        return r


# ---------------------------------------------------------------- output helpers


def _w(s: str) -> int:
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in s)


def _pad(s: str, width: int) -> str:
    return s + " " * (width - _w(s))


def table(rows: list[list], headers: list[str]) -> str:
    cells = [[str(x if x is not None else "") for x in r] for r in rows]
    widths = [max([_w(h)] + [_w(r[i]) for r in cells]) for i, h in enumerate(headers)]
    lines = ["  ".join(_pad(h, w) for h, w in zip(headers, widths, strict=True))]
    lines.append("  ".join("-" * w for w in widths))
    lines += ["  ".join(_pad(c, w) for c, w in zip(r, widths, strict=True)) for r in cells]
    return "\n".join(line.rstrip() for line in lines)


def yen(v) -> str:
    return f"{v:,}" if isinstance(v, int) else ""


# ---------------------------------------------------------------- commands


def cmd_ls(api: Api, a) -> str:
    params = {
        k: v for k, v in {"status": a.status, "q": a.query, "category1": a.category}.items() if v
    }
    data = api.req("GET", "/assets", params=params).json()
    if a.json:
        return json.dumps(data, ensure_ascii=False, indent=2)
    rows = [
        [
            x["asset_tag"],
            x["status"],
            x["model_name"],
            x["serial_number"],
            x["hostname"],
            x["assignee"],
            yen(x["price"]),
            yen(x["book_value"]),
        ]
        for x in data
    ]
    return (
        table(rows, ["資産番号", "状態", "機種", "S/N", "ホスト名", "割当先", "価格", "償却残価"])
        + f"\n\n{len(data)} 件"
    )


def cmd_show(api: Api, a) -> str:
    x = api.req("GET", f"/assets/{a.key}").json()
    if a.json:
        return json.dumps(x, ensure_ascii=False, indent=2)
    pairs = [
        ("資産番号", x["asset_tag"]),
        ("状態", x["status"]),
        ("機種", x["model_name"]),
        ("カテゴリー", " > ".join(c for c in x["categories"] if c)),
        ("P/N", x["part_number"]),
        ("S/N", x["serial_number"]),
        ("予算", x["budget_code"]),
        ("購入先", x["vendor"]),
        ("注文日", x["order_date"]),
        ("納品日", x["delivery_date"]),
        ("価格", yen(x["price"])),
        ("資産項目", x["asset_item"]),
        ("償却残価", yen(x["book_value"])),
        ("保証期限", x["warranty_end"]),
        ("割り当て先", f"{x['assignee']} {x['assignee_department']}"),
        ("利用開始日", x["usage_start"]),
        ("利用終了日", x["usage_end"]),
        ("返却日", x["return_date"]),
        ("ホスト名", x["hostname"]),
        ("IP", x["ip_address"]),
        ("パターン", x["kitting_pattern"]),
        ("OS", x["os_build"]),
        ("廃棄日", x["disposal_date"]),
        ("廃棄処理", x["disposal_method"]),
    ]
    w = max(_w(k) for k, _ in pairs)
    return "\n".join(f"{_pad(k, w)}  {v if v not in (None, '') else '-'}" for k, v in pairs)


def cmd_history(api: Api, a) -> str:
    rows = api.req("GET", f"/assets/{a.key}/history").json()
    return table(
        [
            [
                r["at"][:16].replace("T", " "),
                r["event"],
                f"{r['from']}→{r['to']}" if r["from"] and r["from"] != r["to"] else r["to"],
                r["actor"],
                r["note"],
            ]
            for r in rows
        ],
        ["日時", "イベント", "状態", "操作者", "メモ"],
    )


def cmd_import(api: Api, a) -> str:
    text = Path(a.file).read_text(encoding="utf-8-sig")
    try:
        res = api.req(
            "POST",
            "/assets/import",
            content=text.encode(),
            headers={"Content-Type": "text/csv"},
        ).json()
    except CliError as e:
        raise CliError(f"取り込みを中止しました（1件も登録していません）\n{e}") from e
    return f"{len(res['created'])} 件登録しました: {', '.join(res['created'])}"


def cmd_assign(api: Api, a) -> str:
    x = api.req(
        "POST", f"/assets/{a.key}/assign", json={"assignee": a.assignee, "department": a.dept}
    ).json()
    return f"{x['asset_tag']} を {x['assignee']} に割り当てました（利用開始 {x['usage_start']}）"


def cmd_return(api: Api, a) -> str:
    x = api.req("POST", f"/assets/{a.key}/return", json={"note": a.note}).json()
    return f"{x['asset_tag']} の返却を登録しました（{x['return_date']}）"


def cmd_status(api: Api, a) -> str:
    x = api.req("POST", f"/assets/{a.key}/status", json={"status": a.status, "note": a.note}).json()
    return f"{x['asset_tag']}: {x['status']}"


def cmd_dispose(api: Api, a) -> str:
    x = api.req(
        "POST",
        f"/assets/{a.key}/dispose",
        json={
            "method": a.method,
            "data_erasure": a.erasure,
            "certificate": a.cert,
            "vendor": a.vendor,
        },
    ).json()
    return f"{x['asset_tag']} を廃棄済みにしました（{x['disposal_date']}）"


def _parse_vars(pairs: list[str]) -> dict:
    out = {}
    for p in pairs or []:
        if "=" not in p:
            raise CliError(f"-e は key=value 形式で指定してください: {p}")
        k, v = p.split("=", 1)
        out[k] = v
    return out


def cmd_run(api: Api, a, sleep=time.sleep) -> str:
    job = api.req(
        "POST",
        f"/assets/{a.key}/jobs",
        json={"playbook": a.playbook, "extra_vars": _parse_vars(a.extra)},
    ).json()
    if not a.wait:
        return f"ジョブ #{job['id']} を登録しました（kit job {job['id']} で確認）"
    while job["status"] in ("queued", "running"):
        sleep(3)
        job = api.req("GET", f"/jobs/{job['id']}").json()
    tail = "\n".join((job.get("output") or "").splitlines()[-30:])
    result = f"ジョブ #{job['id']} {job['status']} (rc={job['return_code']})\n{tail}"
    if job["status"] != "succeeded":
        raise CliError(result)
    return result


def cmd_jobs(api: Api, a) -> str:
    rows = api.req("GET", "/jobs", params={"asset": a.key} if a.key else {}).json()
    return table(
        [
            [
                j["id"],
                j["asset_tag"],
                j["playbook"],
                j["status"],
                j["return_code"],
                j["requested_by"],
                (j["created_at"] or "")[:16].replace("T", " "),
            ]
            for j in rows
        ],
        ["#", "資産", "プレイブック", "状態", "rc", "依頼者", "登録日時"],
    )


def cmd_job(api: Api, a) -> str:
    j = api.req("GET", f"/jobs/{a.id}").json()
    return (
        f"#{j['id']} {j['playbook']} on {j['asset_tag']}: {j['status']} rc={j['return_code']}\n"
        + (j.get("output") or "")
    )


def cmd_playbooks(api: Api, a) -> str:
    return table(
        [[p["name"], p["description"]] for p in api.req("GET", "/playbooks").json()],
        ["名前", "内容"],
    )


def cmd_rdp(api: Api, a) -> str:
    r = api.req("GET", f"/assets/{a.key}/rdp", params={"username": a.user} if a.user else {})
    out = Path(a.output or f"{a.key}.rdp")
    out.write_bytes(r.content)
    if a.open:
        opener = {"win32": ["cmd", "/c", "start", ""], "darwin": ["open"]}.get(
            sys.platform, ["xdg-open"]
        )
        subprocess.Popen([*opener, str(out)])
    return f"{out} を保存しました"


def cmd_remote(api: Api, a) -> str:
    return api.req("POST", f"/assets/{a.key}/remote-session").json()["url"]


def cmd_budgets(api: Api, a) -> str:
    rows = api.req("GET", "/budgets").json()
    return table(
        [
            [
                b["fiscal_year"],
                b["code"],
                b["name"],
                yen(b["amount"]),
                yen(b["spent"]),
                yen(b["remaining"]),
                b["assets"],
            ]
            for b in rows
        ],
        ["年度", "コード", "名称", "予算額", "執行額", "残額", "台数"],
    )


def cmd_report(api: Api, a) -> str:
    params = {"as_of": a.as_of} if a.as_of else {}
    return api.req("GET", "/reports/depreciation", params=params).text.lstrip("﻿")


def cmd_token(api: Api, a) -> str:
    x = api.req("POST", "/tokens", json={"name": a.name, "role": a.role}).json()
    return f"{x['token']}\n# {x['note']}"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="kit", description="PC資産管理 CLI")
    p.add_argument("--url", default=os.environ.get("KIT_URL", "http://localhost:8000"))
    p.add_argument("--token", default=os.environ.get("KIT_TOKEN", ""))
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, fn, help_):
        sp = sub.add_parser(name, help=help_)
        sp.set_defaults(fn=fn)
        return sp

    s = add("ls", cmd_ls, "資産一覧")
    s.add_argument("--status")
    s.add_argument("-q", "--query")
    s.add_argument("--category")
    s.add_argument("--json", action="store_true")
    s = add("show", cmd_show, "資産の詳細")
    s.add_argument("key")
    s.add_argument("--json", action="store_true")
    add("history", cmd_history, "状態記録").add_argument("key")
    add("import", cmd_import, "購入CSVの取り込み").add_argument("file")
    s = add("assign", cmd_assign, "割り当て")
    s.add_argument("key")
    s.add_argument("assignee")
    s.add_argument("--dept", default="")
    s = add("return", cmd_return, "返却")
    s.add_argument("key")
    s.add_argument("--note", default="")
    s = add("status", cmd_status, "状態変更")
    s.add_argument("key")
    s.add_argument("status")
    s.add_argument("--note", default="")
    s = add("dispose", cmd_dispose, "廃棄処理（管理者）")
    s.add_argument("key")
    s.add_argument("--method", required=True)
    s.add_argument("--erasure", required=True, help="データ消去の方法・証跡")
    s.add_argument("--cert", default="")
    s.add_argument("--vendor", default="")
    s = add("run", cmd_run, "スクリプト（Ansible）実行")
    s.add_argument("key")
    s.add_argument("playbook")
    s.add_argument("-e", "--extra", action="append", help="key=value")
    s.add_argument("--wait", action="store_true")
    add("jobs", cmd_jobs, "ジョブ一覧").add_argument("key", nargs="?")
    add("job", cmd_job, "ジョブの出力").add_argument("id", type=int)
    add("playbooks", cmd_playbooks, "実行できるプレイブック")
    s = add("rdp", cmd_rdp, ".rdpファイルを保存")
    s.add_argument("key")
    s.add_argument("--user", default="")
    s.add_argument("-o", "--output")
    s.add_argument("--open", action="store_true")
    add("remote", cmd_remote, "ブラウザ接続URL（Guacamole）").add_argument("key")
    add("budgets", cmd_budgets, "予算の執行状況")
    s = add("report", cmd_report, "償却レポート（CSV）")
    s.add_argument("kind", choices=["depreciation"])
    s.add_argument("--as-of")
    s = add("token", cmd_token, "APIトークン発行（管理者）")
    s.add_argument("name")
    s.add_argument("role", choices=["admin", "operator", "viewer", "enroll"])
    return p


def main(argv: list[str] | None = None, client: httpx.Client | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.token:
        print("KIT_TOKEN（または --token）を設定してください", file=sys.stderr)
        return 2
    try:
        print(args.fn(Api(args.url, args.token, client), args))
        return 0
    except CliError as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 1
    except httpx.HTTPError as e:
        print(f"接続エラー: {e}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
