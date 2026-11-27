"""Cliente somente-leitura da REST API do Veeam Backup & Replication.

Garantias:
- Toda listagem paginada é conferida contra o `pagination.total` devolvido
  pelo próprio Veeam. Se o número de itens únicos recebidos não bater, a
  coleta é repetida; se continuar sem bater, levanta IncompleteDataError.
  Nunca devolve lista pela metade.
- Qualquer erro HTTP, de rede, de TLS ou de JSON vira VeeamError com a
  mensagem exata. Não existe fallback silencioso nem dado simulado.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

import requests
import urllib3

log = logging.getLogger("veeam.client")


class VeeamError(Exception):
    """Erro ao falar com o Veeam (rede, autenticação, HTTP, formato)."""


class VeeamAuthError(VeeamError):
    pass


class EndpointNotFound(VeeamError):
    """O endpoint não existe nesta versão do Veeam (HTTP 404)."""


class IncompleteDataError(VeeamError):
    """A paginação não fechou com o total informado pelo Veeam."""


class VeeamClient:
    def __init__(self, url: str, user: str, password: str, api_version: str,
                 verify_ssl: bool | str, timeout: float, page_size: int = 200):
        self.base = url.rstrip("/")
        self.user = user
        self.password = password
        self.api_version = api_version
        self.verify = verify_ssl
        self.timeout = (10.0, timeout)
        self.page_size = page_size
        self._s = requests.Session()
        self._token: str | None = None
        self._refresh: str | None = None
        self._token_exp = 0.0
        self.request_count = 0
        if verify_ssl is False:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    # ------------------------------------------------------------------ auth
    def close(self) -> None:
        if self._token:
            try:
                self._s.post(f"{self.base}/api/oauth2/logout", headers=self._headers(),
                             verify=self.verify, timeout=self.timeout)
            except requests.RequestException:
                pass
        self._s.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _headers(self) -> dict[str, str]:
        h = {"x-api-version": self.api_version, "Accept": "application/json"}
        if self._token:
            h["Authorization"] = f"Bearer {self._token}"
        return h

    def login(self) -> None:
        data = {"grant_type": "password", "username": self.user, "password": self.password}
        self._token_request(data)

    def _token_request(self, data: dict[str, str]) -> None:
        url = f"{self.base}/api/oauth2/token"
        try:
            r = self._s.post(url, data=data, headers={"x-api-version": self.api_version},
                             verify=self.verify, timeout=self.timeout)
        except requests.exceptions.SSLError as e:
            raise VeeamError(f"Erro de certificado TLS ao conectar em {self.base}: {e}") from e
        except requests.RequestException as e:
            raise VeeamError(f"Sem conexão com o Veeam em {self.base}: {e}") from e
        self.request_count += 1
        if r.status_code != 200:
            raise VeeamAuthError(f"Login no Veeam recusado (HTTP {r.status_code}): {r.text[:400]}")
        try:
            body = r.json()
        except ValueError as e:
            raise VeeamAuthError("Resposta de login do Veeam não é JSON") from e
        tok = body.get("access_token")
        if not tok:
            raise VeeamAuthError("Resposta de login do Veeam sem access_token")
        self._token = tok
        self._refresh = body.get("refresh_token")
        # Renova com folga de 60 s antes de expirar.
        self._token_exp = time.monotonic() + max(int(body.get("expires_in") or 900) - 60, 30)

    def _ensure_token(self) -> None:
        if self._token and time.monotonic() < self._token_exp:
            return
        if self._refresh:
            try:
                self._token_request({"grant_type": "refresh_token", "refresh_token": self._refresh})
                return
            except VeeamError:
                log.info("refresh_token recusado; fazendo login novamente")
        self.login()

    # ------------------------------------------------------------------ http
    def _get(self, path: str, params: dict[str, Any] | None) -> Any:
        self._ensure_token()
        url = f"{self.base}{path}"
        for attempt in (1, 2):
            try:
                r = self._s.get(url, headers=self._headers(), params=params,
                                verify=self.verify, timeout=self.timeout)
            except requests.RequestException as e:
                if attempt == 1:
                    log.warning("Falha de rede em %s (%s); repetindo uma vez", path, e)
                    time.sleep(2)
                    continue
                raise VeeamError(f"Falha de rede ao consultar {path}: {e}") from e
            self.request_count += 1
            if r.status_code == 401 and attempt == 1:
                self._token = None
                self._refresh = None
                self.login()
                continue
            if r.status_code in (502, 503, 504) and attempt == 1:
                time.sleep(3)
                continue
            if r.status_code == 404:
                raise EndpointNotFound(f"{path} não existe nesta versão do Veeam (HTTP 404)")
            if r.status_code != 200:
                raise VeeamError(f"{path} respondeu HTTP {r.status_code}: {r.text[:400]}")
            try:
                return r.json()
            except ValueError as e:
                raise VeeamError(f"{path} devolveu conteúdo que não é JSON") from e
        raise VeeamError(f"{path}: não foi possível obter resposta")

    def get_all(self, path: str, params: dict[str, Any] | None = None) -> list[dict]:
        """Lista paginada completa e conferida. Ver docstring do módulo."""
        last_problem = ""
        for attempt in range(1, 4):
            items, total, first_total = self._page_through(path, params)
            unique: dict[str, dict] = {}
            no_id: list[dict] = []
            dupes = 0
            for it in items:
                iid = it.get("id") if isinstance(it, dict) else None
                if iid:
                    if iid in unique:
                        dupes += 1
                    unique[iid] = it
                else:
                    no_id.append(it)
            result = list(unique.values()) + no_id
            if total is None or len(result) == total:
                return result
            if dupes and total:
                # Ordenação instável (empates no campo de ordem) faz um item
                # se repetir na fronteira entre páginas e outro sumir. Sem
                # fronteira não há o problema: pede tudo numa página só.
                single = self._single_page(path, params, total)
                if single is not None:
                    return single
            last_problem = (f"recebidos {len(result)} itens únicos, Veeam informou total={total}"
                            f"; {dupes} repetido(s) entre páginas; total na 1ª página={first_total}")
            log.warning("%s: %s (tentativa %d/3)", path, last_problem, attempt)
            time.sleep(2 * attempt)
        raise IncompleteDataError(f"{path}: paginação não fechou após 3 tentativas ({last_problem})")

    def _single_page(self, path: str, params: dict[str, Any] | None, total: int) -> list[dict] | None:
        """Busca a lista inteira numa requisição. None se não fechar com o total."""
        q = dict(params or {})
        q["skip"] = 0
        q["limit"] = total + 50  # folga para itens criados agora
        body = self._get(path, q)
        if not isinstance(body, dict) or not isinstance(body.get("data"), list):
            return None
        data = body["data"]
        new_total = (body.get("pagination") or {}).get("total")
        if isinstance(new_total, int) and len(data) == new_total:
            # Sem fronteira de página, repetição só pode vir do próprio Veeam
            # (visto em sessões "Configuration Database Resynchronize"): o
            # total conta a cópia. Remove a cópia e avisa.
            unique: dict[str, dict] = {}
            no_id: list[dict] = []
            for it in data:
                iid = it.get("id") if isinstance(it, dict) else None
                if iid:
                    if iid in unique:
                        log.warning("%s: Veeam devolveu o id %s duplicado (%s); mantida uma cópia",
                                    path, iid, it.get("name"))
                    unique[iid] = it
                else:
                    no_id.append(it)
            log.info("%s: lista completa obtida em uma requisição (%d itens, %d únicos)",
                     path, len(data), len(unique) + len(no_id))
            return list(unique.values()) + no_id
        log.warning("%s: requisição única trouxe %d de total=%s (limite do servidor?)",
                    path, len(data), new_total)
        return None

    def get_unpaged(self, path: str) -> list[dict]:
        """Para endpoints sem parâmetros de paginação (ex.: /sessions/{id}/taskSessions)."""
        body = self._get(path, None)
        if isinstance(body, list):
            return body
        if not isinstance(body, dict) or not isinstance(body.get("data"), list):
            raise VeeamError(f"{path}: formato inesperado (sem campo 'data')")
        data = body["data"]
        total = (body.get("pagination") or {}).get("total")
        if isinstance(total, int) and total > len(data):
            raise IncompleteDataError(f"{path}: veio {len(data)} de {total} itens")
        return data

    def _page_through(self, path: str, params: dict[str, Any] | None
                      ) -> tuple[list[dict], int | None, int | None]:
        out: list[dict] = []
        skip = 0
        total: int | None = None
        first_total: int | None = None
        for _ in range(100000):
            q = dict(params or {})
            q["skip"] = skip
            q["limit"] = self.page_size
            body = self._get(path, q)
            if isinstance(body, list):  # endpoint sem paginação
                return body, len(body), len(body)
            if not isinstance(body, dict) or not isinstance(body.get("data"), list):
                raise VeeamError(f"{path}: formato inesperado (sem campo 'data')")
            page = body["data"]
            pag = body.get("pagination") or {}
            if isinstance(pag.get("total"), int):
                total = pag["total"]
                if first_total is None:
                    first_total = total
            out.extend(page)
            skip += len(page)
            if not page or len(page) < self.page_size or (total is not None and skip >= total):
                return out, total, first_total
        raise VeeamError(f"{path}: paginação não terminou")


class ReplaySource:
    """Lê respostas brutas gravadas em disco (mesmo formato da API).

    Usado só em testes/homologação (VEEAM_REPLAY_DIR). Arquivo ausente =
    erro, exatamente como um endpoint fora do ar.
    """

    FILES = {
        "/api/v1/jobs": "jobs.json",
        "/api/v1/jobs/states": "jobs_states.json",
        "/api/v1/sessions": "sessions.json",
        "/api/v1/taskSessions": "taskSessions.json",
        "/api/v1/backups": "backups.json",
        "/api/v1/backupObjects": "backupObjects.json",
        "/api/v1/restorePoints": "restorePoints.json",
        "/api/v1/backupInfrastructure/managedServers": "backupInfrastructure_managedServers.json",
        "/api/v1/backupInfrastructure/repositories/states": "repositories_states.json",
    }

    def __init__(self, directory: Path):
        self.dir = Path(directory)
        self.request_count = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass

    def close(self):
        pass

    def login(self):
        pass

    def get_all(self, path: str, params: dict[str, Any] | None = None) -> list[dict]:
        name = self.FILES.get(path)
        if not name:
            raise EndpointNotFound(f"{path} não existe no replay")
        f = self.dir / name
        if not f.is_file():
            raise EndpointNotFound(f"{path}: arquivo {name} ausente em {self.dir}")
        self.request_count += 1
        body = json.loads(f.read_text(encoding="utf-8"))
        if isinstance(body, dict) and "error" in body and "data" not in body:
            raise EndpointNotFound(f"{path}: {body.get('error')}")
        data = body["data"] if isinstance(body, dict) else body
        if isinstance(body, dict):
            total = (body.get("pagination") or {}).get("total")
            if isinstance(total, int) and total != len(data):
                raise IncompleteDataError(f"{path}: replay com {len(data)} itens mas total={total}")
        return data

    def get_unpaged(self, path: str) -> list[dict]:
        # No replay as task sessions por sessão ficam em taskSessions_<id>.json
        if path.startswith("/api/v1/sessions/") and path.endswith("/taskSessions"):
            sid = path.split("/")[4]
            f = self.dir / f"taskSessions_{sid}.json"
            if not f.is_file():
                raise EndpointNotFound(f"{path}: arquivo ausente no replay")
            self.request_count += 1
            body = json.loads(f.read_text(encoding="utf-8"))
            return body["data"] if isinstance(body, dict) else body
        raise EndpointNotFound(f"{path} não existe no replay")
