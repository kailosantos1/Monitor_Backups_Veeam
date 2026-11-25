"""Sincronização tudo-ou-nada.

1. Coleta tudo do Veeam (collector). Falha em endpoint essencial = aborta.
2. Checagens de sanidade. Falhou = aborta.
3. Numa única transação: grava histórico de sessões/tarefas, recalcula o
   retrato completo e substitui o retrato anterior.
4. Registra a execução (sucesso ou erro detalhado) em sync_runs.

Se qualquer passo falhar, o retrato anterior permanece intacto e a tela
mostra que os dados estão desatualizados e por quê.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import timedelta

from .analysis import analyze
from .collector import RawData, collect
from .config import Settings
from .db import Database
from .timeutil import now_utc, parse_ts, to_db
from .veeam_client import ReplaySource, VeeamClient

log = logging.getLogger("veeam.sync")
_lock = threading.Lock()
HISTORY_KEEP_DAYS = 400


class SanityError(Exception):
    pass


def make_source(settings: Settings):
    if settings.replay_dir:
        return ReplaySource(settings.replay_dir)
    return VeeamClient(settings.veeam_url, settings.veeam_user, settings.veeam_password,
                       settings.veeam_api_version, settings.veeam_verify_ssl,
                       settings.veeam_timeout, settings.veeam_page_size)


def sanity_check(raw: RawData, previous_objects: int) -> None:
    backup_sessions = [s for s in raw.sessions if s.get("sessionType") == "BackupJob"]
    finished_ok = [s for s in backup_sessions if (s.get("state") or "").lower() == "stopped"
                   and ((s.get("result") or {}).get("result") in ("Success", "Warning"))]
    if finished_ok and not raw.task_sessions:
        raise SanityError(
            f"O Veeam devolveu {len(finished_ok)} sessões de backup concluídas com sucesso/alerta, "
            "mas nenhuma task session. Sem o resultado por máquina o status não é confiável.")
    if previous_objects > 0 and not raw.backups and not raw.jobs:
        raise SanityError("O Veeam devolveu zero jobs e zero backups (antes havia dados). Coleta descartada.")
    if raw.jobs and not raw.job_states:
        raise SanityError("/jobs trouxe jobs mas /jobs/states veio vazio.")


def run_sync(db: Database, settings: Settings, trigger: str = "agendado", source=None) -> dict:
    if not _lock.acquire(blocking=False):
        return {"ok": False, "error": "Já existe uma sincronização em andamento."}
    started = now_utc()
    t0 = time.monotonic()
    with db.connect() as c:
        run_id = c.execute("INSERT INTO sync_runs(started_at, trigger) VALUES (?,?)",
                           (to_db(started), trigger)).lastrowid
    try:
        src = source or make_source(settings)
        try:
            raw = collect(src, settings.history_days, now=started)
        finally:
            src.close()
        prev = len(db.objects())
        sanity_check(raw, prev)
        result = _persist(db, raw, settings, run_id, time.monotonic() - t0)
        log.info("Sync OK em %.1fs: %s", time.monotonic() - t0, result["counts"])
        return {"ok": True, **result}
    except Exception as e:  # noqa: BLE001 - registramos qualquer falha
        msg = f"{type(e).__name__}: {e}"
        log.error("Sync falhou: %s", msg, exc_info=not isinstance(e, (SanityError,)) and log.isEnabledFor(logging.DEBUG))
        with db.connect() as c:
            c.execute("UPDATE sync_runs SET finished_at=?, ok=0, error=?, duration=? WHERE id=?",
                      (to_db(now_utc()), msg, round(time.monotonic() - t0, 2), run_id))
        return {"ok": False, "error": msg}
    finally:
        _lock.release()


def _persist(db: Database, raw: RawData, settings: Settings, run_id: int, elapsed: float) -> dict:
    since = to_db(raw.collected_at - timedelta(days=settings.history_days))
    fresh_s = {s["id"]: s for s in raw.sessions if s.get("id")}
    fresh_t = {t["id"]: t for t in raw.task_sessions if t.get("id")}
    running_ids = {sid for sid, s in fresh_s.items() if (s.get("state") or "").lower() != "stopped"}

    with db.transaction() as c:
        # Histórico: a coleta atual é a verdade para a janela consultada.
        c.execute("DELETE FROM tasks WHERE created >= ?", (since,))
        c.execute("DELETE FROM sessions WHERE created >= ?", (since,))
        c.executemany(
            "INSERT OR REPLACE INTO sessions(id, job_id, session_type, created, data) VALUES (?,?,?,?,?)",
            [(sid, s.get("jobId"), s.get("sessionType"), to_db(parse_ts(s.get("creationTime"))), json.dumps(s))
             for sid, s in fresh_s.items()])
        c.executemany(
            "INSERT OR REPLACE INTO tasks(id, session_id, name_lc, created, data) VALUES (?,?,?,?,?)",
            [(tid, t.get("sessionId"), (t.get("name") or "").strip().lower(),
              to_db(parse_ts(t.get("creationTime"))), json.dumps(t)) for tid, t in fresh_t.items()])
        cutoff = to_db(raw.collected_at - timedelta(days=HISTORY_KEEP_DAYS))
        c.execute("DELETE FROM tasks WHERE created < ?", (cutoff,))
        c.execute("DELETE FROM sessions WHERE created < ?", (cutoff,))

        all_s = [json.loads(r[0]) for r in c.execute("SELECT data FROM sessions")]
        all_t = [json.loads(r[0]) for r in c.execute("SELECT data FROM tasks")]

        a = analyze(jobs=raw.jobs, job_states=raw.job_states, sessions=all_s, task_sessions=all_t,
                    backups=raw.backups, backup_objects=raw.backup_objects,
                    restore_points=raw.restore_points, running_session_ids=running_ids)

        # Sessões concluídas com sucesso que não têm nenhuma tarefa = dado inconsistente.
        task_sess = {t.get("sessionId") for t in all_t}
        weird = [s for s in fresh_s.values() if s.get("sessionType") == "BackupJob"
                 and (s.get("state") or "").lower() == "stopped"
                 and (s.get("result") or {}).get("result") == "Success" and s["id"] not in task_sess]
        if weird:
            a.warnings.append(f"{len(weird)} sessão(ões) concluída(s) com Sucesso sem nenhuma task session: "
                              + ", ".join(sorted({s.get('name') or '?' for s in weird})[:5]))

        c.execute("DELETE FROM snap_objects")
        c.executemany("INSERT INTO snap_objects(key, job_id, data) VALUES (?,?,?)",
                      [(o["key"], o["job_id"], json.dumps(o)) for o in a.objects])
        c.execute("DELETE FROM snap_jobs")
        c.executemany("INSERT INTO snap_jobs(job_id, data) VALUES (?,?)",
                      [(j["job_id"], json.dumps(j)) for j in a.jobs])
        c.execute("DELETE FROM snap_restore_points")
        c.executemany(
            "INSERT OR REPLACE INTO snap_restore_points(id, backup_id, name_lc, created, data) VALUES (?,?,?,?,?)",
            [(rp["id"], rp.get("backupId"), (rp.get("name") or "").strip().lower(),
              to_db(parse_ts(rp.get("creationTime"))), json.dumps(rp)) for rp in raw.restore_points if rp.get("id")])

        infra = {
            "managed_servers": raw.managed_servers, "repositories": raw.repositories,
            "aux_errors": raw.aux_errors, "config_backup": a.config_backup,
        }
        for k, v in (("infra", infra), ("snapshot_at", to_db(raw.collected_at)),
                     ("integrity_warnings", a.warnings)):
            c.execute("INSERT OR REPLACE INTO kv(key, value) VALUES (?,?)", (k, json.dumps(v)))

        counts = raw.counts()
        counts["objetos"] = len(a.objects)
        c.execute("UPDATE sync_runs SET finished_at=?, ok=1, error=NULL, duration=?, counts=?, warnings=?, "
                  "aux_errors=?, request_count=?, task_source=? WHERE id=?",
                  (to_db(now_utc()), round(elapsed, 2), json.dumps(counts), json.dumps(a.warnings),
                   json.dumps(raw.aux_errors), raw.request_count, raw.task_source, run_id))
    return {"counts": counts, "warnings": a.warnings}


class Scheduler:
    """Thread única que sincroniza no intervalo configurado."""

    def __init__(self, db: Database, settings: Settings):
        self.db, self.settings = db, settings
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="veeam-sync", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def trigger_now(self) -> None:
        self._wake.set()

    def _loop(self) -> None:
        trigger = "inicialização"
        while not self._stop.is_set():
            try:
                run_sync(self.db, self.settings, trigger=trigger)
            except Exception:  # noqa: BLE001
                log.exception("Erro inesperado no agendador")
            self._wake.wait(self.settings.sync_interval_minutes * 60)
            trigger = "manual" if self._wake.is_set() else "agendado"
            self._wake.clear()
