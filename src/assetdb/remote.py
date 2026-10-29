"""Remote desktop: a downloadable .rdp file and browser access via Apache Guacamole.

Guacamole keeps the RDP port off the user's network path: the browser talks
HTTPS to Guacamole, Guacamole (guacd) talks RDP to the PC, and every session is
recorded in the audit log here.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass

import httpx

from .models import Asset, AssetStatus
from .services import RuleError

REMOTE_ALLOWED = {AssetStatus.IN_USE, AssetStatus.KITTING, AssetStatus.IN_STOCK, AssetStatus.REPAIR}


def _check(asset: Asset) -> None:
    if asset.status not in REMOTE_ALLOWED:
        raise RuleError(f"{asset.asset_tag} は「{asset.status.value}」のためリモート接続できません")
    if not (asset.hostname or asset.ip_address):
        raise RuleError(f"{asset.asset_tag} はホスト名/IPが未登録です")


def rdp_file(asset: Asset, gateway: str = "", username: str = "") -> str:
    """Content of a .rdp file. Windows expects CRLF line endings."""
    _check(asset)
    target = asset.hostname or asset.ip_address
    lines = [
        f"full address:s:{target}:{asset.rdp_port}",
        "prompt for credentials:i:1",
        "authentication level:i:2",  # refuse if the server certificate cannot be verified
        "enablecredsspsupport:i:1",  # Network Level Authentication
        "redirectclipboard:i:1",
        "redirectdrives:i:0",  # no local drive mapping: avoids data leaving via RDP
        "redirectprinters:i:0",
        "screen mode id:i:2",
        "use multimon:i:0",
    ]
    if username:
        lines.append(f"username:s:{username}")
    if gateway:
        lines += [
            f"gatewayhostname:s:{gateway}",
            "gatewayusagemethod:i:1",
            "gatewaycredentialssource:i:4",
            "gatewayprofileusagemethod:i:1",
        ]
    return "\r\n".join(lines) + "\r\n"


def client_url(base_url: str, connection_id: str, datasource: str) -> str:
    """Guacamole client URL: #/client/<base64("<id>\\0c\\0<datasource>")>."""
    ident = base64.b64encode(f"{connection_id}\0c\0{datasource}".encode()).decode().rstrip("=")
    return f"{base_url.rstrip('/')}/#/client/{ident}"


@dataclass
class GuacamoleClient:
    base_url: str
    username: str
    password: str
    datasource: str = "postgresql"
    http: httpx.Client | None = None

    def _client(self) -> httpx.Client:
        return self.http or httpx.Client(timeout=15)

    def _token(self, c: httpx.Client) -> str:
        r = c.post(
            f"{self.base_url}/api/tokens",
            data={"username": self.username, "password": self.password},
        )
        r.raise_for_status()
        return r.json()["authToken"]

    def ensure_connection(self, asset: Asset) -> str:
        """Create the RDP connection for this asset once; reuse its id afterwards."""
        if asset.guac_connection_id:
            return asset.guac_connection_id
        c = self._client()
        token = self._token(c)
        body = {
            "parentIdentifier": "ROOT",
            "name": f"{asset.asset_tag} {asset.hostname}",
            "protocol": "rdp",
            "parameters": {
                "hostname": asset.hostname or asset.ip_address,
                "port": str(asset.rdp_port),
                "security": "nla",
                "ignore-cert": "false",
                "enable-drive": "false",
                "disable-copy": "false",
                "recording-path": "${HISTORY_PATH}/${HISTORY_UUID}",  # session recording
                "create-recording-path": "true",
            },
            "attributes": {"max-connections": "1", "max-connections-per-user": "1"},
        }
        r = c.post(
            f"{self.base_url}/api/session/data/{self.datasource}/connections",
            params={"token": token},
            json=body,
        )
        r.raise_for_status()
        asset.guac_connection_id = str(r.json()["identifier"])
        return asset.guac_connection_id

    def open_url(self, asset: Asset) -> str:
        _check(asset)
        return client_url(self.base_url, self.ensure_connection(asset), self.datasource)
