"""Coleta, paginação, sincronização tudo-ou-nada e camada web."""
import json
import re
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import auth
from app.config import Settings
from app.db import Database
from app.sync import run_sync
from app.veeam_client import IncompleteDataError, ReplaySource, VeeamClient
from app.views import Freshness, build_rows
from app.timeutil import now_utc

from builders import backup, job, rp, session, state, task


def write_replay(d: Path, jobs, states, sessions, tasks, backups, rps):
    files = {"jobs.json": jobs, "jobs_states.json": states, "sessions.json": sessions, "taskSessions.json": tasks,
             "backups.json": backups, "backupObjects.json": [], "restorePoints.json": rps,
             "backupInfrastructure_managedServers.json": [],
             "repositories_states.json": [{"name": "Repo1", "capacityGB": 1000, "freeGB": 50, "isOnline": True}]}
    d.mkdir(parents=True, exist_ok=True)
    for name, data in files.items():
        (d / name).write_text(json.dumps({"data": data, "pagination": {"total": len(data)}}))


def make_settings(tmp: Path, replay: Path) -> Settings:
    return Settings(app_host="127.0.0.1", app_port=8000, secret_key="x" * 40, session_hours=12,
                    db_path=tmp / "t.db", veeam_url="", veeam_user="", veeam_password="",
                    veeam_api_version="1.2-rev1", veeam_verify_ssl=False, veeam_timeout=30, veeam_page_size=200,
                    replay_dir=replay, sync_interval_minutes=10, history_days=30, rpo_vm_hours=36,
                    rpo_agent_hours=72)


@pytest.fixture
def env(tmp_path):
    j = job("Acrel", vms=["Srv-Dados", "Srv-Web"])
    b = backup(j)
    s = session(j, 5, 4)
    write_replay(tmp_path / "r", [j], [state(j)], [s], [task(s, "Srv-Dados", 5, 4), task(s, "Srv-Web", 5, 4)],
                 [b], [rp(b, "Srv-Dados", 4), rp(b, "Srv-Web", 4)])
    st = make_settings(tmp_path, tmp_path / "r")
    return st, Database(st.db_path), tmp_path / "r"


# ------------------------------------------------------------------ sync
def test_sync_ok_builds_snapshot(env):
    st, db, _ = env
    r = run_sync(db, st, trigger="teste")
    assert r["ok"], r
    rows = build_rows(db, st)
    assert {x.obj["name"] for x in rows} == {"Srv-Dados", "Srv-Web"}
    assert all(x.status.code == "ok" for x in rows)
    assert db.last_ok_run()["ok"] == 1


def test_failed_sync_keeps_previous_snapshot_and_records_error(env):
    st, db, replay = env
    assert run_sync(db, st)["ok"]
    before = sorted(o["key"] for o in db.objects())
    (replay / "restorePoints.json").unlink()           # endpoint essencial "fora do ar"
    r = run_sync(db, st)
    assert not r["ok"] and "restorePoints" in r["error"]
    assert sorted(o["key"] for o in db.objects()) == before
    last = db.last_runs(1)[0]
    assert last["ok"] == 0 and "restorePoints" in last["error"]


def test_incomplete_pagination_in_replay_fails_sync(env):
    st, db, replay = env
    body = json.loads((replay / "restorePoints.json").read_text())
    body["pagination"]["total"] = 999
    (replay / "restorePoints.json").write_text(json.dumps(body))
    r = run_sync(db, st)
    assert not r["ok"] and "IncompleteDataError" in r["error"]


def test_sessions_without_any_task_session_is_rejected(env):
    st, db, replay = env
    (replay / "taskSessions.json").write_text(json.dumps({"data": [], "pagination": {"total": 0}}))
    r = run_sync(db, st)
    assert not r["ok"] and "nenhuma task session" in r["error"]


def test_auxiliary_endpoint_failure_does_not_block(env):
    st, db, replay = env
    (replay / "repositories_states.json").unlink()
    r = run_sync(db, st)
    assert r["ok"]
    assert "repositories" in db.kv_get("infra")["aux_errors"]


def test_history_is_kept_and_window_is_authoritative(env):
    st, db, replay = env
    assert run_sync(db, st)["ok"]
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 2
    # uma task "some" da coleta atual (dentro da janela) -> sai do banco também
    body = json.loads((replay / "taskSessions.json").read_text())
    body["data"] = body["data"][:1]
    body["pagination"]["total"] = 1
    (replay / "taskSessions.json").write_text(json.dumps(body))
    assert run_sync(db, st)["ok"]
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1


def test_freshness_marks_stale(env):
    st, db, _ = env
    f = Freshness(db, st, now_utc())
    assert f.never and f.stale
    run_sync(db, st)
    assert not Freshness(db, st, now_utc()).stale
    from datetime import timedelta
    assert Freshness(db, st, now_utc() + timedelta(minutes=st.stale_after_minutes + 1)).stale


# ------------------------------------------------------- paginação HTTP real
class FakeResp:
    def __init__(self, body, code=200):
        self.status_code, self._b, self.text = code, body, json.dumps(body)

    def json(self):
        return self._b


class FakeSession:
    """Simula o Veeam: N itens, mas o total informado pode mentir."""

    def __init__(self, items, reported_total):
        self.items, self.total, self.calls = items, reported_total, 0

    def post(self, url, **kw):
        return FakeResp({"access_token": "t", "refresh_token": "r", "expires_in": 900})

    def get(self, url, params=None, **kw):
        self.calls += 1
        s, l = params["skip"], params["limit"]
        return FakeResp({"data": self.items[s:s + l], "pagination": {"total": self.total}})

    def close(self):
        pass


