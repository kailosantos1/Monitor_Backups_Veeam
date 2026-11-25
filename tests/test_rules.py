"""Regras de status: cada teste reproduz um cenário real do Veeam."""
from app.analysis import analyze, schedule_text
from app.status import compute
from app.timeutil import parse_ts

from builders import NOW, backup, job, rp, session, state, task


def run(jobs=(), states=None, sessions=(), tasks=(), backups=(), rps=(), running=None):
    a = analyze(jobs=list(jobs), job_states=list(states if states is not None else [state(j) for j in jobs]),
                sessions=list(sessions), task_sessions=list(tasks), backups=list(backups), backup_objects=[],
                restore_points=list(rps),
                running_session_ids=running if running is not None else
                {s["id"] for s in sessions if s["state"] != "Stopped"})
    return a


def st(obj, meta=None, job_meta=None):
    return compute(obj, meta, job_meta, NOW, 36, 72)


def by_name(a):
    return {o["name"]: o for o in a.objects}


# --------------------------------------------------------------------- retry
def test_retry_only_reprocesses_failed_vm_and_success_wins():
    j = job("J", vms=["A", "B"])
    b = backup(j)
    s1 = session(j, 10, 9, "Failed", "Processing B")
    s2 = session(j, 8.8, 8.5, "Success")  # retry: só B
    t = [task(s1, "A", 10, 9.5), task(s1, "B", 10, 9, "Failed", "Erro de rede"), task(s2, "B", 8.8, 8.5)]
    a = run([j], sessions=[s1, s2], tasks=t, backups=[b], rps=[rp(b, "A", 9.5), rp(b, "B", 8.5)])
    o = by_name(a)
    assert st(o["A"]).code == "ok"
    assert st(o["B"]).code == "ok"
    assert a.jobs[0]["last_result"] == "Success"


def test_retry_failing_again_keeps_only_that_vm_failed():
    j = job("J", vms=["A", "B"])
    b = backup(j)
    s1 = session(j, 10, 9, "Failed")
    s2 = session(j, 8.8, 8.5, "Failed", "Processing B")  # retry só de B, falhou de novo
    t = [task(s1, "A", 10, 9.5), task(s1, "B", 10, 9, "Failed", "x"), task(s2, "B", 8.8, 8.5, "Failed", "disco cheio")]
    a = run([j], sessions=[s1, s2], tasks=t, backups=[b], rps=[rp(b, "A", 9.5), rp(b, "B", 30)])
    o = by_name(a)
    assert st(o["A"]).code == "ok"           # a sessão de retry tem tarefas: não contamina A
    sb = st(o["B"])
    assert sb.code == "falha"
    assert o["B"]["last_message"] == "disco cheio"


# ---------------------------------------------------------- falha no job
def test_job_level_failure_without_tasks_applies_to_all_vms():
    j = job("Construnivel", vms=["AD"])
    b = backup(j)
    s_old = session(j, 50, 49)
    s_new = session(j, 3, 2.9, "Failed", "Error: Cannot proceed with the job: meta file not synchronized")
    a = run([j], sessions=[s_old, s_new], tasks=[task(s_old, "AD", 50, 49)], backups=[b], rps=[rp(b, "AD", 20)])
    o = by_name(a)["AD"]
    s = st(o)
    assert s.code == "falha"
    assert "Cannot proceed" in o["last_message"]
    assert "nível do job" in " ".join(s.reasons)


def test_job_level_failure_older_than_task_is_ignored():
    j = job("J", vms=["A"])
    b = backup(j)
    s_fail = session(j, 30, 29.9, "Failed", "erro antigo")
    s_ok = session(j, 5, 4)
    a = run([j], sessions=[s_fail, s_ok], tasks=[task(s_ok, "A", 5, 4)], backups=[b], rps=[rp(b, "A", 4)])
    assert st(by_name(a)["A"]).code == "ok"


# ------------------------------------------------------------------- RPO
def test_success_but_old_restore_point_is_sem_backup():
    j = job("J", vms=["A"])
    b = backup(j)
    s = session(j, 5, 4)
    a = run([j], sessions=[s], tasks=[task(s, "A", 5, 4)], backups=[b], rps=[rp(b, "A", 100)])
    r = st(by_name(a)["A"])
    assert r.code == "sem_backup"
    assert "Sucesso" in " ".join(r.reasons)


def test_vm_in_job_without_any_restore_point_is_sem_backup():
    j = job("J", vms=["Nova"])
    a = run([j], backups=[backup(j)])
    r = st(by_name(a)["Nova"])
    assert r.code == "sem_backup" and "Não existe nenhum ponto" in r.reasons[0]


def test_rpo_precedence_object_over_job_over_default():
    j = job("Semanal", vms=["A"])
    b = backup(j)
    s = session(j, 101, 100)
    a = run([j], sessions=[s], tasks=[task(s, "A", 101, 100)], backups=[b], rps=[rp(b, "A", 100)])
    o = by_name(a)["A"]
    assert st(o).code == "sem_backup"
    assert st(o, job_meta={"rpo_hours": 200}).code == "ok"
    assert st(o, meta={"rpo_hours": 50}, job_meta={"rpo_hours": 200}).code == "sem_backup"


