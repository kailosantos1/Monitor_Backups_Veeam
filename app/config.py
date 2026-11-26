"""Configuração lida do arquivo .env (ou variáveis de ambiente).

Toda configuração é validada na inicialização. Se algo obrigatório estiver
faltando ou inválido, o sistema NÃO sobe e diz exatamente o que corrigir.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent


class ConfigError(Exception):
    pass


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    v = raw.strip().lower()
    if v in ("1", "true", "sim", "yes", "on"):
        return True
    if v in ("0", "false", "nao", "não", "no", "off"):
        return False
    raise ConfigError(f"{name}: valor '{raw}' inválido (use true/false)")


def _num(name: str, default: float, minimum: float, cast=float):
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return cast(default)
    try:
        val = cast(raw.strip().replace(",", "."))
    except ValueError:
        raise ConfigError(f"{name}: '{raw}' não é um número")
    if val < minimum:
        raise ConfigError(f"{name}: valor {val} menor que o mínimo {minimum}")
    return val


@dataclass(frozen=True)
class Settings:
    app_host: str
    app_port: int
    secret_key: str
    session_hours: int
    db_path: Path

    veeam_url: str
    veeam_user: str
    veeam_password: str
    veeam_api_version: str
    veeam_verify_ssl: bool | str  # True/False ou caminho para o .crt da CA
    veeam_timeout: float
    veeam_page_size: int

    # Fonte de dados alternativa: diretório com JSONs brutos (só testes/homologação).
    replay_dir: Path | None

    sync_interval_minutes: int
    history_days: int

    # Sem ponto novo por mais que isso (ou 2x o intervalo do agendamento) = SEM BACKUP.
    sem_backup_days: float
    # Agentes standalone não informam agendamento na API: intervalo esperado padrão.
    agent_interval_hours: float

    @property
    def stale_after_minutes(self) -> int:
        """Depois de quanto tempo sem sync OK os dados são considerados desatualizados."""
        return self.sync_interval_minutes * 2 + 5


def load_settings(env_file: Path | None = None) -> Settings:
    load_dotenv(env_file or (BASE_DIR / ".env"), override=False)

    replay = os.getenv("VEEAM_REPLAY_DIR", "").strip()
    replay_dir = Path(replay) if replay else None

    url = os.getenv("VEEAM_URL", "").strip().rstrip("/")
    user = os.getenv("VEEAM_USER", "").strip()
    password = os.getenv("VEEAM_PASSWORD", "")

    if replay_dir is None:
        missing = [n for n, v in (("VEEAM_URL", url), ("VEEAM_USER", user), ("VEEAM_PASSWORD", password)) if not v]
        if missing:
            raise ConfigError("Configuração obrigatória ausente no .env: " + ", ".join(missing))
        if not url.startswith("https://"):
            raise ConfigError("VEEAM_URL deve começar com https:// (ex.: https://192.168.100.245:9419)")
    elif not replay_dir.is_dir():
        raise ConfigError(f"VEEAM_REPLAY_DIR aponta para '{replay_dir}', que não é um diretório")

    secret = os.getenv("SECRET_KEY", "").strip()
    if len(secret) < 32:
        raise ConfigError(
            "SECRET_KEY ausente ou curta (mínimo 32 caracteres). Gere uma com:\n"
            "  python -c \"import secrets; print(secrets.token_urlsafe(48))\""
        )

    verify_raw = os.getenv("VEEAM_VERIFY_SSL", "false").strip()
    if verify_raw.lower() in ("true", "false", "1", "0", "sim", "nao", "não", "yes", "no", ""):
        verify: bool | str = _bool("VEEAM_VERIFY_SSL", False)
    else:
        if not Path(verify_raw).is_file():
            raise ConfigError(f"VEEAM_VERIFY_SSL: arquivo de certificado '{verify_raw}' não encontrado")
        verify = verify_raw

    db = os.getenv("DB_PATH", "").strip()
    db_path = Path(db) if db else BASE_DIR / "data" / "monitor.db"

    return Settings(
        app_host=os.getenv("APP_HOST", "0.0.0.0").strip(),
        app_port=_num("APP_PORT", 8000, 1, int),
        secret_key=secret,
        session_hours=_num("SESSION_HOURS", 12, 1, int),
        db_path=db_path,
        veeam_url=url,
        veeam_user=user,
        veeam_password=password,
        veeam_api_version=os.getenv("VEEAM_API_VERSION", "1.2-rev1").strip(),
        veeam_verify_ssl=verify,
        veeam_timeout=_num("VEEAM_TIMEOUT_SECONDS", 60, 5),
        veeam_page_size=_num("VEEAM_PAGE_SIZE", 200, 10, int),
        replay_dir=replay_dir,
        sync_interval_minutes=_num("SYNC_INTERVAL_MINUTES", 10, 2, int),
        history_days=_num("HISTORY_DAYS", 30, 2, int),
        sem_backup_days=_num("SEM_BACKUP_DAYS", 7, 1),
        agent_interval_hours=_num("AGENT_INTERVAL_HOURS", 48, 1),
    )
