from __future__ import annotations

import json
import logging
from collections import Counter, defaultdict
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import auth
from .config import Settings, load_settings
from .db import Database
from .status import LABELS, ORDER, RESULT_PT, compute
from .sync import Scheduler
from .timeutil import fmt_age, fmt_local, from_db, now_utc, parse_ts, to_db
from .views import KIND_LABEL, NO_CLIENT, Freshness, build_rows, resolve_client

log = logging.getLogger("veeam.web")
from .status import UI_LABEL  # noqa: E402
HERE = Path(__file__).resolve().parent


class Redirect(Exception):
    def __init__(self, url: str):
        self.url = url


def create_app(settings: Settings | None = None, start_scheduler: bool = True) -> FastAPI:
    settings = settings or load_settings()
    db = Database(settings.db_path)
    scheduler = Scheduler(db, settings)
    throttle = auth.LoginThrottle()

    @asynccontextmanager
    async def lifespan(app):
        if start_scheduler:
            scheduler.start()
        yield
        scheduler.stop()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(SessionMiddleware, secret_key=settings.secret_key, session_cookie="vm_session",
                       max_age=settings.session_hours * 3600, same_site="strict", https_only=False)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    tpl = Jinja2Templates(directory=HERE / "templates")
    tpl.env.filters.update(dt=lambda v, s=False: fmt_local(from_db(v) if isinstance(v, str) else v, s),
                           vdt=lambda v: fmt_local(parse_ts(v)), age=lambda v: fmt_age(from_db(v) if isinstance(v, str) else v),
                           q=lambda v: quote(str(v), safe=""), res=lambda v: RESULT_PT.get(v, v or "—"),
                           duration=_duration, runinfo=_runinfo, hours=_hours_txt)
    tpl.env.globals.update(LABELS=LABELS, KIND_LABEL=KIND_LABEL, settings=settings, UI=UI_LABEL)
    app.state.db, app.state.settings, app.state.scheduler = db, settings, scheduler

    @app.exception_handler(Redirect)
    async def _redir(request, exc: Redirect):
        return RedirectResponse(exc.url, status_code=303)

    from starlette.exceptions import HTTPException as StarletteHTTPException

    @app.exception_handler(StarletteHTTPException)
    async def _http_err(request, exc):
        if request.url.path.startswith("/api/") or request.url.path == "/healthz":
            return JSONResponse({"erro": exc.detail}, status_code=exc.status_code)
        return HTMLResponse(f"<!doctype html><meta charset=utf-8><link rel=stylesheet href=/static/app.css>"
                            f"<main><h1>Erro {exc.status_code}</h1><p>{_esc(exc.detail)}</p><p><a href=/>Voltar ao painel</a></p></main>",
                            status_code=exc.status_code)

    # ------------------------------------------------------------- helpers
    def user_of(request: Request, admin: bool = False) -> dict:
        uid = request.session.get("uid")
        user = auth.get_user(db, uid) if uid else None
        if not user:
            request.session.clear()
            raise Redirect("/login")
        if admin and user["role"] != "admin":
            raise HTTPException(403, "Apenas administradores.")
        return user

    def check_csrf(request: Request, token: str) -> None:
        if not token or token != request.session.get("csrf"):
            raise HTTPException(400, "Formulário expirado. Recarregue a página e tente de novo.")

    def render(request: Request, name: str, user: dict | None, **ctx):
        now = now_utc()
        fresh = Freshness(db, settings, now)
        if "csrf" not in request.session:
            request.session["csrf"] = auth.new_csrf()
        flash = request.session.pop("flash", None)
        return tpl.TemplateResponse(request, name, {
            "user": user, "fresh": fresh, "now": now, "csrf": request.session["csrf"], "flash": flash, **ctx})

    def flash(request: Request, msg: str, kind: str = "ok"):
        request.session["flash"] = {"msg": msg, "kind": kind}

    # ---------------------------------------------------------------- auth
    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request):
        with db.connect() as c:
            has_users = c.execute("SELECT COUNT(*) FROM users").fetchone()[0] > 0
        return render(request, "login.html", None, has_users=has_users)

    @app.post("/login")
    def login(request: Request, username: str = Form(""), password: str = Form(""), csrf: str = Form("")):
        check_csrf(request, csrf)
        ip = request.client.host if request.client else "?"
        if throttle.blocked(ip):
            flash(request, "Muitas tentativas. Aguarde 5 minutos.", "erro")
            return RedirectResponse("/login", 303)
        user = auth.authenticate(db, username, password)
        if not user:
            throttle.fail(ip)
            flash(request, "Usuário ou senha incorretos.", "erro")
            return RedirectResponse("/login", 303)
        throttle.ok(ip)
        request.session.clear()
        request.session.update(uid=user["id"], csrf=auth.new_csrf())
        db.audit(user["username"], "login", ip)
        return RedirectResponse("/", 303)

    @app.post("/logout")
    def logout(request: Request, csrf: str = Form("")):
        check_csrf(request, csrf)
        request.session.clear()
        return RedirectResponse("/login", 303)

    # -------------------------------------------------------------- painel
    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request):
        user = user_of(request)
        rows = build_rows(db, settings)
        counts = Counter(r.status.code for r in rows)
        by_client: dict[str, Counter] = defaultdict(Counter)
        worst: dict[str, int] = {}
        for r in rows:
            by_client[r.client][r.status.code] += 1
            worst[r.client] = min(worst.get(r.client, 99), r.status.rank)
        # clientes com problema primeiro (pior status), depois alfabético; "Sem cliente" por último no empate
        clients = sorted(by_client, key=lambda c: (worst[c], c == NO_CLIENT, c.lower()))
        running_jobs = [j for j in db.jobs() if j.get("running")]
        sections = {c: client_sections([r for r in rows if r.client == c]) for c in clients}
        return render(request, "dashboard.html", user, rows=rows, counts=counts, order=ORDER, sections=sections,
                      infra_alerts=infra_alerts(db.kv_get("infra", {}) or {}),
                      clients=clients, by_client=by_client, running_jobs=running_jobs,
                      problems=[r for r in rows if r.status.is_problem])

    @app.get("/objeto", response_class=HTMLResponse)
    def object_page(request: Request, key: str):
        user = user_of(request)
        o = db.object(key)
        if not o:
            raise HTTPException(404, "Objeto não encontrado no retrato atual do Veeam.")
        meta = db.object_meta().get(key)
        jm = db.job_meta().get(o.get("job_id") or "")
        st = compute(o, meta, jm, now_utc(), settings.sem_backup_days, settings.agent_interval_hours)
        client, csrc = resolve_client(o, meta, jm, db.client_rules())
        name_lc = key.split(":", 2)[2]
        tasks = db.tasks_for(o["job_id"], name_lc) if key.startswith("job:") else []
        rps = db.restore_points_for(o.get("backup_ids") or [], name_lc)
        job = db.job(o["job_id"]) if key.startswith("job:") else None
        return render(request, "object.html", user, o=o, meta=meta or {}, st=st, client=client,
                      client_source=csrc, tasks=tasks, rps=rps, job=job)

    @app.post("/objeto/meta")
    def object_meta(request: Request, key: str = Form(...), csrf: str = Form(""), client: str = Form(""),
                    note: str = Form(""), rpo_hours: str = Form(""), ignored: str = Form(""),
                    ignored_reason: str = Form("")):
        user = user_of(request, admin=True)
        check_csrf(request, csrf)
        if not db.object(key):
            raise HTTPException(404)
        rpo = _parse_rpo(rpo_hours)
        ign = ignored == "1"
        if ign and not ignored_reason.strip():
            flash(request, "Para ignorar um objeto é obrigatório informar o motivo.", "erro")
            return RedirectResponse(f"/objeto?key={quote(key, safe='')}", 303)
        old = db.object_meta().get(key) or {}
        now = to_db(now_utc())
        with db.connect() as c:
            c.execute("""INSERT INTO object_meta(key, client, note, rpo_hours, ignored, ignored_reason, ignored_by,
                         ignored_at, updated_by, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)
                         ON CONFLICT(key) DO UPDATE SET client=excluded.client, note=excluded.note,
                         rpo_hours=excluded.rpo_hours, ignored=excluded.ignored, ignored_reason=excluded.ignored_reason,
                         ignored_by=excluded.ignored_by, ignored_at=excluded.ignored_at,
                         updated_by=excluded.updated_by, updated_at=excluded.updated_at""",
                      (key, client.strip() or None, note.strip() or None, rpo, int(ign),
                       ignored_reason.strip() if ign else None,
                       (old.get("ignored_by") if old.get("ignored") else user["username"]) if ign else None,
                       (old.get("ignored_at") if old.get("ignored") else now) if ign else None,
                       user["username"], now))
        db.audit(user["username"], "editar objeto", key,
                 json.dumps({"cliente": client, "rpo": rpo, "ignorado": ign, "motivo": ignored_reason}, ensure_ascii=False))
        flash(request, "Alterações salvas.")
        return RedirectResponse(f"/objeto?key={quote(key, safe='')}", 303)

    # ---------------------------------------------------------------- jobs
    @app.get("/jobs", response_class=HTMLResponse)
    def jobs_page(request: Request):
        user = user_of(request)
        jobs = sorted(db.jobs(), key=lambda j: j["name"].lower())
        rows = build_rows(db, settings)
        per_job: dict[str, Counter] = defaultdict(Counter)
        for r in rows:
            per_job[r.obj.get("job_id")][r.status.code] += 1
        return render(request, "jobs.html", user, jobs=jobs, per_job=per_job, jmeta=db.job_meta())

    @app.get("/job", response_class=HTMLResponse)
    def job_page(request: Request, id: str):
        user = user_of(request)
        job = db.job(id)
        if not job:
            raise HTTPException(404, "Job não encontrado no retrato atual do Veeam.")
        rows = [r for r in build_rows(db, settings) if r.obj.get("job_id") == id and r.key.startswith("job:")]
        return render(request, "job.html", user, job=job, rows=rows, sessions=db.sessions_for_job(id),
                      jm=db.job_meta().get(id) or {})

    @app.post("/job/meta")
    def job_meta(request: Request, job_id: str = Form(...), csrf: str = Form(""), client: str = Form(""),
                 rpo_hours: str = Form("")):
        user = user_of(request, admin=True)
        check_csrf(request, csrf)
        rpo = _parse_rpo(rpo_hours)
        with db.connect() as c:
            c.execute("""INSERT INTO job_meta(job_id, client, rpo_hours, updated_by, updated_at) VALUES (?,?,?,?,?)
                         ON CONFLICT(job_id) DO UPDATE SET client=excluded.client, rpo_hours=excluded.rpo_hours,
                         updated_by=excluded.updated_by, updated_at=excluded.updated_at""",
                      (job_id, client.strip() or None, rpo, user["username"], to_db(now_utc())))
        db.audit(user["username"], "editar job", job_id, json.dumps({"cliente": client, "rpo": rpo}, ensure_ascii=False))
        flash(request, "Alterações do job salvas.")
        return RedirectResponse(f"/job?id={quote(job_id, safe='')}", 303)

    # --------------------------------------------------------------- infra
    @app.get("/infra", response_class=HTMLResponse)
    def infra_page(request: Request):
        user = user_of(request)
        return render(request, "infra.html", user, infra=db.kv_get("infra", {}) or {})

    # --------------------------------------------------------- diagnóstico
    @app.get("/diagnostico", response_class=HTMLResponse)
    def diag_page(request: Request):
        user = user_of(request)
        runs = db.last_runs(60)
        for r in runs:
            r["counts_d"] = json.loads(r["counts"]) if r.get("counts") else {}
            r["warnings_l"] = json.loads(r["warnings"]) if r.get("warnings") else []
            r["aux_d"] = json.loads(r["aux_errors"]) if r.get("aux_errors") else {}
        return render(request, "diag.html", user, runs=runs, warnings=db.kv_get("integrity_warnings", []) or [],
                      audit=db.audit_list(100) if user["role"] == "admin" else [])

    @app.post("/sincronizar")
    def sync_now(request: Request, csrf: str = Form("")):
        user = user_of(request)
        check_csrf(request, csrf)
        scheduler.trigger_now()
        db.audit(user["username"], "sincronizar agora")
        flash(request, "Sincronização solicitada. Acompanhe abaixo (a página atualiza sozinha).")
        return RedirectResponse("/diagnostico", 303)

    # ------------------------------------------------------------- clientes
    @app.get("/admin/clientes", response_class=HTMLResponse)
    def clients_page(request: Request):
        user = user_of(request, admin=True)
        rows = build_rows(db, settings)
        unmatched = sorted({(r.obj.get("job_name") or r.obj["name"]) for r in rows if r.client == NO_CLIENT})
        machines = Counter(r.client for r in rows)
        by_client: dict[str, list] = defaultdict(list)
        for rule in db.client_rules():
            by_client[rule["client"]].append(rule)
        return render(request, "clients.html", user, by_client=dict(sorted(by_client.items(), key=lambda kv: kv[0].lower())),
                      unmatched=unmatched, machines=machines)

    @app.post("/admin/clientes")
    def clients_post(request: Request, csrf: str = Form(""), action: str = Form(""), pattern: str = Form(""),
                     client: str = Form(""), rule_id: str = Form("")):
        user = user_of(request, admin=True)
        check_csrf(request, csrf)
        pattern, client = pattern.strip(), client.strip()
        with db.connect() as c:
            if action == "add":
                if not pattern or not client:
                    flash(request, "Informe o texto e o cliente.", "erro")
                    return RedirectResponse("/admin/clientes", 303)
                row = c.execute("SELECT id FROM client_rules WHERE lower(pattern)=lower(?)", (pattern,)).fetchone()
                if row:  # mesmo texto: só troca o cliente
                    c.execute("UPDATE client_rules SET client=? WHERE id=?", (client, row["id"]))
                else:
                    c.execute("INSERT INTO client_rules(pattern, client) VALUES (?,?)", (pattern, client))
                db.audit(user["username"], "regra cliente +", pattern, client)
                flash(request, f"\"{pattern}\" agora identifica {client}.")
            elif action == "delete" and rule_id.isdigit():
                c.execute("DELETE FROM client_rules WHERE id=?", (int(rule_id),))
                db.audit(user["username"], "regra cliente -", rule_id)
        return RedirectResponse("/admin/clientes", 303)

    # ------------------------------------------------------------- usuários
    @app.get("/admin/usuarios", response_class=HTMLResponse)
    def users_page(request: Request):
        user = user_of(request, admin=True)
        with db.connect() as c:
            users = [dict(r) for r in c.execute("SELECT id, username, role, active, created_at FROM users ORDER BY username")]
        return render(request, "users.html", user, users=users)

    @app.post("/admin/usuarios")
    def users_post(request: Request, csrf: str = Form(""), action: str = Form(""), username: str = Form(""),
                   password: str = Form(""), role: str = Form("tecnico")):
        user = user_of(request, admin=True)
        check_csrf(request, csrf)
        try:
            if action == "create":
                auth.create_user(db, username, password, role)
                db.audit(user["username"], "criar usuário", username, role)
                flash(request, f"Usuário {username} criado.")
            elif action == "password":
                auth.set_password(db, username, password)
                db.audit(user["username"], "trocar senha de", username)
                flash(request, f"Senha de {username} alterada.")
            elif action in ("disable", "enable"):
                if username.lower() == user["username"].lower():
                    raise ValueError("Você não pode desativar a si mesmo.")
                with db.connect() as c:
                    if action == "disable":
                        admins = c.execute("SELECT COUNT(*) FROM users WHERE role='admin' AND active=1 AND username<>?",
                                           (username,)).fetchone()[0]
                        if admins == 0:
                            raise ValueError("Precisa sobrar pelo menos um administrador ativo.")
                    c.execute("UPDATE users SET active=? WHERE username=?", (int(action == "enable"), username))
                db.audit(user["username"], f"{action} usuário", username)
        except ValueError as e:
            flash(request, str(e), "erro")
        return RedirectResponse("/admin/usuarios", 303)

    # ---------------------------------------------------------------- conta
    @app.get("/conta", response_class=HTMLResponse)
    def account_page(request: Request):
        return render(request, "account.html", user_of(request))

    @app.post("/conta")
    def account_post(request: Request, csrf: str = Form(""), current: str = Form(""), new: str = Form(""),
                     confirm: str = Form("")):
        user = user_of(request)
        check_csrf(request, csrf)
        if not auth.authenticate(db, user["username"], current):
            flash(request, "Senha atual incorreta.", "erro")
        elif new != confirm:
            flash(request, "A confirmação não confere.", "erro")
        else:
            try:
                auth.set_password(db, user["username"], new)
                db.audit(user["username"], "trocar a própria senha")
                flash(request, "Senha alterada.")
            except ValueError as e:
                flash(request, str(e), "erro")
        return RedirectResponse("/conta", 303)

    # ----------------------------------------------- saúde / integração
    @app.get("/healthz")
    def healthz():
        """200 se o retrato do Veeam está atualizado; 503 se não. Sem dados sensíveis.
        Serve para outro monitor (ex.: Zabbix) vigiar o próprio monitor."""
        f = Freshness(db, settings, now_utc())
        body = {"atualizado": not f.stale, "retrato_em": to_db(f.snapshot_at),
                "ultima_sync_falhou": f.last_failed}
        return JSONResponse(body, status_code=200 if not f.stale else 503)

    @app.get("/api/resumo")
    def api_summary(request: Request):
        user_of(request)
        rows = build_rows(db, settings)
        f = Freshness(db, settings, now_utc())
        return {
            "atualizado": not f.stale, "retrato_em": to_db(f.snapshot_at),
            "contagem": Counter(r.status.code for r in rows),
            "problemas": [{"cliente": r.client, "nome": r.obj["name"], "job": r.obj.get("job_name"),
                           "status": r.status.code, "motivo": r.status.reasons[:2]} for r in rows if r.status.is_problem],
        }

    return app


