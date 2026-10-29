import json

import httpx
from conftest import ROOT, new_asset

from assetdb.jobs import RunResult, build_command, run_once
from assetdb.models import Job
from assetdb.remote import GuacamoleClient, client_url, rdp_file

CHECKIN = {
    "serial": "SN-0001",
    "hostname": "PC-SN0001",
    "ip": "10.10.0.51",
    "mac": "AA:BB:CC:DD:EE:01",
    "pattern": "standard",
    "os_build": "26100",
}


def test_checkin_registers_endpoint_and_queues_full_kitting(client, as_op, as_enroll):
    new_asset(client, as_op)
    r = client.post("/api/v1/kitting/checkin", headers=as_enroll, json=CHECKIN)
    a = r.json()
    assert r.status_code == 200
    assert (a["status"], a["hostname"], a["ip_address"]) == (
        "キッティング中",
        "PC-SN0001",
        "10.10.0.51",
    )
    jobs = client.get("/api/v1/jobs", headers=as_op).json()
    assert [(j["playbook"], j["status"], j["requested_by"]) for j in jobs] == [
        ("site", "queued", "kitting")
    ]
    # a second check-in (e.g. PC rebooted) does not queue a duplicate run
    client.post("/api/v1/kitting/checkin", headers=as_enroll, json=CHECKIN)
    assert len(client.get("/api/v1/jobs", headers=as_op).json()) == 1


def test_unknown_device_is_refused_and_audited(client, as_enroll, as_admin):
    r = client.post(
        "/api/v1/kitting/checkin", headers=as_enroll, json={**CHECKIN, "serial": "ROGUE"}
    )
    assert r.status_code == 404
    audit = client.get("/api/v1/audit", headers=as_admin).json()
    assert audit[0]["action"] == "kitting.unknown_device" and audit[0]["target"] == "ROGUE"


def test_checkin_validates_netbios_hostname(client, as_op, as_enroll):
    new_asset(client, as_op)
    r = client.post(
        "/api/v1/kitting/checkin",
        headers=as_enroll,
        json={**CHECKIN, "hostname": "THIS-NAME-IS-TOO-LONG"},
    )
    assert r.status_code == 422


def test_report_moves_to_stock_or_in_use(client, as_op, as_enroll):
    new_asset(client, as_op)
    client.post("/api/v1/kitting/checkin", headers=as_enroll, json=CHECKIN)
    r = client.post(
        "/api/v1/kitting/report",
        headers=as_op,
        json={"serial": "SN-0001", "ok": False, "summary": "apps: timeout"},
    )
    assert r.json()["status"] == "キッティング中"
    r = client.post(
        "/api/v1/kitting/report",
        headers=as_op,
        json={"serial": "SN-0001", "ok": True, "summary": "pattern=standard"},
    )
    assert r.json()["status"] == "在庫"
    events = [
        h["event"] for h in client.get("/api/v1/assets/SN-0001/history", headers=as_op).json()
    ]
    assert events[-2:] == ["kitting_failed", "kitting_done"]


def test_inventory_groups_and_excludes_retired(client, as_op, as_enroll, as_admin):
    new_asset(client, as_op)
    new_asset(client, as_op, serial="SN-0002", kitting_pattern="kiosk")
    client.post("/api/v1/kitting/checkin", headers=as_enroll, json=CHECKIN)
    client.post(
        "/api/v1/kitting/checkin",
        headers=as_enroll,
        json={**CHECKIN, "serial": "SN-0002", "hostname": "KSK-SN0002", "pattern": "kiosk"},
    )
    client.post("/api/v1/assets/SN-0002/status", headers=as_op, json={"status": "修理中"})
    client.post("/api/v1/assets/SN-0002/status", headers=as_op, json={"status": "廃棄待ち"})
    inv = client.get("/api/v1/ansible/inventory", headers=as_op).json()
    assert inv["windows"]["hosts"] == ["PC-SN0001"]
    assert inv["pattern_standard"]["hosts"] == ["PC-SN0001"]
    assert "pattern_kiosk" not in inv
    hv = inv["_meta"]["hostvars"]["PC-SN0001"]
    assert hv["ansible_host"] == "10.10.0.51" and hv["serial_number"] == "SN-0001"


