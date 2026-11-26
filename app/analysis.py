"""Transforma os dados brutos do Veeam no estado de cada objeto protegido.

Função pura: recebe listas (como vieram da API) e devolve fatos. Não decide
cor/status; isso é feito em status.py, na hora de exibir, com o relógio
atual. Assim o status nunca "congela" se a sincronização parar.

Regras (todas derivadas de como o próprio Veeam funciona):

1. Objeto = (job, nome da máquina) para jobs existentes, ou (backup, nome)
   para backups sem job no /jobs (Veeam Agent standalone e backups órfãos).
   Uma mesma VM em dois jobs gera dois objetos, igual ao console.

2. Inventário de objetos de um job = união de:
   - VMs listadas explicitamente na definição do job;
   - máquinas que aparecem nas task sessions das sessões do job
     (cobre jobs que incluem um Host/pasta inteira);
   - máquinas que têm ponto de restauração nos backups do job.
   Nada é descartado: se existe ponto ou tarefa, o objeto aparece.

3. Resultado por objeto = a task session FINALIZADA mais recente daquela
   máquina dentro do job. Isso trata retries corretamente (o retry só
   reprocessa quem falhou; quem deu certo antes mantém o sucesso).

4. Falha no nível do job: se a sessão finalizada mais recente do job é
   mais nova que a última tarefa do objeto, terminou em Failed/Warning e
   não tem NENHUMA task session (o job morreu antes de processar VMs),
   o resultado dessa sessão vale para todos os objetos do job.

5. Último backup = creationTime do ponto de restauração mais recente que
   existe hoje no Veeam para aquele objeto naquele backup.

6. "Em execução" só vem da coleta atual, nunca do histórico guardado.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .timeutil import parse_ts, to_db

AGENT_PLATFORMS = {"WindowsPhysical", "LinuxPhysical", "MacPhysical"}
CONTAINER_TYPES_LABEL = {
    "Host": "Host inteiro", "HostSystem": "Host inteiro", "Folder": "Pasta", "Cluster": "Cluster",
    "ResourcePool": "Resource pool", "Datastore": "Datastore", "Tag": "Tag", "VApp": "vApp",
    "Datacenter": "Datacenter", "ComputeResource": "Host/cluster",
}
FINISHED = "stopped"
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _lc(s) -> str:
    return (s or "").strip().lower()


def _result(obj: dict) -> tuple[str, str, bool]:
    r = obj.get("result")
    if isinstance(r, dict):
        return (r.get("result") or "None"), (r.get("message") or "").strip(), bool(r.get("isCanceled"))
    return (r or "None"), (obj.get("message") or "").strip(), False


def task_finished(t: dict) -> bool:
    """Tarefa terminada? Dentro de uma sessão que ainda roda, o Veeam mantém
    state=Working em tarefas que JÁ terminaram (têm resultado e hora de fim).
    O console mostra esse resultado, então nós também."""
    if _lc(t.get("state")) == FINISHED:
        return True
    return _result(t)[0] in ("Success", "Warning", "Failed") and parse_ts(t.get("endTime")) is not None


def _run_info(run_t: list[dict]) -> dict | None:
    """Andamento da tarefa em execução, como o Veeam informa (sem % por VM)."""
    if not run_t:
        return None
    t = max(run_t, key=lambda x: parse_ts(x.get("creationTime")) or EPOCH)
    p = t.get("progress") or {}
    return {"duration": p.get("duration") or "", "processed": p.get("processedSize") or 0,
            "rate": p.get("processingRate") or "", "bottleneck": p.get("bottleneck") or "",
            "since": to_db(parse_ts(t.get("creationTime")))}


WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def _max_gap_days(days: list[str]) -> int | None:
    idx = sorted({WEEKDAYS.index(d.lower()) for d in days or [] if d and d.lower() in WEEKDAYS})
    if not idx:
        return None
    gaps = [(idx[(i + 1) % len(idx)] - idx[i]) % 7 or 7 for i in range(len(idx))]
    return max(gaps)


def expected_hours(job: dict, jobs_by_name: dict[str, dict], _seen: frozenset = frozenset()) -> tuple[float | None, str]:
    """Intervalo máximo entre execuções segundo o agendamento do job no Veeam.
    Retorna (horas, de onde veio). None = sem agendamento automático."""
    s = job.get("schedule") or {}
    if not s or not s.get("runAutomatically", False) or job.get("isDisabled"):
        return None, ""
    cands: list[tuple[float, str]] = []
    d = s.get("daily") or {}
    if d.get("isEnabled"):
        kind = d.get("dailyKind") or ""
        if kind == "Everyday":
            cands.append((24.0, f"diário às {d.get('localTime', '?')}"))
        else:
            days = WEEKDAYS[:5] if kind == "WeekDays" else (d.get("days") or [])
            gap = _max_gap_days(days)
            if gap:
                cands.append((gap * 24.0, f"dias {'úteis' if kind == 'WeekDays' else 'selecionados'} às {d.get('localTime', '?')}"))
    p = s.get("periodically") or {}
    if p.get("isEnabled") and p.get("frequency"):
        f = float(p["frequency"])
        h = f / 60 if p.get("periodicallyKind") == "Minutes" else f
        cands.append((max(h, 1.0), f"a cada {p['frequency']} {'min' if p.get('periodicallyKind') == 'Minutes' else 'h'}"))
    m = s.get("monthly") or {}
    if m.get("isEnabled"):
        cands.append((31 * 24.0, "mensal"))
    if (s.get("continuously") or {}).get("isEnabled"):
        cands.append((24.0, "contínuo"))
    a = s.get("afterThisJob") or {}
    if a.get("isEnabled") and a.get("jobName"):
        parent = jobs_by_name.get(a["jobName"])
        if parent and parent.get("id") not in _seen:
            ph, psrc = expected_hours(parent, jobs_by_name, _seen | {job.get("id")})
            if ph:
                cands.append((ph, f"após \"{a['jobName']}\" ({psrc})"))
    if not cands:
        return None, ""
    return min(cands, key=lambda c: c[0])


def schedule_text(job: dict) -> tuple[str, bool]:
    """Descrição do agendamento como no console. Retorna (texto, agendado?)."""
    s = job.get("schedule") or {}
    if not s:
        return "—", False
    if not s.get("runAutomatically", False):
        return "Não agendado (execução manual)", False
    parts = []
    d = s.get("daily") or {}
    if d.get("isEnabled"):
        kind = d.get("dailyKind") or ""
        when = d.get("localTime") or "?"
        if kind == "Everyday":
            parts.append(f"Diário às {when}")
        elif kind == "WeekDays":
            parts.append(f"Dias úteis às {when}")
        else:
            dias = ", ".join((x or "")[:3] for x in d.get("days") or [])
            parts.append(f"Às {when} ({dias})")
    m = s.get("monthly") or {}
    if m.get("isEnabled"):
        if m.get("dayOfMonth"):
            parts.append(f"Mensal dia {m['dayOfMonth']} às {m.get('localTime', '?')}")
        else:
            parts.append(f"Mensal ({m.get('dayNumberInMonth', '')} {m.get('dayOfWeek', '')}) às {m.get('localTime', '?')}")
    p = s.get("periodically") or {}
    if p.get("isEnabled"):
        unidade = {"Hours": "h", "Minutes": "min"}.get(p.get("periodicallyKind"), p.get("periodicallyKind", ""))
        parts.append(f"A cada {p.get('frequency', '?')} {unidade}")
    c = s.get("continuously") or {}
    if c.get("isEnabled"):
        parts.append("Contínuo")
    a = s.get("afterThisJob") or {}
    if a.get("isEnabled"):
        parts.append(f"Após o job \"{a.get('jobName', '?')}\"")
    if not parts:
        return "Agendamento sem gatilho ativo", False
    return " + ".join(parts), True


@dataclass
class Analysis:
    objects: list[dict]
    jobs: list[dict]
    config_backup: list[dict]
    warnings: list[str] = field(default_factory=list)


def analyze(*, jobs: list[dict], job_states: list[dict], sessions: list[dict],
            task_sessions: list[dict], backups: list[dict], backup_objects: list[dict],
            restore_points: list[dict], running_session_ids: set[str] | None = None) -> Analysis:
    """`sessions`/`task_sessions` podem conter histórico guardado + coleta atual.
    `running_session_ids`: ids das sessões que a coleta ATUAL mostrou em execução."""
    warnings: list[str] = []
    running_session_ids = running_session_ids or set()

    jobs_by_id = {j["id"]: j for j in jobs if j.get("id")}
    jobs_by_name = {j.get("name"): j for j in jobs if j.get("name")}
    states_by_id = {s["id"]: s for s in job_states if s.get("id")}
    for jid in states_by_id.keys() - jobs_by_id.keys():
        warnings.append(f"Job '{states_by_id[jid].get('name')}' aparece em /jobs/states mas não em /jobs")
    for jid in jobs_by_id.keys() - states_by_id.keys():
        warnings.append(f"Job '{jobs_by_id[jid].get('name')}' aparece em /jobs mas não em /jobs/states")

    # ---------------------------------------------------------------- sessões
    sess_by_id: dict[str, dict] = {}
    for s in sessions:
        if s.get("id"):
            sess_by_id[s["id"]] = s
    job_sessions: dict[str, list[dict]] = defaultdict(list)
    config_backup: list[dict] = []
    for s in sess_by_id.values():
        stype = s.get("sessionType")
        if stype == "BackupJob" and s.get("jobId"):
            job_sessions[s["jobId"]].append(s)
        elif stype == "ConfigurationBackup":
            config_backup.append(s)

    def s_end(s):
        return parse_ts(s.get("endTime")) or parse_ts(s.get("creationTime"))

    for lst in job_sessions.values():
        lst.sort(key=lambda s: parse_ts(s.get("creationTime")) or EPOCH, reverse=True)

    # ----------------------------------------------------------------- tasks
    tasks_by_job_name: dict[tuple[str, str], list[dict]] = defaultdict(list)
    tasks_per_session: dict[str, int] = defaultdict(int)
    orphan_tasks = 0
    for t in task_sessions:
        if t.get("type") not in (None, "Backup"):
            continue
        if t.get("sessionType") not in (None, "BackupJob"):
            continue
        sid = t.get("sessionId")
        sess = sess_by_id.get(sid)
        if not sess:
            orphan_tasks += 1
            continue
        tasks_per_session[sid] += 1
        name = _lc(t.get("name"))
        if name:
            tasks_by_job_name[(sess.get("jobId"), name)].append(t)
    if orphan_tasks:
        warnings.append(f"{orphan_tasks} task session(s) de sessões fora da janela de histórico (ignoradas)")

    # --------------------------------------------------------- backups / RPs
    backups_by_id = {b["id"]: b for b in backups if b.get("id")}
    backups_by_job: dict[str, list[dict]] = defaultdict(list)
    for b in backups_by_id.values():
        backups_by_job[b.get("jobId") or ""].append(b)

    rps: dict[tuple[str, str], list[dict]] = defaultdict(list)
    orphan_rp = 0
    for rp in restore_points:
        bid = rp.get("backupId")
        if bid not in backups_by_id:
            orphan_rp += 1
            continue
        rps[(bid, _lc(rp.get("name")))].append(rp)
    if orphan_rp:
        warnings.append(f"{orphan_rp} ponto(s) de restauração apontam para backup inexistente em /backups")

    # host por nome (de backupObjects), só quando o nome é único
    bo_by_name: dict[str, list[dict]] = defaultdict(list)
    for bo in backup_objects:
        bo_by_name[_lc(bo.get("name"))].append(bo)

    def host_from_bo(name_lc: str) -> str:
        cands = [b for b in bo_by_name.get(name_lc, []) if b.get("path")]
        hosts = {str(b["path"]).split("\\")[0] for b in cands}
        return hosts.pop() if len(hosts) == 1 else ""

    def rp_facts(backup_ids: list[str], name_lc: str) -> dict:
        allrp = []
        for bid in backup_ids:
            allrp.extend(rps.get((bid, name_lc), []))
        allrp.sort(key=lambda r: parse_ts(r.get("creationTime")) or EPOCH, reverse=True)
        last = allrp[0] if allrp else None
        return {
            "rp_count": len(allrp),
            "last_rp_at": to_db(parse_ts(last.get("creationTime"))) if last else None,
            "last_rp_type": last.get("type") if last else None,
            "last_rp_id": last.get("id") if last else None,
            "last_rp_malware": (last.get("malwareStatus") or "") if last else "",
            "oldest_rp_at": to_db(parse_ts(allrp[-1].get("creationTime"))) if allrp else None,
            "rp_ids": [r.get("id") for r in allrp[:60]],
        }

    objects: list[dict] = []
    jobs_out: list[dict] = []

    # ------------------------------------------------------------------ jobs
    for jid in sorted(jobs_by_id.keys() | states_by_id.keys(),
                      key=lambda k: _lc((jobs_by_id.get(k) or states_by_id.get(k) or {}).get("name"))):
        job = jobs_by_id.get(jid, {})
        st = states_by_id.get(jid, {})
        jname = job.get("name") or st.get("name") or jid
        disabled = bool(job.get("isDisabled")) or _lc(st.get("status")) == "disabled"
        sched, scheduled = schedule_text(job)
        exp_h, exp_src = expected_hours(job, jobs_by_name) if job else (None, "")
        sess_list = job_sessions.get(jid, [])
        finished = [s for s in sess_list if _lc(s.get("state")) == FINISHED]
        finished.sort(key=lambda s: s_end(s) or EPOCH, reverse=True)
        last_fin = finished[0] if finished else None
        running = [s for s in sess_list if s.get("id") in running_session_ids]
        run_sess = running[0] if running else None

        lf_res, lf_msg, lf_cancel = _result(last_fin) if last_fin else ("", "", False)
        job_level_failure = None
        if last_fin and tasks_per_session.get(last_fin["id"], 0) == 0 and lf_res in ("Failed", "Warning"):
            job_level_failure = last_fin

        vm_spec = (job.get("virtualMachines") or {})
        includes = vm_spec.get("includes") or []
        containers = []
        explicit: dict[str, dict] = {}
        for inc in includes:
            itype = inc.get("type") or ""
            if itype == "VirtualMachine":
                k = _lc(inc.get("name"))
                if k in explicit:
                    warnings.append(f"Job '{jname}': duas VMs com o mesmo nome '{inc.get('name')}' na definição")
                explicit[k] = inc
            else:
                containers.append(f"{CONTAINER_TYPES_LABEL.get(itype, itype)}: {inc.get('name')}")
        excluded = {_lc(e.get("name")) for e in (vm_spec.get("excludes") or {}).get("vms") or []}

        job_backups = backups_by_job.get(jid, [])
        bids = [b["id"] for b in job_backups]

        names: dict[str, str] = {}  # lc -> nome de exibição
        for k, inc in explicit.items():
            names[k] = inc.get("name") or k
        for (j, n), tl in tasks_by_job_name.items():
            if j == jid and n not in names:
                names[n] = tl[0].get("name") or n
        for bid in bids:
            for (b, n), rl in rps.items():
                if b == bid and n not in names:
                    names[n] = rl[0].get("name") or n

        jobs_out.append({
            "job_id": jid, "name": jname, "type": job.get("type") or st.get("type") or "",
            "workload": st.get("workload") or "", "description": (job.get("description") or st.get("description") or "").strip(),
            "disabled": disabled, "schedule": sched, "scheduled": scheduled,
            "expected_hours": exp_h, "expected_source": exp_src,
            "next_run": to_db(parse_ts(st.get("nextRun"))), "last_run": to_db(parse_ts(st.get("lastRun"))),
            "repository": st.get("repositoryName") or "", "objects_count_veeam": st.get("objectsCount"),
            "running": run_sess is not None,
            "running_progress": (run_sess or {}).get("progressPercent"),
            "running_since": to_db(parse_ts((run_sess or {}).get("creationTime"))),
            "last_result": lf_res or None, "last_message": lf_msg, "last_canceled": lf_cancel,
            "last_end": to_db(s_end(last_fin)) if last_fin else None,
            "last_session_id": last_fin["id"] if last_fin else None,
            "containers": containers, "objects_count": len(names),
            "retry": ((job.get("schedule") or {}).get("retry") or {}),
            "state_raw": st.get("status") or "", "last_result_raw": st.get("lastResult") or "",
        })

        for n_lc, display in sorted(names.items()):
            inc = explicit.get(n_lc)
            tl = tasks_by_job_name.get((jid, n_lc), [])
            fin_t = [t for t in tl if task_finished(t)]
            fin_t.sort(key=lambda t: parse_ts(t.get("endTime")) or parse_ts(t.get("creationTime")) or EPOCH, reverse=True)
            last_t = fin_t[0] if fin_t else None
            run_t = [t for t in tl if t.get("sessionId") in running_session_ids and not task_finished(t)]
            ok_t = [t for t in fin_t if _result(t)[0] in ("Success", "Warning")]

            res = msg = src = sid = None
            res_at = None
            if last_t:
                res, msg, _ = _result(last_t)
                res_at = parse_ts(last_t.get("endTime")) or parse_ts(last_t.get("creationTime"))
                src, sid = "tarefa da VM", last_t.get("sessionId")
            if job_level_failure is not None:
                jl_at = s_end(job_level_failure)
                if res_at is None or (jl_at and jl_at > res_at):
                    res, msg, _ = _result(job_level_failure)
                    res_at, src, sid = jl_at, "sessão do job (falhou antes de processar as VMs)", job_level_failure["id"]

            host = (inc or {}).get("hostName") or host_from_bo(n_lc)
            in_def = inc is not None
            obj = {
                "key": f"job:{jid}:{n_lc}", "kind": "agent" if _is_agent_backup(job_backups) else "vm",
                "name": display, "host": host, "job_id": jid, "job_name": jname, "backup_ids": bids,
                "in_definition": in_def, "job_has_containers": bool(containers),
                "excluded": n_lc in excluded, "job_disabled": disabled, "job_scheduled": scheduled,
                "expected_hours": exp_h, "schedule_source": exp_src, "job_last_result": lf_res or None,
                "size": (inc or {}).get("size") or "",
                "last_result": res, "last_message": msg or "", "last_result_at": to_db(res_at),
                "last_result_source": src, "last_result_session": sid,
                "last_success_at": to_db(max((parse_ts(t.get("endTime")) for t in ok_t if t.get("endTime")), default=None)),
                "running": bool(run_t),
                "run_task": _run_info(run_t),
                "job_running": run_sess is not None,
                "job_running_since": to_db(parse_ts((run_sess or {}).get("creationTime"))) if run_sess else None,
                "running_progress": (run_sess or {}).get("progressPercent") if run_sess else None,
                "task_count": len(fin_t),
                "platform": "",
            }
            obj.update(rp_facts(bids, n_lc))
            # Ponto criado DURANTE uma tarefa desta VM que falhou pode estar
            # incompleto (o Veeam costuma apagá-lo na execução seguinte).
            # Não descartamos (o console ainda o mostra), mas sinalizamos e
            # guardamos a data do último ponto fora de execuções com falha.
            fail_windows = [(parse_ts(t.get("creationTime")), parse_ts(t.get("endTime")))
                            for t in fin_t if _result(t)[0] == "Failed"]
            obj["last_rp_in_failed_run"] = False
            obj["last_good_rp_at"] = obj["last_rp_at"]
            if fail_windows and obj["last_rp_at"]:
                def in_fail(ts_):
                    return any(a and b and a <= ts_ <= b for a, b in fail_windows)
                pts = sorted((parse_ts(r.get("creationTime")) for bid in bids for r in rps.get((bid, n_lc), [])
                              if parse_ts(r.get("creationTime"))), reverse=True)
                if pts and in_fail(pts[0]):
                    obj["last_rp_in_failed_run"] = True
                    obj["last_good_rp_at"] = to_db(next((p for p in pts if not in_fail(p)), None))
            objects.append(obj)

    # ---------------------------------------- backups sem job (agentes/órfãos)
    for bid, b in sorted(backups_by_id.items(), key=lambda kv: _lc(kv[1].get("name"))):
        if (b.get("jobId") or "") in jobs_by_id:
            continue
        platform = b.get("platformName") or ""
        is_agent = platform in AGENT_PLATFORMS
        names = {n: rl[0].get("name") or n for (bb, n), rl in rps.items() if bb == bid}
        if not names:
            names = {_lc(b.get("name")) or bid: b.get("name") or bid}
        for n_lc, display in sorted(names.items()):
            obj = {
                "key": f"backup:{bid}:{n_lc}", "kind": "agent" if is_agent else "orphan",
                "name": display, "host": "" if is_agent else host_from_bo(n_lc),
                "job_id": b.get("jobId") or "", "job_name": b.get("name") or "", "backup_ids": [bid],
                "in_definition": True, "job_has_containers": False, "excluded": False,
                "job_disabled": False, "job_scheduled": True, "size": "",
                "last_result": None, "last_message": "", "last_result_at": None,
                "last_result_source": None, "last_result_session": None, "last_success_at": None,
                "running": False, "run_task": None, "job_running": False, "running_progress": None, "task_count": 0, "platform": platform,
            }
            obj.update(rp_facts([bid], n_lc))
            objects.append(obj)

    config_backup.sort(key=lambda s: parse_ts(s.get("creationTime")) or EPOCH, reverse=True)
    cfg = [{
        "id": s.get("id"), "name": s.get("name"), "state": s.get("state"),
        "result": _result(s)[0], "message": _result(s)[1],
        "created": to_db(parse_ts(s.get("creationTime"))), "ended": to_db(parse_ts(s.get("endTime"))),
    } for s in config_backup[:20]]

    keys = [o["key"] for o in objects]
    if len(keys) != len(set(keys)):
        warnings.append("Chaves de objeto duplicadas detectadas (erro interno)")
    return Analysis(objects=objects, jobs=jobs_out, config_backup=cfg, warnings=warnings)


def _is_agent_backup(job_backups: list[dict]) -> bool:
    return bool(job_backups) and all((b.get("platformName") in AGENT_PLATFORMS) for b in job_backups)

