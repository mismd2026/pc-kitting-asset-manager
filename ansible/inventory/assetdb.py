#!/usr/bin/env python3
"""Ansible dynamic inventory: fetches hosts from the asset DB API.

    ASSETDB_URL=https://assets.example.local ASSETDB_TOKEN=... ansible-inventory --graph

Only assets in a managed state (在庫/キッティング中/稼働中/修理中/返却済) are returned,
so retired hardware can never be targeted. Uses only the standard library so it
runs on any Ansible controller without extra packages.
"""

import json
import os
import ssl
import sys
import urllib.request


def fetch() -> dict:
    url = os.environ.get("ASSETDB_URL", "http://localhost:8000").rstrip("/")
    token = os.environ.get("ASSETDB_TOKEN", "")
    if not token:
        sys.exit("ASSETDB_TOKEN is not set")
    req = urllib.request.Request(
        f"{url}/api/v1/ansible/inventory", headers={"Authorization": f"Bearer {token}"}
    )
    ctx = ssl.create_default_context(cafile=os.environ.get("ASSETDB_CA_BUNDLE") or None)
    with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
        return json.load(resp)


def main(argv: list[str]) -> None:
    if "--host" in argv:
        # hostvars are delivered via _meta in --list, so per-host calls are not needed
        print("{}")
        return
    print(json.dumps(fetch()))


if __name__ == "__main__":
    main(sys.argv[1:])
