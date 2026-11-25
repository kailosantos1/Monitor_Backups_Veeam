"""Regra única de status, calculada na hora de exibir (com o relógio atual).

Ordem de avaliação (a primeira que casar vence):

  1. IGNORADO    -> um admin marcou o objeto como ignorado (com motivo).
  2. SEM BACKUP  -> não existe ponto de restauração, ou o mais novo é mais
                    velho que o RPO do objeto. É o alarme principal: vale
                    independentemente de qualquer resultado de sessão.
  3. DESATIVADO  -> o job está desativado no Veeam (ainda dentro do RPO).
  4. FALHA       -> último resultado Failed, ou o ponto mais novo foi
                    marcado como Infected pelo Veeam.
  5. ALERTA      -> último resultado Warning, sessão terminou sem resultado,
                    ou ponto mais novo marcado como Suspicious.
  6. OK          -> último resultado Success, ou agente com ponto recente
                    (para Veeam Agent standalone a API não expõe sessões; a
                    única evidência confiável é o ponto de restauração).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .timeutil import fmt_local, from_db

ORDER = ["sem_backup", "falha", "alerta", "desativado", "executando", "ok", "ignorado"]
LABELS = {
    "sem_backup": "SEM BACKUP",
    "falha": "FALHA",
    "alerta": "ALERTA",
    "desativado": "DESATIVADO",
    "executando": "EM EXECUÇÃO",
    "ok": "OK",
    "ignorado": "IGNORADO",
}
PROBLEM = {"sem_backup", "falha", "alerta", "desativado"}
RESULT_PT = {"Success": "Sucesso", "Warning": "Alerta", "Failed": "Falha", "None": "Sem resultado"}


@dataclass
class Status:
    code: str
    reasons: list[str] = field(default_factory=list)
    rpo_hours: float = 0
    rpo_source: str = ""
    rp_age_hours: float | None = None
    summary: str = ""
    # Veeam está trabalhando nesta máquina agora (processando, ou na fila de um
    # retry). Só muda a ETIQUETA exibida; o código acima continua valendo para
    # contagens e para a aba "Atenção".
    active: bool = False

    @property
    def label(self) -> str:
        return LABELS[self.code]

    @property
    def rank(self) -> int:
        return ORDER.index(self.code)

    @property
    def is_problem(self) -> bool:
        return self.code in PROBLEM


def effective_rpo(obj: dict, meta: dict | None, job_meta: dict | None,
                  rpo_vm: float, rpo_agent: float) -> tuple[float, str]:
    if meta and meta.get("rpo_hours"):
        return float(meta["rpo_hours"]), "definido no objeto"
    if job_meta and job_meta.get("rpo_hours"):
        return float(job_meta["rpo_hours"]), "definido no job"
    if obj.get("kind") == "agent":
        return rpo_agent, "padrão para agentes"
    return rpo_vm, "padrão para VMs"


def compute(obj: dict, meta: dict | None, job_meta: dict | None, now: datetime,
            rpo_vm: float, rpo_agent: float) -> Status:
    st = _compute(obj, meta, job_meta, now, rpo_vm, rpo_agent)
    if st.code != "ignorado":
        res_at = from_db(obj.get("last_result_at"))
        run_since = from_db(obj.get("job_running_since"))
        st.active = bool(obj.get("running")) or (
            bool(obj.get("job_running")) and obj.get("kind") == "vm" and obj.get("last_result") != "Success"
            and (res_at is None or (run_since is not None and res_at < run_since)))
    return st


def _compute(obj: dict, meta: dict | None, job_meta: dict | None, now: datetime,
             rpo_vm: float, rpo_agent: float) -> Status:
    rpo, rpo_src = effective_rpo(obj, meta, job_meta, rpo_vm, rpo_agent)
    st = Status(code="ok", rpo_hours=rpo, rpo_source=rpo_src)
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

    # 2. RPO
    if last_rp is None:
        st.code = "sem_backup"
        r.append("Não existe nenhum ponto de restauração para esta máquina no Veeam.")
    elif st.rp_age_hours > rpo:
        st.code = "sem_backup"
        r.append(f"Último ponto de restauração em {fmt_local(last_rp)} ({_h(st.rp_age_hours)} atrás); "
                 f"o limite (RPO) é {_h(rpo)}.")
    if st.code == "sem_backup":
        if obj.get("running") or obj.get("job_running"):
            r.append("O job está em execução agora, mas isso não muda o status: a máquina já passou do RPO sem ponto novo.")
        # Resumo curto: só o que a coluna "último backup" ainda não diz.
        if res in ("Failed", "Warning") and obj.get("last_message"):
            st.summary = obj["last_message"]
        elif context:
            st.summary = context[0]
        else:
            st.summary = ("Nenhum ponto de restauração" if last_rp is None
                          else f"Passou do limite de {_h(rpo)}")
        if res_txt:
            r.append(res_txt)
        r.extend(context)
        return st

    r.append(f"Último ponto de restauração em {fmt_local(last_rp)} ({_h(st.rp_age_hours)} atrás, RPO {_h(rpo)}).")
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
    # DENTRO do RPO: execução que se arrasta sem gerar ponto já virou SEM BACKUP.
    run_since = from_db(obj.get("job_running_since"))
    # "Na fila": só para quem não teve sucesso na última vez. Quem deu certo
    # continua OK (num retry o Veeam nem vai reprocessá-la).
    waiting = (bool(obj.get("job_running")) and obj.get("kind") == "vm" and res != "Success"
               and (res_at is None or (run_since and res_at < run_since)))
    # Se o ponto mais novo veio de uma execução que falhou, só mostramos
    # "em execução" quando o último ponto BOM também está dentro do RPO.
    good = from_db(obj.get("last_good_rp_at")) if obj.get("last_rp_in_failed_run") else last_rp
    good_ok = good is not None and (now - good).total_seconds() / 3600 <= rpo
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
    return f"{hours / 24:.1f} dias".replace(".", ",")