def _client(items, total, page=2):
    c = VeeamClient("https://x:9419", "u", "p", "1.2-rev1", False, 10, page_size=page)
    c._s = FakeSession(items, total)
    return c


def test_pagination_collects_everything():
    items = [{"id": str(i)} for i in range(7)]
    assert len(_client(items, 7).get_all("/api/v1/x")) == 7


def test_pagination_mismatch_raises(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    items = [{"id": str(i)} for i in range(5)]
    with pytest.raises(IncompleteDataError):
        _client(items, 9).get_all("/api/v1/x")


def test_http_error_is_explicit():
    c = _client([], 0)
    c._s.get = lambda url, **kw: FakeResp({"error": "boom"}, 500)
    with pytest.raises(Exception) as e:
        c.get_all("/api/v1/jobs")
    assert "HTTP 500" in str(e.value)


# ------------------------------------------------------------------- web
def _login(c, user, pw):
    tok = re.search(r'name="csrf" value="([^"]+)"', c.get("/login").text).group(1)
    c.post("/login", data={"username": user, "password": pw, "csrf": tok})
    return re.search(r'name="csrf" value="([^"]+)"', c.get("/conta").text).group(1)


@pytest.fixture
def web(env):
    from app.web import create_app
    st, db, _ = env
    run_sync(db, st)
    auth.create_user(db, "admin", "admin-12345", "admin")
    auth.create_user(db, "tec", "tec-123456", "tecnico")
    return TestClient(create_app(st, start_scheduler=False)), db


def test_login_required(web):
    c, _ = web
    r = c.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert c.get("/healthz").status_code == 200


def test_wrong_password(web):
    c, _ = web
    tok = re.search(r'name="csrf" value="([^"]+)"', c.get("/login").text).group(1)
    r = c.post("/login", data={"username": "admin", "password": "errada", "csrf": tok})
    assert "incorretos" in r.text


def test_pages_render(web):
    c, db = web
    _login(c, "admin", "admin-12345")
    for p in ["/", "/jobs", "/infra", "/diagnostico", "/admin/clientes", "/admin/usuarios", "/conta"]:
        assert c.get(p).status_code == 200, p
    for o in db.objects():
        assert c.get("/objeto", params={"key": o["key"]}).status_code == 200
    assert "Srv-Dados" in c.get("/").text
    assert "pouco espaço" in c.get("/").text      # repositório com 5% livre


def test_tecnico_cannot_edit(web):
    c, db = web
    tok = _login(c, "tec", "tec-123456")
    key = db.objects()[0]["key"]
    r = c.post("/objeto/meta", data={"key": key, "csrf": tok, "ignored": "1", "ignored_reason": "x"})
    assert r.status_code == 403
    assert c.get("/admin/usuarios").status_code == 403


def test_csrf_required(web):
    c, db = web
    _login(c, "admin", "admin-12345")
    r = c.post("/objeto/meta", data={"key": db.objects()[0]["key"], "csrf": "falso"})
    assert r.status_code == 400


def test_ignore_requires_reason_and_is_audited(web):
    c, db = web
    tok = _login(c, "admin", "admin-12345")
    key = db.objects()[0]["key"]
    c.post("/objeto/meta", data={"key": key, "csrf": tok, "ignored": "1", "ignored_reason": ""})
    assert not (db.object_meta().get(key) or {}).get("ignored")
    c.post("/objeto/meta", data={"key": key, "csrf": tok, "ignored": "1", "ignored_reason": "máquina desligada"})
    assert db.object_meta()[key]["ignored"] == 1
    assert any(a["action"] == "editar objeto" for a in db.audit_list())
    row = next(r for r in build_rows(db, c.app.state.settings) if r.key == key)
    assert row.status.code == "ignorado"


def test_admin_metadata_survives_sync(web):
    c, db = web
    tok = _login(c, "admin", "admin-12345")
    key = db.objects()[0]["key"]
    c.post("/objeto/meta", data={"key": key, "csrf": tok, "client": "Cliente X", "rpo_hours": "48"})
    run_sync(db, c.app.state.settings)
    row = next(r for r in build_rows(db, c.app.state.settings) if r.key == key)
    assert row.client == "Cliente X" and row.status.rpo_hours == 48


def test_stale_banner_when_sync_old(web, monkeypatch):
    c, db = web
    _login(c, "admin", "admin-12345")
    with db.connect() as conn:
        conn.execute("UPDATE kv SET value=? WHERE key='snapshot_at'", (json.dumps("2020-01-01T00:00:00+00:00"),))
    assert "DADOS DESATUALIZADOS" in c.get("/").text
    assert c.get("/healthz").status_code == 503


# ------------------------------------------------------- dump real (opcional)
REAL = Path(__file__).resolve().parent.parent / "data" / "replay_real"


@pytest.mark.skipif(not REAL.is_dir(), reason="sem dump real em data/replay_real")
def test_real_dump_invariants(tmp_path):
    """Com um dump real: todo ponto de restauração e toda VM da definição viram objeto."""
    st = make_settings(tmp_path, REAL)
    db = Database(st.db_path)
    r = run_sync(db, st)
    assert r["ok"], r
    keys = {(o["job_name"], o["name"].lower()) for o in db.objects()}
    rps = json.loads((REAL / "restorePoints.json").read_text())["data"]
    backups = {b["id"]: b for b in json.loads((REAL / "backups.json").read_text())["data"]}
    jobs = {j["id"]: j["name"] for j in json.loads((REAL / "jobs.json").read_text())["data"]}
    for p in rps:
        b = backups[p["backupId"]]
        owner = jobs.get(b["jobId"], b["name"])
        assert (owner, p["name"].lower()) in keys
