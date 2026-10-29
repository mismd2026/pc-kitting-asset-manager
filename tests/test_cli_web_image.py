from pathlib import Path
from xml.etree import ElementTree

import pytest
import yaml
from conftest import ROOT, new_asset

from assetdb import cli
from kitimage import build
from kitimage.pattern import PatternError, hostname_for, load, load_all, validate

PATTERNS = ROOT / "imaging" / "patterns"


# ------------------------------------------------------------------ CLI


def run_cli(client, token, *args, capsys):
    rc = cli.main(["--token", token, *args], client=client)
    out = capsys.readouterr()
    return rc, out.out, out.err


def test_cli_end_to_end(client, tokens, capsys, tmp_path):
    from assetdb.models import Role

    op = tokens[Role.OPERATOR]
    csv_file = tmp_path / "po.csv"
    csv_file.write_text(
        "S/N,機種,価格,納品日\nCLI-1,ThinkPad X1,280000,2026-06-01\n", encoding="utf-8"
    )
    rc, out, _ = run_cli(client, op, "import", str(csv_file), capsys=capsys)
    assert rc == 0 and "1 件登録しました" in out

    rc, out, _ = run_cli(
        client, op, "assign", "CLI-1", "佐藤 花子", "--dept", "経理部", capsys=capsys
    )
    assert rc == 0 and "佐藤 花子" in out

    rc, out, _ = run_cli(client, op, "ls", "--status", "稼働中", capsys=capsys)
    lines = out.splitlines()
    assert lines[0].startswith("資産番号") and "CLI-1" in out and "1 件" in out
    # columns stay aligned with full-width characters
    assert cli._w(lines[0][: lines[0].index("状態")]) == cli._w(
        lines[2][: lines[2].index("稼働中")]
    )

    rc, out, _ = run_cli(client, op, "show", "CLI-1", capsys=capsys)
    assert "割り当て先" in out and "経理部" in out

    rc, _, err = run_cli(
        client, op, "dispose", "CLI-1", "--method", "x", "--erasure", "y", capsys=capsys
    )
    assert rc == 1 and "403" in err  # operator cannot dispose

    rc, out, _ = run_cli(client, op, "report", "depreciation", capsys=capsys)
    assert out.startswith("資産番号,") and "CLI-1" in out


def test_cli_rdp_saves_file(client, tokens, capsys, tmp_path, as_op, as_enroll):
    from assetdb.models import Role

    new_asset(client, as_op)
    client.post(
        "/api/v1/kitting/checkin",
        headers=as_enroll,
        json={"serial": "SN-0001", "hostname": "PC-SN0001"},
    )
    target = tmp_path / "x.rdp"
    rc, out, _ = run_cli(
        client, tokens[Role.OPERATOR], "rdp", "PC-00001", "-o", str(target), capsys=capsys
    )
    assert rc == 0 and target.read_bytes().startswith(b"full address:s:PC-SN0001")


def test_cli_requires_token(capsys):
    assert cli.main(["--token", "", "ls"]) == 2


# ------------------------------------------------------------------ web UI


