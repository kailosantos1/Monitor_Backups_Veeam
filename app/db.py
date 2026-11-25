"""SQLite (stdlib). Uma conexão por operação; WAL para leitura concorrente."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('admin','tecnico')),
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sync_runs (
    id INTEGER PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    ok INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    duration REAL,
    counts TEXT,
    warnings TEXT,
    aux_errors TEXT,
    request_count INTEGER,
    task_source TEXT,
    trigger TEXT
);
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    job_id TEXT,
    session_type TEXT,
    created TEXT,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_sessions_job ON sessions(job_id, created);
CREATE INDEX IF NOT EXISTS ix_sessions_created ON sessions(created);
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    session_id TEXT,
    name_lc TEXT,
    created TEXT,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_tasks_session ON tasks(session_id);
CREATE INDEX IF NOT EXISTS ix_tasks_created ON tasks(created);
CREATE TABLE IF NOT EXISTS snap_objects (
    key TEXT PRIMARY KEY,
    job_id TEXT,
    data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS snap_jobs (
    job_id TEXT PRIMARY KEY,
    data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS snap_restore_points (
    id TEXT PRIMARY KEY,
    backup_id TEXT,
    name_lc TEXT,
    created TEXT,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_rp_obj ON snap_restore_points(backup_id, name_lc);
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS object_meta (
    key TEXT PRIMARY KEY,
    client TEXT,
    note TEXT,
    rpo_hours REAL,
    ignored INTEGER NOT NULL DEFAULT 0,
    ignored_reason TEXT,
    ignored_by TEXT,
    ignored_at TEXT,
    updated_by TEXT,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS job_meta (
    job_id TEXT PRIMARY KEY,
    client TEXT,
    rpo_hours REAL,
    updated_by TEXT,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS client_rules (
    id INTEGER PRIMARY KEY,
    pattern TEXT NOT NULL,
    client TEXT NOT NULL,
    position INTEGER NOT NULL DEFAULT 100
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    username TEXT,
    action TEXT NOT NULL,
    target TEXT,
    detail TEXT
);
"""

