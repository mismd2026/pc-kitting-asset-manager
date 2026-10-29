"""Server-rendered web UI (Jinja2). Same rules as the API: every action goes
through services.* so history/audit are always written.

Auth: the API token is exchanged for an HttpOnly, SameSite=Strict cookie.
POSTs additionally require a same-origin Origin/Referer header (CSRF guard).
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from urllib.parse import quote, urlsplit

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from . import jobs as jobsvc
from . import services as svc
from .api import get_session, job_out
from .auth import Principal, authenticate
from .models import Asset, AssetStatus, Budget, Job, Role
from .schemas import AssetOut

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
templates.env.filters["yen"] = lambda v: f"{v:,}円" if v is not None else ""
templates.env.filters["d"] = lambda v: v.strftime("%Y-%m-%d") if v else "—"
templates.env.filters["dt"] = lambda v: v.strftime("%Y-%m-%d %H:%M") if v else "—"


class LoginRequired(Exception):
    pass


def ui_principal(request: Request, session: Session = Depends(get_session)) -> Principal:
    p = authenticate(session, request.cookies.get("assetdb_token", ""))
    if p is None or p.role == Role.ENROLL:
        raise HTTPException(303, headers={"Location": "/login"})
    return p


def same_origin(request: Request) -> None:
    origin = request.headers.get("origin") or request.headers.get("referer") or ""
    if urlsplit(origin).netloc != request.url.netloc:
        raise HTTPException(403, "不正なリクエスト元です")


def _back(key: str, msg: str = "", err: str = "") -> RedirectResponse:
    q = f"?msg={quote(msg)}" if msg else (f"?err={quote(err)}" if err else "")
    return RedirectResponse(f"/ui/assets/{quote(key)}{q}", status_code=303)


def build_web() -> APIRouter:
    r = APIRouter(include_in_schema=False)

    def ctx(request: Request, p: Principal | None = None, **kw) -> dict:
        return {
            "request": request,
            "me": p,
            "msg": request.query_params.get("msg"),
            "err": request.query_params.get("err"),
            **kw,
        }

    @r.get("/")
    def root():
        return RedirectResponse("/ui/assets")

    @r.get("/login", response_class=HTMLResponse)
    def login_form(request: Request):
        return templates.TemplateResponse(request, "login.html", ctx(request))

    @r.post("/login")
    def login(request: Request, token: str = Form(...), session: Session = Depends(get_session)):
        p = authenticate(session, token.strip())
        if p is None or p.role == Role.ENROLL:
            return templates.TemplateResponse(
                request,
                "login.html",
                ctx(request, err="トークンが正しくありません"),
                status_code=401,
            )
        resp = RedirectResponse("/ui/assets", status_code=303)
        resp.set_cookie(
            "assetdb_token",
            token.strip(),
            httponly=True,
            samesite="strict",
            secure=request.url.scheme == "https",
            max_age=8 * 3600,
        )
        return resp

    @r.post("/logout")
    def logout():
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie("assetdb_token")
        return resp

    @r.get("/ui/assets", response_class=HTMLResponse)
    def assets(
        request: Request,
        status: str = "",
        q: str = "",
        session: Session = Depends(get_session),
        p: Principal = Depends(ui_principal),
    ):
        st = request.app.state
        stmt = select(Asset).order_by(Asset.asset_tag).limit(500)
        if status:
            stmt = stmt.where(Asset.status == AssetStatus(status))
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
        rows = [
            AssetOut.build(a, st.today(), st.settings.fiscal_year_start_month)
            for a in session.scalars(stmt)
        ]
        counts = dict(
            session.execute(select(Asset.status, func.count()).group_by(Asset.status)).all()
        )
        return templates.TemplateResponse(
            request,
            "assets.html",
            ctx(
                request,
                p,
                rows=rows,
                statuses=list(AssetStatus),
                status=status,
                q=q,
                counts={s.value: counts.get(s, 0) for s in AssetStatus},
                total_book=sum(x.book_value for x in rows if x.status != AssetStatus.DISPOSED),
            ),
        )

    @r.get("/ui/assets/{key}", response_class=HTMLResponse)
    def asset_detail(
        request: Request,
        key: str,
        session: Session = Depends(get_session),
        p: Principal = Depends(ui_principal),
    ):
        st = request.app.state
        try:
            a = svc.get_asset(session, key)
        except LookupError as e:
            raise HTTPException(404, str(e)) from e
        return templates.TemplateResponse(
            request,
            "asset.html",
            ctx(
                request,
                p,
                a=AssetOut.build(a, st.today(), st.settings.fiscal_year_start_month),
                history=list(reversed(a.history)),
                jobs=[job_out(j) for j in a.jobs[:10]],
                playbooks=jobsvc.RUNNABLE,
                statuses=[
                    s
                    for s in AssetStatus
                    if s not in (AssetStatus.IN_USE, AssetStatus.RETURNED, AssetStatus.DISPOSED)
                ],
                can_operate=p.has(Role.OPERATOR),
                is_admin=p.has(Role.ADMIN),
                guac=st.guacamole is not None,
                today=st.today(),
            ),
        )

    def _action(request: Request, key: str, session: Session, p: Principal, fn, ok: str):
        same_origin(request)
        if not p.has(Role.OPERATOR):
            return _back(key, err="この操作の権限がありません")
        try:
            a = svc.get_asset(session, key)
            fn(a)
            session.commit()
            return _back(a.asset_tag, msg=ok)
        except (svc.RuleError, LookupError, ValueError) as e:
            session.rollback()
            return _back(key, err=str(e))

    @r.post("/ui/assets/{key}/assign")
    def ui_assign(
        request: Request,
        key: str,
        assignee: str = Form(...),
        department: str = Form(""),
        session: Session = Depends(get_session),
        p: Principal = Depends(ui_principal),
    ):
        return _action(
            request,
            key,
            session,
            p,
            lambda a: svc.assign(
                session, p.name, a, assignee, department, request.app.state.today()
            ),
            f"{assignee} に割り当てました",
        )

    @r.post("/ui/assets/{key}/return")
    def ui_return(
        request: Request,
        key: str,
        note: str = Form(""),
        session: Session = Depends(get_session),
        p: Principal = Depends(ui_principal),
    ):
        return _action(
            request,
            key,
            session,
            p,
            lambda a: svc.return_asset(session, p.name, a, request.app.state.today(), note),
            "返却を登録しました",
        )

    @r.post("/ui/assets/{key}/status")
    def ui_status(
        request: Request,
        key: str,
        status: str = Form(...),
        note: str = Form(""),
        session: Session = Depends(get_session),
        p: Principal = Depends(ui_principal),
    ):
        return _action(
            request,
            key,
            session,
            p,
            lambda a: svc.change_status(session, p.name, a, AssetStatus(status), note),
            f"状態を「{status}」にしました",
        )

    @r.post("/ui/assets/{key}/jobs")
    def ui_job(
        request: Request,
        key: str,
        playbook: str = Form(...),
        session: Session = Depends(get_session),
        p: Principal = Depends(ui_principal),
    ):
        return _action(
            request,
            key,
            session,
            p,
            lambda a: jobsvc.enqueue(
                session, p.name, a, playbook, {}, request.app.state.settings.ansible_dir
            ),
            f"{playbook} を実行キューに登録しました",
        )

    @r.post("/ui/assets/{key}/dispose")
    def ui_dispose(
        request: Request,
        key: str,
        method: str = Form(...),
        data_erasure: str = Form(...),
        certificate: str = Form(""),
        vendor: str = Form(""),
        session: Session = Depends(get_session),
        p: Principal = Depends(ui_principal),
    ):
        if not p.has(Role.ADMIN):
            return _back(key, err="廃棄処理は管理者のみ実行できます")
        return _action(
            request,
            key,
            session,
            p,
            lambda a: svc.dispose(
                session,
                p.name,
                a,
                method=method,
                data_erasure=data_erasure,
                vendor=vendor,
                certificate=certificate,
                on=request.app.state.today(),
            ),
            "廃棄処理を記録しました",
        )

    @r.post("/ui/assets/{key}/remote")
    def ui_remote(
        request: Request,
        key: str,
        session: Session = Depends(get_session),
        p: Principal = Depends(ui_principal),
    ):
        same_origin(request)
        guac = request.app.state.guacamole
        if not p.has(Role.OPERATOR) or guac is None:
            return _back(key, err="リモート接続は利用できません")
        try:
            a = svc.get_asset(session, key)
            url = guac.open_url(a)
            svc.audit(session, p.name, "remote.guacamole", a.asset_tag)
            session.commit()
        except (svc.RuleError, LookupError) as e:
            return _back(key, err=str(e))
        return RedirectResponse(url, status_code=303)

    @r.get("/ui/jobs/{job_id}", response_class=HTMLResponse)
    def ui_job_detail(
        request: Request,
        job_id: int,
        session: Session = Depends(get_session),
        p: Principal = Depends(ui_principal),
    ):
        j = session.get(Job, job_id)
        if not j:
            raise HTTPException(404)
        return templates.TemplateResponse(
            request, "job.html", ctx(request, p, j=job_out(j, with_output=True))
        )

    @r.get("/ui/budgets", response_class=HTMLResponse)
    def ui_budgets(
        request: Request,
        session: Session = Depends(get_session),
        p: Principal = Depends(ui_principal),
    ):
        rows = []
        for b in session.scalars(select(Budget).order_by(Budget.fiscal_year.desc(), Budget.code)):
            spent = svc.budget_spent(session, b)
            rows.append(
                {
                    "b": b,
                    "spent": spent,
                    "remaining": b.amount - spent,
                    "pct": round(spent * 100 / b.amount) if b.amount else 0,
                }
            )
        return templates.TemplateResponse(
            request, "budgets.html", ctx(request, p, rows=rows, today=date.today())
        )

    return r
