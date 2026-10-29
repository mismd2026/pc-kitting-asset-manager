from __future__ import annotations

import os
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from assetdb.api import create_app
from assetdb.auth import issue_token
from assetdb.config import Settings
from assetdb.db import Database
from assetdb.models import Base, Role

ROOT = Path(__file__).resolve().parents[1]
TODAY = date(2026, 10, 30)


class FakeGuac:
    def __init__(self):
        self.opened = []

    def open_url(self, asset):
        from assetdb.remote import _check

        _check(asset)
        asset.guac_connection_id = asset.guac_connection_id or "42"
        self.opened.append(asset.asset_tag)
        return f"https://guac.example/#/client/{asset.guac_connection_id}"


@pytest.fixture
def db():
    """SQLite by default; set ASSETDB_TEST_URL=postgresql+psycopg://... to run the
    whole suite against PostgreSQL (CI does both)."""
    d = Database(os.environ.get("ASSETDB_TEST_URL", "sqlite://"))
    Base.metadata.drop_all(d.engine)
    d.create_all()
    yield d
    d.engine.dispose()


@pytest.fixture
def tokens(db):
    with db.SessionLocal() as s:
        return {
            role: issue_token(s, f"{role.value}-user", role)
            for role in (Role.ADMIN, Role.OPERATOR, Role.VIEWER, Role.ENROLL)
        }


@pytest.fixture
def app(db, tokens):
    settings = Settings(database_url="sqlite://", ansible_dir=ROOT / "ansible")
    return create_app(settings, db, guacamole=FakeGuac(), today=lambda: TODAY)


@pytest.fixture
def client(app):
    return TestClient(app)


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def as_admin(tokens):
    return auth(tokens[Role.ADMIN])


@pytest.fixture
def as_op(tokens):
    return auth(tokens[Role.OPERATOR])


@pytest.fixture
def as_viewer(tokens):
    return auth(tokens[Role.VIEWER])


@pytest.fixture
def as_enroll(tokens):
    return auth(tokens[Role.ENROLL])


@pytest.fixture
def budget(client, as_admin):
    r = client.post(
        "/api/v1/budgets",
        headers=as_admin,
        json={
            "code": "IT-2026-PC",
            "name": "2026年度 PC更新",
            "fiscal_year": 2026,
            "amount": 1_000_000,
        },
    )
    assert r.status_code == 201
    return "IT-2026-PC"


def new_asset(client, headers, serial="SN-0001", **kw):
    body = {
        "serial_number": serial,
        "vendor": "サンプル商事",
        "order_date": "2026-04-01",
        "delivery_date": "2026-04-10",
        "price": 240_000,
        "category1": "ハードウェア",
        "category2": "PC",
        "category3": "ノート",
        "part_number": "21MC-CTO1WW",
        "model_name": "ThinkPad T14 Gen 5",
        "kitting_pattern": "standard",
        **kw,
    }
    r = client.post("/api/v1/assets", headers=headers, json=body)
    assert r.status_code == 201, r.text
    return r.json()