def client_sections(rows) -> list[dict]:
    """Separa as máquinas de um cliente: servidores por host ESXi, agentes, órfãos."""
    hosts: dict[str, list] = defaultdict(list)
    agents, orphans = [], []
    for r in rows:
        kind = r.obj.get("kind")
        if kind == "agent":
            agents.append(r)
        elif kind == "orphan":
            orphans.append(r)
        else:
            hosts[r.obj.get("host") or ""].append(r)
    out = []
    for h in sorted(hosts, key=lambda h: (min(x.status.rank for x in hosts[h]), h == "", h)):
        out.append({"kind": "esxi", "title": f"ESXi {h}" if h else "Servidores (host não identificado)",
                    "host": h, "rows": hosts[h]})
    if agents:
        out.append({"kind": "agent", "title": "Agentes (Veeam Agent)", "host": "", "rows": agents})
    if orphans:
        out.append({"kind": "orphan", "title": "Backups órfãos (job apagado)", "host": "", "rows": orphans})
    return out


def infra_alerts(infra: dict) -> list[str]:
    out = []
    cfg = infra.get("config_backup") or []
    if cfg and cfg[0].get("result") in ("Failed", "Warning"):
        out.append(f"Backup da configuração do Veeam: {RESULT_PT.get(cfg[0]['result'])} em "
                   f"{fmt_local(from_db(cfg[0].get('ended')))}" + (f" — {cfg[0]['message']}" if cfg[0].get("message") else ""))
    for r in infra.get("repositories") or []:
        cap, free = r.get("capacityGB"), r.get("freeGB")
        if r.get("isOnline") is False:
            out.append(f"Repositório {r.get('name')} está OFFLINE")
        elif isinstance(cap, (int, float)) and isinstance(free, (int, float)) and cap > 0 and free / cap < 0.10:
            out.append(f"Repositório {r.get('name')} com pouco espaço: {free:.0f} GB livres de {cap:.0f} GB ({free / cap:.1%})")
    for m in infra.get("managed_servers") or []:
        if m.get("status") and m.get("status") != "Available":
            out.append(f"Servidor gerenciado {m.get('name')} com status {m.get('status')}")
    for k, v in (infra.get("aux_errors") or {}).items():
        out.append(f"Não foi possível ler {k}: {v}")
    return out