# ----------------------------------------------------------------- execução
def test_running_job_keeps_previous_result_and_counts_finished_vm_in_current_run():
    j = job("J", vms=["A", "B"])
    b = backup(j)
    prev = session(j, 30, 28, "Failed", "Processing B")
    cur = session(j, 1, None, "None", state_="Working", progress=60)
    t = [task(prev, "A", 30, 29), task(prev, "B", 30, 28, "Failed", "timeout"),
         task(cur, "A", 1, 0.5), task(cur, "B", 0.5, None, "None", state_="Working")]
    a = run([j], states=[state(j, status="running", last_result="None")], sessions=[prev, cur], tasks=t,
            backups=[b], rps=[rp(b, "A", 0.5), rp(b, "B", 29)])
    o = by_name(a)
    assert a.jobs[0]["running"] and a.jobs[0]["running_progress"] == 60
    assert a.jobs[0]["last_result"] == "Failed"           # console: resultado da última finalizada
    assert st(o["A"]).code == "ok"                         # A já terminou nesta execução
    assert o["B"]["running"] and st(o["B"]).code == "falha"  # B processando, último resultado = falha


def test_running_flag_comes_only_from_current_collection():
    j = job("J", vms=["A"])
    b = backup(j)
    stale_working = session(j, 50, None, "None", state_="Working")
    a = run([j], sessions=[stale_working], backups=[b], rps=[rp(b, "A", 2)], running=set())
    assert not a.jobs[0]["running"]


# --------------------------------------------------------------- inventário
def test_host_include_discovers_vms_from_tasks_and_restore_points():
    j = job("Compasi.08", hosts=["192.168.0.8"])
    b = backup(j)
    s = session(j, 5, 4)
    a = run([j], sessions=[s], tasks=[task(s, "Windows-Cancelas", 5, 4)], backups=[b],
            rps=[rp(b, "Windows-Cancelas", 4), rp(b, "Ubuntu-Unifi", 500)])
    o = by_name(a)
    assert set(o) == {"Windows-Cancelas", "Ubuntu-Unifi"}
    assert st(o["Windows-Cancelas"]).code == "ok"
    assert st(o["Ubuntu-Unifi"]).code == "sem_backup"
    assert a.jobs[0]["containers"] == ["Host inteiro: 192.168.0.8"]


def test_same_vm_in_two_jobs_gives_two_objects():
    j1, j2 = job("Compasi", vms=["PfSense"]), job("Systemup", vms=["PfSense"])
    b1, b2 = backup(j1), backup(j2)
    s1, s2 = session(j1, 4, 3), session(j2, 4, 3, "Failed", "VM indisponível")
    a = run([j1, j2], sessions=[s1, s2], tasks=[task(s1, "PfSense", 4, 3), task(s2, "PfSense", 4, 3, "Failed", "x")],
            backups=[b1, b2], rps=[rp(b1, "PfSense", 3), rp(b2, "PfSense", 300)])
    codes = sorted((o["job_name"], st(o).code) for o in a.objects)
    assert codes == [("Compasi", "ok"), ("Systemup", "sem_backup")]


def test_vm_removed_from_job_but_with_points_still_listed():
    j = job("J", vms=["Nova"])
    b = backup(j)
    a = run([j], backups=[b], rps=[rp(b, "Nova", 2), rp(b, "Antiga", 900)])
    o = by_name(a)
    assert not o["Antiga"]["in_definition"]
    r = st(o["Antiga"])
    assert r.code == "sem_backup" and any("não está mais na lista" in x for x in r.reasons)


# ----------------------------------------------------------- agente/órfão
def test_standalone_agent_judged_by_restore_point():
    b = backup(name="Job PCP-01", platform="WindowsPhysical", job_id="aaaaaaaa-0000-0000-0000-000000000001")
    a = run(backups=[b], rps=[rp(b, "PCP-01", 30)])
    o = a.objects[0]
    assert o["kind"] == "agent"
    assert st(o).code == "ok"                       # 30 h < RPO de agente 72 h
    a2 = run(backups=[b], rps=[rp(b, "PCP-01", 100)])
    assert st(a2.objects[0]).code == "sem_backup"


def test_agent_backup_without_points_is_listed():
    b = backup(name="Job Vazio", platform="WindowsPhysical", job_id="aaaaaaaa-0000-0000-0000-000000000002")
    a = run(backups=[b])
    assert len(a.objects) == 1 and st(a.objects[0]).code == "sem_backup"


def test_orphan_vmware_backup():
    b = backup(name="11 - CBHIDRO-DADOS")
    a = run(backups=[b], rps=[rp(b, "Dados", 20000)])
    o = a.objects[0]
    assert o["kind"] == "orphan"
    r = st(o)
    assert r.code == "sem_backup" and any("job foi apagado" in x for x in r.reasons)


