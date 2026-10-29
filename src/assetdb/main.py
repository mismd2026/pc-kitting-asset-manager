"""Entrypoints.

uvicorn assetdb.main:app            # web UI + API
python -m assetdb.main worker       # job worker (runs ansible-playbook)
"""

from __future__ import annotations

import logging
import sys
import time

from .api import create_app
from .config import Settings
from .db import Database
from .jobs import ansible_runner, run_once

log = logging.getLogger("assetdb")


def worker(poll_seconds: float = 3.0) -> None:
    settings = Settings()
    db = Database(settings.database_url)
    db.create_all()
    log.info("worker started (ansible dir: %s)", settings.ansible_dir)
    while True:
        with db.SessionLocal() as s:
            job = run_once(s, ansible_runner, settings.ansible_dir, settings.job_output_limit)
        if job:
            log.info(
                "job #%s %s on %s -> %s", job.id, job.playbook, job.asset.hostname, job.status.value
            )
        else:
            time.sleep(poll_seconds)


def __getattr__(name: str):  # lazily build the ASGI app so `worker` doesn't need it
    if name == "app":
        return create_app()
    raise AttributeError(name)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if sys.argv[1:] == ["worker"]:
        worker()
    else:
        sys.exit("usage: python -m assetdb.main worker")
