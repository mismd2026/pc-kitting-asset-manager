"""Generate the per-pattern files for a recovery-image USB.

    kit-image validate                       # check every pattern
    kit-image build standard --out dist      # needs KIT_LOCAL_ADMIN_PASSWORD, KIT_ENROLL_TOKEN
    kit-image build standard --out dist --placeholder-secrets   # for review / CI

Output (copied onto the USB by imaging/New-KittingUsb.ps1):

    dist/<pattern>/
      deploy.cmd          run in WinPE: partition, apply WIM, drivers, boot files
      diskpart.txt        UEFI/GPT layout with a WinRE recovery partition
      unattend.xml        OOBE skipped, locale/time zone, temporary local admin
      SetupComplete.cmd   runs enroll.ps1 as SYSTEM before first logon
      enroll.ps1          rename, open WinRM HTTPS for the mgmt subnet, check in to asset DB
      manifest.json       SHA-256 of every file (build record for audit)

The enroll token baked into enroll.ps1 can ONLY call /kitting/checkin for serials
already registered in the asset DB; rotate it per batch.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from xml.etree import ElementTree

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from .pattern import NETBIOS_MAX, Pattern, PatternError, load, load_all

TEMPLATES = Path(__file__).parent / "templates"
FILES = [
    "deploy.cmd",
    "diskpart.txt",
    "list-disks.txt",
    "unattend.xml",
    "SetupComplete.cmd",
    "enroll.ps1",
]
PLACEHOLDER = "__SET_AT_BUILD__"


def unattend_password(password: str, kind: str = "Password") -> str:
    """Encoding Windows Setup expects with <PlainText>false</PlainText>.
    This is obfuscation, not encryption — the USB must be handled as a secret
    and the password is rotated by Windows LAPS after kitting."""
    return base64.b64encode((password + kind).encode("utf-16-le")).decode()


def _env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(TEMPLATES),
        undefined=StrictUndefined,
        keep_trailing_newline=True,
        autoescape=False,
    )
    env.filters["xml"] = lambda s: (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
    env.filters["ps"] = lambda s: str(s).replace("'", "''")  # PowerShell single-quoted string
    env.filters["win"] = lambda s: str(s).replace("/", "\\")
    return env


def render(
    pattern: Pattern, admin_password: str, enroll_token: str, admin_user: str = "kitadmin"
) -> dict[str, str]:
    ctx = {
        "p": pattern.data,
        "name": pattern.name,
        "admin_user": admin_user,
        "admin_password_b64": unattend_password(admin_password),
        "enroll_token": enroll_token,
        "netbios_max": NETBIOS_MAX,
        "built_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    env = _env()
    out = {}
    for name in FILES:
        text = env.get_template(f"{name}.j2").render(**ctx)
        if name.endswith((".cmd", ".txt")):
            text = text.replace("\r\n", "\n").replace("\n", "\r\n")  # cmd.exe / diskpart want CRLF
        out[name] = text
    ElementTree.fromstring(out["unattend.xml"].encode())  # fail the build on malformed XML
    return out


def write(pattern: Pattern, files: dict[str, str], out_dir: Path) -> Path:
    target = out_dir / pattern.name
    target.mkdir(parents=True, exist_ok=True)
    manifest = {
        "pattern": pattern.name,
        "description": pattern["description"],
        "image": pattern["image"],
        "files": {},
    }
    for name, text in files.items():
        data = text.encode("utf-8")
        (target / name).write_bytes(data)
        manifest["files"][name] = hashlib.sha256(data).hexdigest()
    (target / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return target


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="kit-image")
    ap.add_argument("--patterns-dir", default="imaging/patterns")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("validate")
    sub.add_parser("list")
    b = sub.add_parser("build")
    b.add_argument("pattern")
    b.add_argument("--out", default="dist")
    b.add_argument(
        "--placeholder-secrets",
        action="store_true",
        help="write placeholders instead of real secrets (for review/CI)",
    )
    a = ap.parse_args(argv)
    pdir = Path(a.patterns_dir)

    try:
        if a.cmd in ("validate", "list"):
            pats = load_all(pdir)
            for p in pats.values():
                print(f"{p.name:12s} {p['oobe']['hostname_prefix']:6s} {p['description']}")
            print(f"{len(pats)} pattern(s) OK" if a.cmd == "validate" else "")
            return 0
        pattern = load(pdir / f"{a.pattern}.yml")
    except (PatternError, FileNotFoundError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    if a.placeholder_secrets:
        password, token = PLACEHOLDER, PLACEHOLDER
    else:
        password = os.environ.get("KIT_LOCAL_ADMIN_PASSWORD", "")
        token = os.environ.get("KIT_ENROLL_TOKEN", "")
        if len(password) < 14 or not token:
            print(
                "KIT_LOCAL_ADMIN_PASSWORD (14+ chars) and KIT_ENROLL_TOKEN are required",
                file=sys.stderr,
            )
            return 2
    target = write(pattern, render(pattern, password, token), Path(a.out))
    print(f"built {pattern.name} -> {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