def test_web_login_and_csrf(client, tokens, as_op):
    from assetdb.models import Role

    new_asset(client, as_op)
    r = client.get("/ui/assets", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert client.post("/login", data={"token": tokens[Role.ENROLL]}).status_code == 401

    r = client.post("/login", data={"token": tokens[Role.OPERATOR]}, follow_redirects=False)
    assert "httponly" in r.headers["set-cookie"].lower()
    assert "samesite=strict" in r.headers["set-cookie"].lower()
    client.cookies.set("assetdb_token", tokens[Role.OPERATOR])
    page = client.get("/ui/assets").text
    assert "PC-00001" in page and "ThinkPad T14" in page

    # cross-site form post is refused
    r = client.post(
        "/ui/assets/PC-00001/assign",
        data={"assignee": "x"},
        headers={"origin": "https://evil.example"},
    )
    assert r.status_code == 403
    r = client.post(
        "/ui/assets/PC-00001/assign",
        data={"assignee": "鈴木", "department": "総務"},
        headers={"origin": "http://testserver"},
        follow_redirects=True,
    )
    assert "鈴木 に割り当てました" in r.text and "稼働中" in r.text


def test_web_shows_rule_errors(client, tokens, as_op):
    from assetdb.models import Role

    new_asset(client, as_op)
    client.cookies.set("assetdb_token", tokens[Role.OPERATOR])
    r = client.post(
        "/ui/assets/PC-00001/jobs",
        data={"playbook": "apps"},
        headers={"origin": "http://testserver"},
        follow_redirects=True,
    )
    assert "ホスト名が未登録" in r.text


# ------------------------------------------------------------------ image builder


def test_shipped_patterns_are_valid():
    pats = load_all(PATTERNS)
    assert set(pats) == {"standard", "developer", "kiosk"}


def test_pattern_validation_catches_mistakes(tmp_path):
    data = yaml.safe_load((PATTERNS / "standard.yml").read_text(encoding="utf-8"))
    data["oobe"]["hostname_prefix"] = "toolong-prefix"
    data["enroll"]["url"] = "http://insecure"
    data["image"]["wim"] = "../../secret.wim"
    data["ansible"]["apps_remove"] = ["googlechrome"]
    errors = " | ".join(validate(data, tmp_path / "standard.yml"))
    for expected in ("hostname_prefix", "https", "relative path", "overlap"):
        assert expected in errors
    bad = tmp_path / "other.yml"
    bad.write_text(yaml.safe_dump({**data, "name": "standard"}), encoding="utf-8")
    with pytest.raises(PatternError, match="file name"):
        load(bad)


@pytest.mark.parametrize(
    "prefix,serial,expected",
    [
        ("PC", "PF-4A2B9C", "PC-PF4A2B9C"),
        ("DEV", "MP2X-ABCDEFGHIJ", "DEV-XABCDEFGHIJ"),  # keep the tail, max 15 chars
        ("KSK", "", "KSK-UNKNOWN"),
    ],
)
def test_hostname_rule(prefix, serial, expected):
    name = hostname_for(prefix, serial)
    assert name == expected and len(name) <= 15


def test_build_outputs(tmp_path):
    pattern = load(PATTERNS / "standard.yml")
    files = build.render(pattern, "S3cure-Temp-Pass!", "enroll-token-123")
    target = build.write(pattern, files, tmp_path)

    unattend = ElementTree.fromstring(files["unattend.xml"].encode())
    ns = {"u": "urn:schemas-microsoft-com:unattend"}
    assert unattend.find(".//u:SystemLocale", ns).text == "ja-JP"
    pw = unattend.find(".//u:LocalAccount/u:Password", ns)
    assert pw.find("u:PlainText", ns).text == "false"
    assert "S3cure-Temp-Pass!" not in files["unattend.xml"]
    assert pw.find("u:Value", ns).text == build.unattend_password("S3cure-Temp-Pass!")

    assert "\r\n" in files["deploy.cmd"] and "/Index:1" in files["deploy.cmd"]
    assert 'set id="de94bba4-06d1-4d40-a16a-bfd50179d6ac"' in files["diskpart.txt"]
    assert "$Token     = 'enroll-token-123'" in files["enroll.ps1"]
    assert "-RemoteAddress $MgmtNet" in files["enroll.ps1"]
    manifest = (target / "manifest.json").read_text(encoding="utf-8")
    assert '"deploy.cmd"' in manifest and len(list(Path(target).iterdir())) == 7


def test_build_escapes_values(tmp_path):
    data = yaml.safe_load((PATTERNS / "kiosk.yml").read_text(encoding="utf-8"))
    data["oobe"]["organization"] = 'A&B <"Corp">'
    p = tmp_path / "kiosk.yml"
    p.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    files = build.render(load(p), "x" * 14, "it's")
    ElementTree.fromstring(files["unattend.xml"].encode())  # still well-formed
    assert "$Token     = 'it''s'" in files["enroll.ps1"]  # PowerShell quoting


def test_build_cli_requires_secrets(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("KIT_LOCAL_ADMIN_PASSWORD", raising=False)
    monkeypatch.delenv("KIT_ENROLL_TOKEN", raising=False)
    args = ["--patterns-dir", str(PATTERNS), "build", "standard", "--out", str(tmp_path)]
    assert build.main(args) == 2
    assert build.main([*args, "--placeholder-secrets"]) == 0
