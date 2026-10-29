"""Kitting pattern definitions (imaging/patterns/*.yml).

One YAML per pattern is the single source of truth for both the image USB
(this package) and the Ansible configuration (ansible/playbooks/tasks/load_pattern.yml).
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import yaml

NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{1,30}$")
PREFIX_RE = re.compile(r"^[A-Z][A-Z0-9]{0,5}$")
APP_RE = re.compile(r"^[a-z0-9][a-z0-9.+_-]{0,80}$")
LOCALE_RE = re.compile(r"^[a-z]{2}-[A-Z]{2}$")
NETBIOS_MAX = 15


class PatternError(ValueError):
    pass


@dataclass(frozen=True)
class Pattern:
    path: Path
    data: dict

    @property
    def name(self) -> str:
        return self.data["name"]

    def __getitem__(self, key: str):
        return self.data[key]


def _require(d: dict, dotted: str):
    cur = d
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            raise PatternError(f"missing required key: {dotted}")
        cur = cur[part]
    return cur


def _safe_rel(p: str, key: str) -> None:
    pp = PurePosixPath(p.replace("\\", "/"))
    if pp.is_absolute() or ".." in pp.parts or ":" in p:
        raise PatternError(f"{key} must be a relative path inside the USB: {p}")


def validate(data: dict, path: Path) -> list[str]:
    errors: list[str] = []

    def check(cond: bool, msg: str) -> None:
        if not cond:
            errors.append(msg)

    try:
        for key in (
            "name",
            "description",
            "image.wim",
            "image.index",
            "oobe.locale",
            "oobe.timezone",
            "oobe.hostname_prefix",
            "enroll.url",
            "enroll.management_subnet",
            "ansible",
        ):
            _require(data, key)
    except PatternError as e:
        return [str(e)]

    name = data["name"]
    check(bool(NAME_RE.match(name)), f"name must match {NAME_RE.pattern}: {name}")
    check(path.stem == name, f"file name ({path.stem}) must equal pattern name ({name})")
    img, oobe, enroll, ans = data["image"], data["oobe"], data["enroll"], data["ansible"]
    for key in ("wim", "drivers"):
        if img.get(key):
            try:
                _safe_rel(img[key], f"image.{key}")
            except PatternError as e:
                errors.append(str(e))
    check(isinstance(img["index"], int) and img["index"] >= 1, "image.index must be >= 1")
    check(
        bool(PREFIX_RE.match(oobe["hostname_prefix"])),
        "oobe.hostname_prefix must be 1-6 upper-case letters/digits",
    )
    check(bool(LOCALE_RE.match(oobe["locale"])), f"oobe.locale looks wrong: {oobe['locale']}")
    check(enroll["url"].startswith("https://"), "enroll.url must use https")
    try:
        ipaddress.ip_network(enroll["management_subnet"])
    except ValueError:
        errors.append(f"enroll.management_subnet is not a network: {enroll['management_subnet']}")
    if ans.get("join_domain"):
        check(
            bool(ans.get("domain")) and bool(ans.get("ou")),
            "ansible.domain and ansible.ou are required when join_domain is true",
        )
    for app in ans.get("apps", []) + ans.get("apps_remove", []):
        check(bool(APP_RE.match(str(app))), f"invalid package name: {app}")
    overlap = set(ans.get("apps", [])) & set(ans.get("apps_remove", []))
    check(not overlap, f"apps and apps_remove overlap: {sorted(overlap)}")
    disk = img.get("disk", {})
    check(
        int(disk.get("recovery_partition_mb", 1024)) >= 990,
        "image.disk.recovery_partition_mb must be >= 990 (WinRE needs it)",
    )
    return errors


def load(path: Path) -> Pattern:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise PatternError(f"{path}: not a mapping")
    errors = validate(data, path)
    if errors:
        raise PatternError(f"{path.name}: " + "; ".join(errors))
    return Pattern(path, data)


def load_all(directory: Path) -> dict[str, Pattern]:
    return {p.stem: load(p) for p in sorted(directory.glob("*.yml"))}


def hostname_for(prefix: str, serial: str) -> str:
    """PREFIX-<tail of serial>, max 15 chars (NetBIOS). Same rule as enroll.ps1."""
    clean = re.sub(r"[^A-Za-z0-9]", "", serial).upper() or "UNKNOWN"
    room = NETBIOS_MAX - len(prefix) - 1
    return f"{prefix}-{clean[-room:]}"
