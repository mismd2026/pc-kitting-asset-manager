"""REST API (/api/v1) and app factory."""

from __future__ import annotations

import calendar
import csv
import io
from collections.abc import Callable
from datetime import date
from pathlib import Path

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from . import jobs as jobsvc
from . import services as svc
from .auth import Principal, authenticate, issue_token
from .config import Settings
from .db import Database
from .depreciation import fiscal_year
from .inventory import build_inventory
from .models import ApiToken, Asset, AssetStatus, AuditLog, Budget, Job, Role
from .remote import GuacamoleClient, rdp_file
from .schemas import (
    AssetIn,
    AssetOut,
    AssetPatch,
    AssignIn,
    BudgetIn,
    CheckinIn,
    DisposeIn,
    JobIn,
    JobOut,
    ReportIn,
    ReturnIn,
    StatusIn,
    TokenIn,
)


def job_out(j: Job, with_output: bool = False) -> JobOut:
    return JobOut(
        id=j.id,
        asset_tag=j.asset.asset_tag,
        playbook=j.playbook,
        status=j.status.value,
        requested_by=j.requested_by,
        return_code=j.return_code,
        created_at=j.created_at,
        started_at=j.started_at,
        finished_at=j.finished_at,
        output=j.output if with_output else None,
    )


def create_app(
    settings: Settings | None = None,
    db: Database | None = None,
    guacamole: GuacamoleClient | None = None,
    today: Callable[[], date] = date.today,
) -> FastAPI:
    settings = settings or Settings()
    db = db or Database(settings.database_url)
    db.create_all()
    if settings.bootstrap_admin_token:
        with db.SessionLocal() as s:
            if not s.scalar(select(ApiToken.id).where(ApiToken.role == Role.ADMIN)):
                issue_token(s, "bootstrap-admin", Role.ADMIN, settings.bootstrap_admin_token)
    if guacamole is None and settings.guac_url:
        guacamole = GuacamoleClient(
            settings.guac_url, settings.guac_user, settings.guac_password, settings.guac_datasource
        )

    app = FastAPI(title="PC Kitting & Asset Manager", version="0.1.0")
    app.state.settings = settings
    app.state.db = db
    app.state.today = today
    app.state.guacamole = guacamole

    @app.exception_handler(svc.RuleError)
    async def _rule(_req: Request, exc: svc.RuleError):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.exception_handler(LookupError)
    async def _missing(_req: Request, exc: LookupError):
        return JSONResponse({"detail": str(exc)}, status_code=404)

    @app.get("/healthz")
    def healthz() -> dict:
        return {"status": "ok"}

    app.include_router(build_router(), prefix="/api/v1")
    from .web import build_web  # noqa: PLC0415  (avoid import cycle)

    app.include_router(build_web())
    return app


# --------------------------------------------------------------------------- deps


def get_session(request: Request):
    yield from request.app.state.db.session()


def principal(request: Request, session: Session = Depends(get_session)) -> Principal:
    header = request.headers.get("Authorization", "")
    token = header.removeprefix("Bearer ").strip() if header.startswith("Bearer ") else ""
    token = token or request.cookies.get("assetdb_token", "")
    p = authenticate(session, token)
    if p is None:
        raise HTTPException(401, "認証が必要です", headers={"WWW-Authenticate": "Bearer"})
    return p


def require(role: Role):
    def dep(p: Principal = Depends(principal)) -> Principal:
        if not p.has(role):
            raise HTTPException(403, "この操作の権限がありません")
        return p

    return dep


