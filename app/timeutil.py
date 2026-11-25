from __future__ import annotations

import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

LOCAL_TZ = ZoneInfo("America/Sao_Paulo")
_FRAC = re.compile(r"\.(\d+)")


def parse_ts(value) -> datetime | None:
    """Converte timestamp do Veeam para datetime com fuso (UTC).

    Aceita frações com qualquer número de dígitos (o Veeam manda 5 ou 7),
    'Z' e offsets. Sem offset, assume o horário local de São Paulo.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        s = str(value).strip()
        if not s:
            return None
        s = s.replace("Z", "+00:00").replace(" ", "T", 1)
        s = _FRAC.sub(lambda m: "." + (m.group(1) + "000000")[:6], s, count=1)
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            return None
    if dt.year < 1971:  # o Veeam usa 1900-01-01 como "sem data" (ex.: tarefa ainda rodando)
        return None
    if dt.tzinfo is None:
        # Task sessions vêm sem fuso: é o horário local do servidor do Veeam.
        dt = dt.replace(tzinfo=LOCAL_TZ)
    return dt.astimezone(timezone.utc)


def to_db(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds") if dt else None


def from_db(s: str | None) -> datetime | None:
    if not s:
        return None
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def fmt_local(dt: datetime | None, with_seconds: bool = False) -> str:
    if not dt:
        return "—"
    return dt.astimezone(LOCAL_TZ).strftime("%d/%m/%Y %H:%M:%S" if with_seconds else "%d/%m/%Y %H:%M")


def fmt_age(dt: datetime | None, now: datetime | None = None) -> str:
    if not dt:
        return "nunca"
    now = now or now_utc()
    secs = (now - dt).total_seconds()
    if secs < 0:
        return "agora"
    mins = int(secs // 60)
    if mins < 60:
        return f"há {mins} min"
    hours = mins // 60
    if hours < 48:
        return f"há {hours} h"
    return f"há {hours // 24} dias"