def test_job_whitelist_and_variable_validation(client, as_op, as_enroll):
    new_asset(client, as_op)
    # no hostname yet -> cannot target
    r = client.post("/api/v1/assets/SN-0001/jobs", headers=as_op, json={"playbook": "baseline"})
    assert r.status_code == 409 and "ホスト名" in r.json()["detail"]
    client.post("/api/v1/kitting/checkin", headers=as_enroll, json=CHECKIN)
    for bad in (
        {"playbook": "../../etc/passwd"},
        {"playbook": "shell"},
        {"playbook": "apps", "extra_vars": {"x; rm -rf /": 1}},
        {"playbook": "apps", "extra_vars": {"ok": {"nested": 1}}},
    ):
        assert (
            client.post("/api/v1/assets/SN-0001/jobs", headers=as_op, json=bad).status_code == 409
        ), bad
    # site already queued by check-in -> one job at a time per asset
    r = client.post("/api/v1/assets/SN-0001/jobs", headers=as_op, json={"playbook": "apps"})
    assert r.status_code == 409 and "実行待ち" in r.json()["detail"]


def test_worker_runs_job_and_keeps_output_tail(app, client, as_op, as_enroll, db):
    new_asset(client, as_op)
    client.post("/api/v1/kitting/checkin", headers=as_enroll, json=CHECKIN)
    calls = []

    def fake_runner(job, ansible_dir):
        calls.append(build_command(job, ansible_dir))
        return RunResult(2, "x" * 500 + "\nfatal: [PC-SN0001]: UNREACHABLE!")

    with db.SessionLocal() as s:
        job = run_once(s, fake_runner, ROOT / "ansible", output_limit=100)
        assert run_once(s, fake_runner, ROOT / "ansible", 100) is None  # queue empty
    assert job.status.value == "failed" and job.return_code == 2
    assert job.output.endswith("UNREACHABLE!") and "truncated" in job.output
    cmd = calls[0]
    assert cmd[0] == "ansible-playbook" and cmd[cmd.index("--limit") + 1] == "PC-SN0001"
    assert cmd[3].endswith("ansible/playbooks/site.yml") and cmd[2].endswith("assetdb.py")
    detail = client.get(f"/api/v1/jobs/{job.id}", headers=as_op).json()
    assert detail["status"] == "failed" and "UNREACHABLE" in detail["output"]


def test_rdp_file_and_browser_session(client, app, as_op, as_viewer, as_enroll, as_admin):
    new_asset(client, as_op)
    assert client.get("/api/v1/assets/SN-0001/rdp", headers=as_op).status_code == 409  # no host
    client.post("/api/v1/kitting/checkin", headers=as_enroll, json=CHECKIN)
    r = client.get("/api/v1/assets/SN-0001/rdp", headers=as_op, params={"username": "CORP\\ops"})
    assert r.headers["content-type"].startswith("application/x-rdp")
    assert 'filename="PC-00001.rdp"' in r.headers["content-disposition"]
    assert "full address:s:PC-SN0001:3389\r\n" in r.text and "redirectdrives:i:0" in r.text
    assert client.get("/api/v1/assets/SN-0001/rdp", headers=as_viewer).status_code == 403

    r = client.post("/api/v1/assets/SN-0001/remote-session", headers=as_op)
    assert r.json()["url"].startswith("https://guac.example/#/client/")
    actions = [x["action"] for x in client.get("/api/v1/audit", headers=as_admin).json()]
    assert {"remote.rdp_file", "remote.guacamole"} <= set(actions)


def test_guacamole_client_creates_connection_once():
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append((req.method, req.url.path))
        if req.url.path.endswith("/api/tokens"):
            return httpx.Response(200, json={"authToken": "T"})
        body = json.loads(req.content)
        assert req.url.params["token"] == "T"
        assert body["protocol"] == "rdp" and body["parameters"]["security"] == "nla"
        assert body["parameters"]["enable-drive"] == "false"
        return httpx.Response(200, json={"identifier": "17"})

    from assetdb.models import Asset, AssetStatus

    a = Asset(asset_tag="PC-1", serial_number="S", hostname="PC-S", status=AssetStatus.IN_USE)
    g = GuacamoleClient(
        "https://guac", "u", "p", http=httpx.Client(transport=httpx.MockTransport(handler))
    )
    url1, url2 = g.open_url(a), g.open_url(a)
    assert url1 == url2 == client_url("https://guac", "17", "postgresql")
    assert calls == [("POST", "/api/tokens"), ("POST", "/api/session/data/postgresql/connections")]


def test_rdp_gateway_options():
    from assetdb.models import Asset, AssetStatus

    a = Asset(
        asset_tag="PC-1",
        serial_number="S",
        hostname="PC-S",
        rdp_port=3389,
        status=AssetStatus.IN_USE,
    )
    text = rdp_file(a, gateway="rdgw.example.com")
    assert "gatewayhostname:s:rdgw.example.com" in text


def test_job_model_has_no_shell(app):
    # Defence in depth: the runner never goes through a shell
    import inspect

    from assetdb import jobs

    assert "shell=True" not in inspect.getsource(jobs)
    assert Job.__tablename__ == "jobs"