def build_router() -> APIRouter:
    r = APIRouter()
    viewer, operator, admin = require(Role.VIEWER), require(Role.OPERATOR), require(Role.ADMIN)

    def out(request: Request, a: Asset) -> AssetOut:
        st = request.app.state
        return AssetOut.build(a, st.today(), st.settings.fiscal_year_start_month)

    # ------------------------------------------------------------ assets
    @r.get("/assets", response_model=list[AssetOut])
    def list_assets(
        request: Request,
        status: AssetStatus | None = None,
        category1: str | None = None,
        q: str | None = Query(None, description="資産番号・S/N・ホスト名・割当先・機種で検索"),
        limit: int = Query(200, le=1000),
        session: Session = Depends(get_session),
        _p: Principal = Depends(viewer),
    ):
        stmt = select(Asset).order_by(Asset.asset_tag).limit(limit)
        if status:
            stmt = stmt.where(Asset.status == status)
        if category1:
            stmt = stmt.where(Asset.category1 == category1)
        if q:
            like = f"%{q}%"
            stmt = stmt.where(
                or_(
                    Asset.asset_tag.ilike(like),
                    Asset.serial_number.ilike(like),
                    Asset.hostname.ilike(like),
                    Asset.assignee.ilike(like),
                    Asset.model_name.ilike(like),
                )
            )
        return [out(request, a) for a in session.scalars(stmt)]

    @r.post("/assets", response_model=AssetOut, status_code=201)
    def create_asset(
        request: Request,
        body: AssetIn,
        session: Session = Depends(get_session),
        p: Principal = Depends(operator),
    ):
        a = svc.register_asset(session, p.name, body.model_dump())
        session.commit()
        return out(request, a)

    @r.post("/assets/import")
    async def import_assets(
        request: Request, session: Session = Depends(get_session), p: Principal = Depends(operator)
    ):
        text = (await request.body()).decode("utf-8-sig")
        result = svc.import_csv(session, p.name, text)
        if result["errors"]:
            return JSONResponse(result, status_code=422)
        session.commit()
        return result

    @r.get("/assets/{key}", response_model=AssetOut)
    def show_asset(
        request: Request,
        key: str,
        session: Session = Depends(get_session),
        _p: Principal = Depends(viewer),
    ):
        return out(request, svc.get_asset(session, key))

    @r.patch("/assets/{key}", response_model=AssetOut)
    def patch_asset(
        request: Request,
        key: str,
        body: AssetPatch,
        session: Session = Depends(get_session),
        p: Principal = Depends(operator),
    ):
        a = svc.update_asset(
            session, p.name, svc.get_asset(session, key), body.model_dump(exclude_unset=True)
        )
        session.commit()
        return out(request, a)

    @r.get("/assets/{key}/history")
    def history(key: str, session: Session = Depends(get_session), _p=Depends(viewer)):
        a = svc.get_asset(session, key)
        return [
            {
                "at": h.at,
                "event": h.event,
                "from": h.from_status,
                "to": h.to_status,
                "actor": h.actor,
                "note": h.note,
            }
            for h in a.history
        ]

    @r.post("/assets/{key}/receive", response_model=AssetOut)
    def receive(
        request: Request,
        key: str,
        on: date | None = None,
        session: Session = Depends(get_session),
        p: Principal = Depends(operator),
    ):
        a = svc.receive(
            session, p.name, svc.get_asset(session, key), on or request.app.state.today()
        )
        session.commit()
        return out(request, a)

    @r.post("/assets/{key}/assign", response_model=AssetOut)
    def assign(
        request: Request,
        key: str,
        body: AssignIn,
        session: Session = Depends(get_session),
        p: Principal = Depends(operator),
    ):
        a = svc.assign(
            session,
            p.name,
            svc.get_asset(session, key),
            body.assignee,
            body.department,
            body.start or request.app.state.today(),
        )
        session.commit()
        return out(request, a)

    @r.post("/assets/{key}/return", response_model=AssetOut)
    def return_(
        request: Request,
        key: str,
        body: ReturnIn,
        session: Session = Depends(get_session),
        p: Principal = Depends(operator),
    ):
        a = svc.return_asset(
            session,
            p.name,
            svc.get_asset(session, key),
            body.on or request.app.state.today(),
            body.note,
        )
        session.commit()
        return out(request, a)

    @r.post("/assets/{key}/status", response_model=AssetOut)
    def status_(
        request: Request,
        key: str,
        body: StatusIn,
        session: Session = Depends(get_session),
        p: Principal = Depends(operator),
    ):
        a = svc.change_status(session, p.name, svc.get_asset(session, key), body.status, body.note)
        session.commit()
        return out(request, a)

    @r.post("/assets/{key}/dispose", response_model=AssetOut)
    def dispose(
        request: Request,
        key: str,
        body: DisposeIn,
        session: Session = Depends(get_session),
        p: Principal = Depends(admin),
    ):
        a = svc.dispose(
            session,
            p.name,
            svc.get_asset(session, key),
            method=body.method,
            data_erasure=body.data_erasure,
            on=body.on or request.app.state.today(),
            vendor=body.vendor,
            certificate=body.certificate,
        )
        session.commit()
        return out(request, a)

    # ------------------------------------------------------------ script running
    @r.get("/playbooks")
    def playbooks(_p=Depends(viewer)):
        return [{"name": k, "description": v} for k, v in jobsvc.RUNNABLE.items()]

    @r.post("/assets/{key}/jobs", response_model=JobOut, status_code=202)
    def run_job(
        request: Request,
        key: str,
        body: JobIn,
        session: Session = Depends(get_session),
        p: Principal = Depends(operator),
    ):
        job = jobsvc.enqueue(
            session,
            p.name,
            svc.get_asset(session, key),
            body.playbook,
            body.extra_vars,
            request.app.state.settings.ansible_dir,
        )
        session.commit()
        return job_out(job)

    @r.get("/jobs", response_model=list[JobOut])
    def list_jobs(
        asset: str | None = None,
        limit: int = Query(50, le=500),
        session: Session = Depends(get_session),
        _p=Depends(viewer),
    ):
        stmt = select(Job).order_by(Job.id.desc()).limit(limit)
        if asset:
            stmt = stmt.where(Job.asset_id == svc.get_asset(session, asset).id)
        return [job_out(j) for j in session.scalars(stmt)]

    @r.get("/jobs/{job_id}", response_model=JobOut)
    def show_job(job_id: int, session: Session = Depends(get_session), _p=Depends(viewer)):
        j = session.get(Job, job_id)
        if not j:
            raise HTTPException(404, "ジョブが見つかりません")
        return job_out(j, with_output=True)

    # ------------------------------------------------------------ remote desktop
    @r.get("/assets/{key}/rdp")
    def rdp(
        request: Request,
        key: str,
        username: str = "",
        session: Session = Depends(get_session),
        p: Principal = Depends(operator),
    ):
        a = svc.get_asset(session, key)
        body = rdp_file(a, request.app.state.settings.rdp_gateway, username)
        svc.audit(session, p.name, "remote.rdp_file", a.asset_tag)
        session.commit()
        return Response(
            body,
            media_type="application/x-rdp",
            headers={"Content-Disposition": f'attachment; filename="{a.asset_tag}.rdp"'},
        )

    @r.post("/assets/{key}/remote-session")
    def remote_session(
        request: Request,
        key: str,
        session: Session = Depends(get_session),
        p: Principal = Depends(operator),
    ):
        guac: GuacamoleClient | None = request.app.state.guacamole
        if guac is None:
            raise HTTPException(503, "Guacamole が設定されていません（GUAC_URL）")
        a = svc.get_asset(session, key)
        url = guac.open_url(a)
        svc.audit(session, p.name, "remote.guacamole", a.asset_tag)
        session.commit()
        return {"url": url}

    # ------------------------------------------------------------ kitting
    @r.post("/kitting/checkin", response_model=AssetOut)
    def checkin(
        request: Request,
        body: CheckinIn,
        session: Session = Depends(get_session),
        p: Principal = Depends(require(Role.ENROLL)),
    ):
        a = svc.kitting_checkin(
            session,
            p.name,
            serial=body.serial,
            hostname=body.hostname,
            ip=body.ip,
            mac=body.mac,
            pattern=body.pattern,
            os_build=body.os_build,
        )
        # queue the full configuration right away unless one is already pending
        try:
            with session.begin_nested():
                jobsvc.enqueue(
                    session, "kitting", a, "site", {}, request.app.state.settings.ansible_dir
                )
        except svc.RuleError:
            pass
        session.commit()
        return out(request, a)

    @r.post("/kitting/report", response_model=AssetOut)
    def report(
        request: Request,
        body: ReportIn,
        session: Session = Depends(get_session),
        p: Principal = Depends(operator),
    ):
        a = svc.kitting_report(
            session, p.name, svc.get_asset(session, body.serial), body.ok, body.summary
        )
        session.commit()
        return out(request, a)

    @r.get("/ansible/inventory")
    def inventory(session: Session = Depends(get_session), _p=Depends(operator)):
        return build_inventory(session)

    # ------------------------------------------------------------ budgets & reports
    @r.post("/budgets", status_code=201)
    def create_budget(
        body: BudgetIn, session: Session = Depends(get_session), p: Principal = Depends(admin)
    ):
        if session.scalar(select(Budget).where(Budget.code == body.code)):
            raise svc.RuleError(f"予算コード {body.code} は登録済みです")
        session.add(Budget(**body.model_dump()))
        svc.audit(session, p.name, "budget.create", body.code, amount=body.amount)
        session.commit()
        return body

    @r.get("/budgets")
    def budgets(session: Session = Depends(get_session), _p=Depends(viewer)):
        res = []
        for b in session.scalars(select(Budget).order_by(Budget.fiscal_year, Budget.code)):
            spent = svc.budget_spent(session, b)
            res.append(
                {
                    "code": b.code,
                    "name": b.name,
                    "fiscal_year": b.fiscal_year,
                    "amount": b.amount,
                    "spent": spent,
                    "remaining": b.amount - spent,
                    "assets": len(b.assets),
                }
            )
        return res

    @r.get("/reports/depreciation", response_class=PlainTextResponse)
    def depreciation_report(
        request: Request,
        as_of: date | None = None,
        session: Session = Depends(get_session),
        _p=Depends(viewer),
    ):
        """CSV for accounting: book value of every asset not yet disposed, as of a date
        (default: end of the current fiscal year)."""
        st = request.app.state
        fy_start = st.settings.fiscal_year_start_month
        if as_of is None:
            fy = fiscal_year(st.today(), fy_start)
            end_month = (fy_start - 2) % 12 + 1
            end_year = fy + 1 if fy_start > 1 else fy
            as_of = date(end_year, end_month, calendar.monthrange(end_year, end_month)[1])
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(
            [
                "資産番号",
                "S/N",
                "機種",
                "資産項目",
                "取得価額",
                "利用開始日",
                "償却累計額",
                "償却残価",
                "状態",
                "予算コード",
                "基準日",
            ]
        )
        total = 0
        stmt = select(Asset).where(Asset.status != AssetStatus.DISPOSED).order_by(Asset.asset_tag)
        for a in session.scalars(stmt):
            o = AssetOut.build(a, as_of, fy_start)
            total += o.book_value
            w.writerow(
                [
                    o.asset_tag,
                    o.serial_number,
                    o.model_name,
                    o.asset_item.value,
                    o.price,
                    o.usage_start or "",
                    o.accumulated_depreciation,
                    o.book_value,
                    o.status.value,
                    o.budget_code or "",
                    as_of,
                ]
            )
        w.writerow(["合計", "", "", "", "", "", "", total, "", "", as_of])
        return PlainTextResponse("﻿" + buf.getvalue(), media_type="text/csv; charset=utf-8")

    # ------------------------------------------------------------ admin
    @r.post("/tokens", status_code=201)
    def create_token(
        body: TokenIn, session: Session = Depends(get_session), p: Principal = Depends(admin)
    ):
        token = issue_token(session, body.name, Role(body.role))
        svc.audit(session, p.name, "token.issue", body.name, role=body.role)
        session.commit()
        return {
            "name": body.name,
            "role": body.role,
            "token": token,
            "note": "このトークンは再表示できません。安全な場所に保管してください",
        }

    @r.get("/audit")
    def audit_log(
        limit: int = Query(100, le=1000), session: Session = Depends(get_session), _p=Depends(admin)
    ):
        rows = session.scalars(select(AuditLog).order_by(AuditLog.id.desc()).limit(limit))
        return [
            {
                "at": x.at,
                "actor": x.actor,
                "action": x.action,
                "target": x.target,
                "detail": x.detail,
            }
            for x in rows
        ]

    return r


def ansible_dir_default() -> Path:
    return Path(__file__).resolve().parents[2] / "ansible"