# ----------------------------------------------------- desativado / ignorado
def test_disabled_job():
    j = job("J", vms=["A"], disabled=True)
    b = backup(j)
    a = run([j], backups=[b], rps=[rp(b, "A", 5)])
    assert st(by_name(a)["A"]).code == "desativado"
    a2 = run([j], backups=[b], rps=[rp(b, "A", 500)])
    r = st(by_name(a2)["A"])
    assert r.code == "sem_backup" and any("DESATIVADO" in x for x in r.reasons)


def test_ignored_wins_and_shows_reason():
    j = job("J", vms=["A"])
    a = run([j], backups=[backup(j)])
    r = st(by_name(a)["A"], meta={"ignored": 1, "ignored_reason": "desligada", "ignored_by": "kailo",
                                  "ignored_at": "2026-11-01T10:00:00+00:00"})
    assert r.code == "ignorado" and "desligada" in r.reasons[0]


def test_vm_with_point_but_no_session_in_history_is_alert():
    j = job("J", vms=["A"])
    b = backup(j)
    a = run([j], backups=[b], rps=[rp(b, "A", 3)])
    r = st(by_name(a)["A"])
    assert r.code == "alerta" and any("Não há resultado" in x for x in r.reasons)


def test_warning_and_malware():
    j = job("J", vms=["A", "B"])
    b = backup(j)
    s = session(j, 5, 4, "Warning", "low disk")
    a = run([j], sessions=[s], tasks=[task(s, "A", 5, 4, "Warning", "low disk"), task(s, "B", 5, 4)],
            backups=[b], rps=[rp(b, "A", 4), rp(b, "B", 4, malware="Infected")])
    o = by_name(a)
    assert st(o["A"]).code == "alerta"
    assert st(o["B"]).code == "falha"


# ------------------------------------------------------------------- miscel.
def test_timestamp_parsing_veeam_formats():
    assert parse_ts("2026-11-19T10:19:24.64051-03:00").isoformat() == "2026-11-19T13:19:24.640510+00:00"
    assert parse_ts("2026-11-19T10:19:24.1234567-03:00").microsecond == 123456
    assert parse_ts("2026-11-19T13:00:00Z").hour == 13
    assert parse_ts(None) is None and parse_ts("lixo") is None


def test_schedule_texts():
    assert schedule_text(job("x", daily="21:00"))[0] == "Diário às 21:00"
    t, scheduled = schedule_text(job("x", after="9 - BD Voltaic"))
    assert t == 'Após o job "9 - BD Voltaic"' and scheduled
    j = job("x")
    j["schedule"]["runAutomatically"] = False
    assert schedule_text(j) == ("Não agendado (execução manual)", False)


def test_states_without_job_definition_warns():
    j = job("J")
    a = analyze(jobs=[], job_states=[state(j)], sessions=[], task_sessions=[], backups=[],
                backup_objects=[], restore_points=[])
    assert any("não em /jobs" in w for w in a.warnings)


# ------------------------------------------- formato real do Veeam 12.3 (dump 24/11)
def test_real_task_format_naive_time_and_1900_end():
    assert parse_ts("1900-01-01T00:00:00") is None
    # task sessions vêm sem fuso: horário local do servidor (São Paulo)
    assert parse_ts("2026-11-24T11:35:05.662475").isoformat() == "2026-11-24T14:35:05.662475+00:00"


def test_task_finished_inside_running_session_counts():
    """Na sessão em execução o Veeam deixa state=Working em tarefa que já terminou."""
    j = job("Acrel", vms=["Srv-Flex-Web", "Srv-BD-Flex"])
    b = backup(j)
    cur = session(j, 10, None, "None", state_="Working", progress=40)
    t1 = task(cur, "Srv-Flex-Web", 9, 8, "Success", state_="Working")
    t2 = task(cur, "Srv-BD-Flex", 9, 7, "Failed", "NFS4.1 error", state_="Working")
    t3 = task(cur, "Srv-BD-Flex", 1, None, "None", state_="Working")
    t3["endTime"] = "1900-01-01T00:00:00"
    a = run([j], states=[state(j, status="running", last_result="None")], sessions=[cur], tasks=[t1, t2, t3],
            backups=[b], rps=[rp(b, "Srv-Flex-Web", 8), rp(b, "Srv-BD-Flex", 20)])
    o = by_name(a)
    assert st(o["Srv-Flex-Web"]).code == "ok"
    assert o["Srv-BD-Flex"]["last_result"] == "Failed" and o["Srv-BD-Flex"]["running"]


def test_point_created_during_failed_run_is_flagged():
    j = job("Laticinio", vms=["AD-SRV"])
    b = backup(j)
    s = session(j, 3, 2, "Failed", "Processing AD-SRV")
    a = run([j], sessions=[s], tasks=[task(s, "AD-SRV", 3, 2, "Failed", "SSL_ERROR_SYSCALL")],
            backups=[b], rps=[rp(b, "AD-SRV", 2.5), rp(b, "AD-SRV", 100)])
    o = by_name(a)["AD-SRV"]
    assert o["last_rp_in_failed_run"]
    r = st(o)
    assert r.code == "falha"
    assert any("pode estar incompleto" in x for x in r.reasons)
