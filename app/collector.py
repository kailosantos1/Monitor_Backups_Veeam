"""Coleta de todos os dados necessários do Veeam em uma única passada.

Endpoints ESSENCIAIS: se qualquer um falhar, a coleta inteira falha e nada
é alterado no banco. Endpoints AUXILIARES (infraestrutura) podem falhar sem
invalidar o status dos backups; a falha aparece na tela de infraestrutura.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .veeam_client import EndpointNotFound, VeeamError

log = logging.getLogger("veeam.collector")


@dataclass
class RawData:
    collected_at: datetime
    jobs: list[dict]
    job_states: list[dict]
    sessions: list[dict]
    task_sessions: list[dict]
    backups: list[dict]
    backup_objects: list[dict]
    restore_points: list[dict]
    managed_servers: list[dict] | None = None
    repositories: list[dict] | None = None
    aux_errors: dict[str, str] = field(default_factory=dict)
    task_source: str = ""  # "global" ou "por-sessao"
    request_count: int = 0

    def counts(self) -> dict[str, int]:
        return {
            "jobs": len(self.jobs),
            "jobs_states": len(self.job_states),
            "sessions": len(self.sessions),
            "taskSessions": len(self.task_sessions),
            "backups": len(self.backups),
            "backupObjects": len(self.backup_objects),
            "restorePoints": len(self.restore_points),
            "managedServers": -1 if self.managed_servers is None else len(self.managed_servers),
            "repositories": -1 if self.repositories is None else len(self.repositories),
        }


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def collect(source, history_days: int, now: datetime | None = None) -> RawData:
    """`source` é um VeeamClient ou ReplaySource. Levanta VeeamError em falha essencial."""
    now = now or datetime.now(timezone.utc)
    since = _iso(now - timedelta(days=history_days))

    source.login()
    jobs = source.get_all("/api/v1/jobs")
    job_states = source.get_all("/api/v1/jobs/states")
    sessions = source.get_all("/api/v1/sessions", {"createdAfterFilter": since})
    backups = source.get_all("/api/v1/backups")
    backup_objects = source.get_all("/api/v1/backupObjects")
    restore_points = source.get_all("/api/v1/restorePoints")

    # Task sessions (resultado por VM). Preferimos o endpoint global; se a
    # versão do Veeam não tiver, buscamos sessão a sessão. Qualquer sessão
    # que falhe derruba a coleta: resultado por VM incompleto não serve.
    try:
        tasks = source.get_all("/api/v1/taskSessions", {"createdAfterFilter": since})
        task_source = "global"
    except EndpointNotFound:
        log.info("/api/v1/taskSessions indisponível; buscando por sessão")
        task_source = "por-sessao"
        targets = [s["id"] for s in sessions if s.get("sessionType") == "BackupJob" and s.get("id")]
        tasks = []

        def one(sid: str) -> list[dict]:
            rows = source.get_unpaged(f"/api/v1/sessions/{sid}/taskSessions")
            for r in rows:
                r.setdefault("sessionId", sid)
            return rows

        with ThreadPoolExecutor(max_workers=6) as ex:
            for rows in ex.map(one, targets):
                tasks.extend(rows)

    raw = RawData(
        collected_at=now, jobs=jobs, job_states=job_states, sessions=sessions,
        task_sessions=tasks, backups=backups, backup_objects=backup_objects,
        restore_points=restore_points, task_source=task_source,
    )

    for attr, path in (("managed_servers", "/api/v1/backupInfrastructure/managedServers"),
                       ("repositories", "/api/v1/backupInfrastructure/repositories/states")):
        try:
            setattr(raw, attr, source.get_all(path))
        except VeeamError as e:
            raw.aux_errors[attr] = str(e)
            log.warning("Endpoint auxiliar %s falhou: %s", path, e)

    raw.request_count = getattr(source, "request_count", 0)
    return raw
