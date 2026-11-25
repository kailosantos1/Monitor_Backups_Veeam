"""Monta as linhas exibidas: objeto do Veeam + metadados do admin + status."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .config import Settings
from .db import Database
from .status import Status, compute
from .timeutil import from_db, now_utc

KIND_LABEL = {"vm": "VM", "agent": "Agente", "orphan": "Backup órfão"}
NO_CLIENT = "Sem cliente"


def resolve_client(obj: dict, meta: dict | None, job_meta: dict | None, rules: list[dict]) -> tuple[str, str]:
    if meta and (meta.get("client") or "").strip():
        return meta["client"].strip(), "objeto"
    if job_meta and (job_meta.get("client") or "").strip():
        return job_meta["client"].strip(), "job"
    hay = f"{obj.get('job_name', '')} {obj.get('name', '')}".lower()
    for r in sorted(rules, key=lambda x: -len(x["pattern"].strip())):  # mais específico primeiro
        if r["pattern"].strip() and r["pattern"].strip().lower() in hay:
            return r["client"], f"regra \"{r['pattern']}\""
    return NO_CLIENT, ""


@dataclass
class Row:
    obj: dict
    meta: dict | None
    status: Status
    client: str
    client_source: str

    @property
    def key(self):
        return self.obj["key"]

    @property
    def last_rp(self) -> datetime | None:
        return from_db(self.obj.get("last_rp_at"))

    @property
    def kind_label(self):
        return KIND_LABEL.get(self.obj.get("kind"), self.obj.get("kind"))


class Freshness:
    def __init__(self, db: Database, settings: Settings, now: datetime):
        self.snapshot_at = from_db(db.kv_get("snapshot_at"))
        runs = db.last_runs(1)
        self.last_run = runs[0] if runs else None
        self.age_min = (now - self.snapshot_at).total_seconds() / 60 if self.snapshot_at else None
        self.stale = self.snapshot_at is None or self.age_min > settings.stale_after_minutes
        self.last_failed = bool(self.last_run and self.last_run.get("finished_at") and not self.last_run["ok"])
        self.running = bool(self.last_run and not self.last_run.get("finished_at"))
        self.never = self.snapshot_at is None


def build_rows(db: Database, settings: Settings, now: datetime | None = None) -> list[Row]:
    now = now or now_utc()
    metas = db.object_meta()
    jmetas = db.job_meta()
    rules = db.client_rules()
    rows = []
    for o in db.objects():
        m = metas.get(o["key"])
        jm = jmetas.get(o.get("job_id") or "")
        st = compute(o, m, jm, now, settings.rpo_vm_hours, settings.rpo_agent_hours)
        client, csrc = resolve_client(o, m, jm, rules)
        rows.append(Row(obj=o, meta=m, status=st, client=client, client_source=csrc))
    rows.sort(key=lambda r: (r.status.rank, (r.status.rp_age_hours if r.status.rp_age_hours is not None else -1) if r.status.code == "sem_backup" else 0,
                             r.client.lower(), r.obj["name"].lower()))
    return rows
