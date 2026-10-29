from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


@dataclass(frozen=True)
class Settings:
    database_url: str = field(
        default_factory=lambda: _env("ASSETDB_URL", "sqlite:///./assetdb.sqlite")
    )
    ansible_dir: Path = field(
        default_factory=lambda: Path(_env("ANSIBLE_DIR", str(Path.cwd() / "ansible")))
    )
    fiscal_year_start_month: int = field(
        default_factory=lambda: int(_env("FISCAL_YEAR_START_MONTH", "4"))
    )
    # Remote desktop via Apache Guacamole (optional)
    guac_url: str = field(default_factory=lambda: _env("GUAC_URL"))
    guac_user: str = field(default_factory=lambda: _env("GUAC_USER"))
    guac_password: str = field(default_factory=lambda: _env("GUAC_PASSWORD"))
    guac_datasource: str = field(default_factory=lambda: _env("GUAC_DATASOURCE", "postgresql"))
    rdp_gateway: str = field(default_factory=lambda: _env("RDP_GATEWAY"))
    # Created on first start if no admin token exists yet (store it in a secret manager)
    bootstrap_admin_token: str = field(default_factory=lambda: _env("BOOTSTRAP_ADMIN_TOKEN"))
    job_output_limit: int = 200_000
