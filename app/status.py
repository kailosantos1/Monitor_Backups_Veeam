"""Regra única de status, calculada na hora de exibir (com o relógio atual).

O prazo de cada máquina vem do AGENDAMENTO do job no Veeam (diário, a cada
N horas, dias selecionados, "após o job X"...). Ordem (a primeira que casar vence):

  1. IGNORADO    -> um admin marcou o objeto como ignorado (com motivo).
  2. PARADO      -> o ponto mais novo é mais velho que o limite (padrão
                    7 dias, ou 2x o
                    intervalo do agendamento + 12 h, o que for maior).
     NUNCA TEVE BACKUP -> não existe nenhum ponto (mesmo código interno).
  3. EM EXECUÇÃO -> o Veeam está processando a máquina (ou ela está na fila
                    de um retry) e ainda existe ponto bom dentro do limite.
  4. DESATIVADO  -> o job está desativado no Veeam.
  5. FALHA       -> último resultado Failed, ou ponto marcado como Infected.
  6. ALERTA      -> último resultado Warning, sem resultado no histórico,
                    ponto Suspicious ou ponto criado numa execução que falhou.
  7. ATRASADO    -> o último resultado foi bom, mas o job perdeu o horário:
                    não há ponto novo dentro do intervalo do agendamento
                    + tolerância (máx(12 h, metade do intervalo)).
  8. OK          -> resultado bom e ponto em dia com o agendamento.
Agentes standalone não têm agendamento na API: usam um intervalo padrão
configurável e são avaliados só pelo ponto de restauração.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .timeutil import fmt_local, from_db

ORDER = ["sem_backup", "falha", "atrasado", "alerta", "desativado", "executando", "ok", "ignorado"]
LABELS = {
    "sem_backup": "PARADO",
    "falha": "FALHA",
    "atrasado": "ATRASADO",
    "alerta": "ALERTA",
    "desativado": "DESATIVADO",
    "executando": "EM EXECUÇÃO",
    "ok": "OK",
    "ignorado": "IGNORADO",
}
# Rótulos de tela. "sem_backup" tem dois nomes: "Nunca teve backup" quando não
# existe ponto nenhum, e "Parado" quando já teve, mas não há ponto novo há dias.
UI_LABEL = {"sem_backup": "Parado", "falha": "Falha", "atrasado": "Atrasado", "alerta": "Alerta",
            "desativado": "Desativado", "executando": "Em execução", "ok": "OK", "ignorado": "Ignorado"}
NEVER_LABEL = "Nunca teve backup"
PROBLEM = {"sem_backup", "falha", "atrasado", "alerta", "desativado"}
RESULT_PT = {"Success": "Sucesso", "Warning": "Alerta", "Failed": "Falha", "None": "Sem resultado"}


@dataclass
class Status:
    code: str
    reasons: list[str] = field(default_factory=list)
    interval_hours: float | None = None   # de quanto em quanto tempo deveria haver ponto novo
    interval_source: str = ""
    late_hours: float | None = None       # passou disso sem ponto novo -> ATRASADO
    sem_hours: float = 0                  # passou disso sem ponto novo -> SEM BACKUP
    rp_age_hours: float | None = None
    summary: str = ""
    # Veeam está trabalhando nesta máquina agora (processando, ou na fila de um
    # retry). Só muda a ETIQUETA exibida; o código acima continua valendo para
    # contagens e para a aba "Atenção".
    active: bool = False
    never: bool = False                   # não existe nenhum ponto de restauração

    @property
    def label(self) -> str:
        return NEVER_LABEL.upper() if self.never and self.code == "sem_backup" else LABELS[self.code]

    @property
    def ui(self) -> str:
        return NEVER_LABEL if self.never and self.code == "sem_backup" else UI_LABEL[self.code]

    @property
    def rank(self) -> int:
        return ORDER.index(self.code)

    @property
    def is_problem(self) -> bool:
        return self.code in PROBLEM


def expected_interval(obj: dict, meta: dict | None, job_meta: dict | None,
                      agent_hours: float) -> tuple[float | None, str]:
    """De quanto em quanto tempo esta máquina deveria ganhar ponto novo."""
    if meta and meta.get("rpo_hours"):
        return float(meta["rpo_hours"]), "definido na máquina"
    if job_meta and job_meta.get("rpo_hours"):
        return float(job_meta["rpo_hours"]), "definido no job"
    if obj.get("kind") == "agent":
        return agent_hours, "padrão para agentes (a API não informa o agendamento deles)"
    if obj.get("expected_hours"):
        return float(obj["expected_hours"]), f"agendamento do Veeam: {obj.get('schedule_source') or 'job'}"
    return None, "sem agendamento automático"


def limits(interval: float | None, sem_backup_days: float) -> tuple[float | None, float]:
    sem_floor = sem_backup_days * 24
    if not interval:
        return None, sem_floor
    late = interval + max(12.0, interval / 2)
    return late, max(sem_floor, 2 * interval + 12)


def compute(obj: dict, meta: dict | None, job_meta: dict | None, now: datetime,
            sem_backup_days: float, agent_hours: float) -> Status:
    st = _compute(obj, meta, job_meta, now, sem_backup_days, agent_hours)
    if st.code != "ignorado":
        res_at = from_db(obj.get("last_result_at"))
        run_since = from_db(obj.get("job_running_since"))
        st.active = bool(obj.get("running")) or (
            bool(obj.get("job_running")) and obj.get("kind") == "vm" and obj.get("last_result") != "Success"
            and (res_at is None or (run_since is not None and res_at < run_since)))
    return st


def _compute(obj: dict, meta: dict | None, job_meta: dict | None, now: datetime,
             sem_backup_days: float, agent_hours: float) -> Status:
    interval, src = expected_interval(obj, meta, job_meta, agent_hours)
    late, sem = limits(interval, sem_backup_days)
    st = Status(code="ok", interval_hours=interval, interval_source=src, late_hours=late, sem_hours=sem)
    r = st.reasons

    last_rp = from_db(obj.get("last_rp_at"))
    if last_rp:
        st.rp_age_hours = (now - last_rp).total_seconds() / 3600

    res = obj.get("last_result")
    res_at = from_db(obj.get("last_result_at"))
    res_txt = None
    if res:
        res_txt = f"Último resultado no Veeam: {RESULT_PT.get(res, res)} em {fmt_local(res_at)}"
        if obj.get("last_result_source") and "job" in obj["last_result_source"]:
            res_txt += " (falha no nível do job, antes de processar as VMs)"
        if obj.get("last_message") and res != "Success":
            res_txt += f" — {obj['last_message']}"

    if meta and meta.get("ignored"):
        st.code = "ignorado"
        st.summary = meta.get("ignored_reason") or ""
        r.append(f"Ignorado por {meta.get('ignored_by') or '?'} em {fmt_local(from_db(meta.get('ignored_at')))}: "
                 f"{meta.get('ignored_reason') or 'sem motivo informado'}")
        return st

    context = []
    if obj.get("job_disabled"):
        context.append("O job está DESATIVADO no Veeam.")
    elif obj.get("kind") == "vm" and not obj.get("job_scheduled"):
        context.append("O job não tem agendamento automático ativo.")
    if obj.get("kind") == "vm" and not obj.get("in_definition") and not obj.get("job_has_containers"):
        context.append("Esta máquina não está mais na lista do job; só restam pontos antigos.")
    if obj.get("excluded"):
        context.append("Esta máquina está na lista de EXCLUSÕES do job.")
    if obj.get("kind") == "orphan":
        context.append("Backup sem job no Veeam (o job foi apagado); não recebe novos pontos.")

    # 2. SEM BACKUP
    if last_rp is None:
        st.code = "sem_backup"
        st.never = True
        r.append("Não existe nenhum ponto de restauração para esta máquina no Veeam: ela nunca teve backup (ou todos foram apagados).")
    elif st.rp_age_hours > sem:
        st.code = "sem_backup"
        r.append(f"Último ponto de restauração em {fmt_local(last_rp)} ({_h(st.rp_age_hours)} atrás); "
                 f"o backup está PARADO (sem ponto novo por mais de {_h(sem)}).")
    if st.code == "sem_backup":
        if obj.get("running") or obj.get("job_running"):
            r.append("O job está em execução agora, mas isso não muda o status: a máquina já passou do limite sem ponto novo.")
        if res in ("Failed", "Warning") and obj.get("last_message"):
            st.summary = obj["last_message"]
        elif context:
            st.summary = context[0]
        else:
            st.summary = ("Nenhum ponto de restauração" if last_rp is None
                          else f"Mais de {_h(sem)} sem backup novo")
        if res_txt:
            r.append(res_txt)
        r.extend(context)
        return st

    if interval:
        r.append(f"Último ponto de restauração em {fmt_local(last_rp)} ({_h(st.rp_age_hours)} atrás). "
                 f"Esperado: ponto novo a cada {_h(interval)} ({src}).")
    else:
        r.append(f"Último ponto de restauração em {fmt_local(last_rp)} ({_h(st.rp_age_hours)} atrás). "
                 "O job não tem agendamento automático, então não há horário esperado.")
    if obj.get("last_rp_in_failed_run"):
        good = from_db(obj.get("last_good_rp_at"))
        r.append("ATENÇÃO: esse ponto foi criado durante uma execução desta máquina que FALHOU e pode estar "
                 "incompleto (o Veeam costuma removê-lo na próxima execução). Último ponto fora de execução com falha: "
                 + (fmt_local(good) + f" ({_h((now - good).total_seconds() / 3600)} atrás)." if good else "nenhum."))

    # 3. desativado
    if obj.get("job_disabled"):
        st.code = "desativado"
        st.summary = "Job desativado no Veeam"
        r.extend(context)
        if res_txt:
            r.append(res_txt)
        return st

    # Em execução: a VM está sendo processada agora, ou está na fila do job que
    # está rodando e ainda não terminou nesta execução. Só chega aqui quem está
    # dentro do limite: execução que se arrasta sem gerar ponto já virou SEM BACKUP.
    run_since = from_db(obj.get("job_running_since"))
    # "Na fila": só para quem não teve sucesso na última vez. Quem deu certo
    # continua OK (num retry o Veeam nem vai reprocessá-la).
    waiting = (bool(obj.get("job_running")) and obj.get("kind") == "vm" and res != "Success"
               and (res_at is None or (run_since and res_at < run_since)))
    # Se o ponto mais novo veio de uma execução que falhou, só mostramos
    # "em execução" quando o último ponto BOM também está dentro do limite.
    good = from_db(obj.get("last_good_rp_at")) if obj.get("last_rp_in_failed_run") else last_rp
    good_ok = good is not None and (now - good).total_seconds() / 3600 <= sem
    if (obj.get("running") or waiting) and good_ok:
        st.code = "executando"
        prev = f"anterior: {RESULT_PT.get(res, res)} em {fmt_local(res_at)}" if res else "sem resultado anterior"
        st.summary = ("Processando agora" if obj.get("running") else "Na fila do job em execução") + f" · {prev}"
        r.append("O Veeam está " + ("processando esta máquina agora." if obj.get("running")
                 else "rodando o job desta máquina; ela ainda não foi processada nesta execução."))
        if res_txt:
            r.append(res_txt.replace("Último resultado no Veeam", "Resultado anterior"))
        r.extend(context)
        return st

    malware = (obj.get("last_rp_malware") or "").strip()
    # 4/5. resultado
    if res == "Failed":
        st.code = "falha"
    elif res == "Warning":
        st.code = "alerta"
    elif res == "None" or (res is None and obj.get("kind") == "vm"):
        st.code = "alerta"
        r.append("Não há resultado de sessão finalizada para esta máquina no período de histórico.")
    if malware == "Infected":
        st.code = "falha"
        r.append("O Veeam marcou o ponto de restauração mais recente como INFECTADO.")
    elif malware == "Suspicious" and st.code == "ok":
        st.code = "alerta"
        r.append("O Veeam marcou o ponto de restauração mais recente como SUSPEITO.")

    if st.code in ("falha", "alerta"):
        st.summary = obj.get("last_message") or next((x for x in r if "INFECT" in x or "SUSPEITO" in x or "Não há resultado" in x), "")
    if obj.get("last_rp_in_failed_run") and st.code == "ok":
        st.code = "alerta"
    # 7. ATRASADO: resultado bom, mas o job perdeu o horário.
    if late is not None and st.rp_age_hours > late:
        msg = (f"Atrasado: o agendamento prevê ponto novo a cada {_h(interval)}, "
               f"mas o último é de {_h(st.rp_age_hours)} atrás (tolerância {_h(late)}).")
        r.append(msg)
        if st.code == "ok":
            st.code = "atrasado"
            st.summary = f"Sem ponto novo há {_h(st.rp_age_hours)}; esperado a cada {_h(interval)}"
            if obj.get("job_last_result") in ("Failed", "Warning") or obj.get("job_running"):
                st.summary += " · " + ("job em execução" if obj.get("job_running") else "o job não concluiu")
    if res_txt:
        r.append(res_txt)
    elif obj.get("kind") == "agent":
        r.append("Agente standalone: a API do Veeam não expõe o resultado das sessões; "
                 "status avaliado pelo ponto de restauração.")
    r.extend(context)
    return st


def _h(hours: float) -> str:
    if hours < 48:
        return f"{hours:.0f} h"
    d = hours / 24
    if abs(d - round(d)) < 0.05:
        return f"{round(d)} dias"
    return f"{d:.1f} dias".replace(".", ",")