def _size(n) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB", "MB") or n >= 100 else f"{n:.1f} {unit}".replace(".", ",")
        n /= 1024
    return ""


def _hms(d: str) -> str:
    """'09:48:28' ou '1.02:03:04' -> '9 h 48' / '26 h 03'."""
    try:
        days = 0
        if "." in d.split(":")[0]:
            dd, rest = d.split(".", 1)
            days, d = int(dd), rest
        h, m, _ = (int(x) for x in d.split(":"))
        h += days * 24
        return f"{h} h {m:02d}" if h else f"{m} min"
    except (ValueError, AttributeError):
        return d or ""


def _hours_txt(h) -> str:
    if not h:
        return "—"
    h = float(h)
    if h < 48:
        return f"{h:.0f} h"
    d = h / 24
    return f"{d:.0f} dias" if abs(d - round(d)) < 0.05 else f"{d:.1f} dias".replace(".", ",")


def _runinfo(obj: dict) -> str:
    """Texto de andamento da máquina no job em execução."""
    rt = obj.get("run_task")
    if obj.get("running") and rt:
        if rt.get("processed"):
            txt = f"{_size(rt['processed'])} lidos em {_hms(rt['duration'])}"
            if rt.get("rate") and rt["rate"] != "N/A":
                txt += f" · {rt['rate']}/s"
            if rt.get("bottleneck") and rt["bottleneck"] not in ("NotDefined", "None"):
                txt += f" · gargalo: {rt['bottleneck']}"
            return txt
        return f"Aguardando há {_hms(rt['duration'])}, nada lido ainda"
    return "Na fila do job, ainda não começou"


def _duration(sess: dict) -> str:
    a, b = parse_ts(sess.get("creationTime")), parse_ts(sess.get("endTime"))
    if not a:
        return "—"
    secs = int(((b or now_utc()) - a).total_seconds())
    if secs < 3600:
        txt = f"{secs // 60} min"
    elif secs < 48 * 3600:
        txt = f"{secs // 3600} h {secs % 3600 // 60:02d}"
    else:
        txt = f"{secs // 86400} dias {secs % 86400 // 3600} h"
    return txt if b else f"{txt} (rodando)"


def _esc(v) -> str:
    import html
    return html.escape(str(v))


def _parse_rpo(raw: str) -> float | None:
    raw = (raw or "").strip().replace(",", ".")
    if not raw:
        return None
    try:
        v = float(raw)
    except ValueError:
        raise HTTPException(400, "Intervalo inválido (informe horas, ex.: 24).")
    if v < 1 or v > 24 * 400:
        raise HTTPException(400, "O intervalo deve estar entre 1 e 9600 horas.")
    return v