# Regras iniciais de cliente (texto contido no nome do job/backup/máquina).
# Editáveis pela tela de administração; servem só para agrupar a exibição.
DEFAULT_RULES = [
    ("cbhidro", "CBHidro"), ("cbhiidro", "CBHidro"), ("construnivel", "Construnível"), ("c-nivel", "Construnível"),
    ("laticinio", "Laticínios Oliveira"), ("enerview", "Enerview"), ("voltaic", "Voltaic"),
    ("hacker", "Hacker"), ("compasi", "Compasi"), ("acrel", "Acrel"), ("ronnau", "Ronnau"),
    ("systemup", "SystemUp"), ("amazon", "Amazon"), ("american-frigo", "American Frigo"),
    ("crescimentum", "Crescimentum"), ("bom jesus", "Incubatório Bom Jesus"), ("allexcorp", "Allexcorp"),
    ("ala-forte", "Ala-Forte"),
]


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as c:
            c.executescript(SCHEMA)
            if c.execute("SELECT COUNT(*) FROM client_rules").fetchone()[0] == 0 and \
               c.execute("SELECT value FROM kv WHERE key='rules_seeded'").fetchone() is None:
                c.executemany("INSERT INTO client_rules(pattern, client, position) VALUES (?,?,?)",
                              [(p, cl, i * 10) for i, (p, cl) in enumerate(DEFAULT_RULES, 1)])
                c.execute("INSERT OR REPLACE INTO kv(key,value) VALUES('rules_seeded','1')")

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def transaction(self):
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise

    # ---------------------------------------------------------------- leitura
    def kv_get(self, key: str, default=None):
        with self.connect() as c:
            row = c.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row and row["value"] is not None else default

    def objects(self) -> list[dict]:
        with self.connect() as c:
            return [json.loads(r["data"]) for r in c.execute("SELECT data FROM snap_objects")]

    def object(self, key: str) -> dict | None:
        with self.connect() as c:
            r = c.execute("SELECT data FROM snap_objects WHERE key=?", (key,)).fetchone()
        return json.loads(r["data"]) if r else None

    def jobs(self) -> list[dict]:
        with self.connect() as c:
            return [json.loads(r["data"]) for r in c.execute("SELECT data FROM snap_jobs")]

    def job(self, job_id: str) -> dict | None:
        with self.connect() as c:
            r = c.execute("SELECT data FROM snap_jobs WHERE job_id=?", (job_id,)).fetchone()
        return json.loads(r["data"]) if r else None

    def object_meta(self) -> dict[str, dict]:
        with self.connect() as c:
            return {r["key"]: dict(r) for r in c.execute("SELECT * FROM object_meta")}

    def job_meta(self) -> dict[str, dict]:
        with self.connect() as c:
            return {r["job_id"]: dict(r) for r in c.execute("SELECT * FROM job_meta")}

    def client_rules(self) -> list[dict]:
        with self.connect() as c:
            # Ordem automática: o texto mais longo (mais específico) é testado primeiro.
            return [dict(r) for r in c.execute(
                "SELECT * FROM client_rules ORDER BY length(pattern) DESC, lower(pattern), id")]

    def restore_points_for(self, backup_ids: list[str], name_lc: str, limit: int = 200) -> list[dict]:
        if not backup_ids:
            return []
        q = ",".join("?" * len(backup_ids))
        with self.connect() as c:
            rows = c.execute(
                f"SELECT data FROM snap_restore_points WHERE backup_id IN ({q}) AND name_lc=? "
                f"ORDER BY created DESC LIMIT ?", (*backup_ids, name_lc, limit)).fetchall()
        return [json.loads(r["data"]) for r in rows]

    def tasks_for(self, job_id: str, name_lc: str, limit: int = 100) -> list[dict]:
        with self.connect() as c:
            rows = c.execute(
                "SELECT t.data AS t, s.data AS s FROM tasks t JOIN sessions s ON s.id=t.session_id "
                "WHERE s.job_id=? AND t.name_lc=? ORDER BY t.created DESC LIMIT ?",
                (job_id, name_lc, limit)).fetchall()
        return [{"task": json.loads(r["t"]), "session": json.loads(r["s"])} for r in rows]

    def sessions_for_job(self, job_id: str, limit: int = 60) -> list[dict]:
        with self.connect() as c:
            rows = c.execute("SELECT id, data FROM sessions WHERE job_id=? ORDER BY created DESC LIMIT ?",
                             (job_id, limit)).fetchall()
            counts = dict(c.execute(
                "SELECT session_id, COUNT(*) FROM tasks WHERE session_id IN (SELECT id FROM sessions WHERE job_id=?) "
                "GROUP BY session_id", (job_id,)).fetchall())
        out = []
        for r in rows:
            d = json.loads(r["data"])
            d["_task_count"] = counts.get(r["id"], 0)
            out.append(d)
        return out

    def last_runs(self, limit: int = 50) -> list[dict]:
        with self.connect() as c:
            return [dict(r) for r in c.execute("SELECT * FROM sync_runs ORDER BY id DESC LIMIT ?", (limit,))]

    def last_ok_run(self) -> dict | None:
        with self.connect() as c:
            r = c.execute("SELECT * FROM sync_runs WHERE ok=1 ORDER BY id DESC LIMIT 1").fetchone()
        return dict(r) if r else None

    def audit(self, username: str | None, action: str, target: str = "", detail: str = "") -> None:
        from .timeutil import now_utc, to_db
        with self.connect() as c:
            c.execute("INSERT INTO audit(at, username, action, target, detail) VALUES (?,?,?,?,?)",
                      (to_db(now_utc()), username, action, target, detail))

    def audit_list(self, limit: int = 200) -> list[dict]:
        with self.connect() as c:
            return [dict(r) for r in c.execute("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,))]
