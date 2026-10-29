"""Script Running: queue Ansible playbooks against a single asset and keep the output.

Safety rules:
* only playbooks that exist in ``ansible/playbooks`` and are listed in
  ``RUNNABLE`` can be requested — no arbitrary commands from the web/CLI;
* extra_vars must be a flat dict of simple scalars;
* the command is built as an argument list (no shell), targeted with --limit.

A separate worker process claims queued jobs (``SELECT ... FOR UPDATE SKIP LOCKED``
on PostgreSQL), so several workers can run in parallel without double execution.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Asset, AssetStatus, Job, JobStatus
from .services import RuleError, audit

# playbook name -> description shown in UI/CLI
RUNNABLE: dict[str, str] = {
    "site": "パターンに応じた全設定（キッティング）",
    "baseline": "基本設定（ホスト名・タイムゾーン・電源・Windows Update）",
    "security": "セキュリティ設定（BitLocker・Defender・ファイアウォール・ローカル管理者）",
    "apps": "アプリケーションの導入・更新",
    "collect_facts": "ハードウェア・OS情報の収集と資産DBへの反映",
    "windows_update": "Windows Update の即時適用",
}
_VAR_KEY = re.compile(r"^[a-z_][a-z0-9_]{0,40}$")


def validate_request(playbook: str, extra_vars: dict | None, ansible_dir: Path) -> dict:
    if playbook not in RUNNABLE:
        raise RuleError(f"実行できないプレイブックです: {playbook}")
    if not (ansible_dir / "playbooks" / f"{playbook}.yml").is_file():
        raise RuleError(f"プレイブックがありません: {playbook}.yml")
    extra_vars = extra_vars or {}
    for k, v in extra_vars.items():
        if not _VAR_KEY.match(k):
            raise RuleError(f"変数名が不正です: {k}")
        if not isinstance(v, str | int | float | bool) or (isinstance(v, str) and len(v) > 200):
            raise RuleError(f"変数 {k} の値は200文字以内の文字列・数値・真偽値にしてください")
    return extra_vars


def enqueue(
    session: Session,
    actor: str,
    asset: Asset,
    playbook: str,
    extra_vars: dict | None,
    ansible_dir: Path,
) -> Job:
    extra_vars = validate_request(playbook, extra_vars, ansible_dir)
    if not asset.hostname:
        raise RuleError(f"{asset.asset_tag} はホスト名が未登録です（キッティング前）")
    if asset.status in (AssetStatus.DISPOSED, AssetStatus.PENDING_DISPOSAL, AssetStatus.ORDERED):
        raise RuleError(f"{asset.asset_tag} は「{asset.status.value}」のため実行できません")
    running = session.scalar(
        select(Job.id).where(
            Job.asset_id == asset.id, Job.status.in_([JobStatus.QUEUED, JobStatus.RUNNING])
        )
    )
    if running:
        raise RuleError(f"{asset.asset_tag} では別のジョブ #{running} が実行待ち/実行中です")
    job = Job(asset=asset, playbook=playbook, extra_vars=extra_vars, requested_by=actor)
    session.add(job)
    session.flush()
    audit(session, actor, "job.enqueue", asset.asset_tag, job=job.id, playbook=playbook)
    return job


@dataclass
class RunResult:
    return_code: int
    output: str


Runner = Callable[[Job, Path], RunResult]


def build_command(job: Job, ansible_dir: Path) -> list[str]:
    return [
        "ansible-playbook",
        "-i",
        str(ansible_dir / "inventory" / "assetdb.py"),
        str(ansible_dir / "playbooks" / f"{job.playbook}.yml"),
        "--limit",
        job.asset.hostname,
        "-e",
        json.dumps(job.extra_vars or {}),
    ]


def ansible_runner(job: Job, ansible_dir: Path, timeout: int = 3600) -> RunResult:
    try:
        proc = subprocess.run(
            build_command(job, ansible_dir),
            cwd=ansible_dir,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=None,
        )
        return RunResult(proc.returncode, proc.stdout + proc.stderr)
    except subprocess.TimeoutExpired as e:
        return RunResult(124, f"timeout after {timeout}s\n{e.stdout or ''}")
    except FileNotFoundError:
        return RunResult(127, "ansible-playbook not found on worker")


def claim_next(session: Session) -> Job | None:
    stmt = (
        select(Job)
        .where(Job.status == JobStatus.QUEUED)
        .order_by(Job.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    job = session.scalar(stmt)
    if job is None:
        return None
    job.status = JobStatus.RUNNING
    job.started_at = datetime.now(UTC)
    session.commit()
    return job


def run_once(session: Session, runner: Runner, ansible_dir: Path, output_limit: int) -> Job | None:
    job = claim_next(session)
    if job is None:
        return None
    result = runner(job, ansible_dir)
    out = result.output
    if len(out) > output_limit:  # keep the tail: errors are at the end of Ansible output
        out = f"... ({len(out) - output_limit} chars truncated)\n" + out[-output_limit:]
    job.output = out
    job.return_code = result.return_code
    job.status = JobStatus.SUCCEEDED if result.return_code == 0 else JobStatus.FAILED
    job.finished_at = datetime.now(UTC)
    audit(session, "worker", "job.finish", job.asset.asset_tag, job=job.id, rc=result.return_code)
    session.commit()
    return job
