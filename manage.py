#!/usr/bin/env python3
"""Comandos de operação do Monitor de Backups.

  python manage.py servir                      inicia o sistema web (+ sincronização automática)
  python manage.py criar-usuario NOME [--admin]
  python manage.py senha NOME                  troca a senha de um usuário
  python manage.py testar-conexao              testa login e leitura básica no Veeam
  python manage.py sync                        roda UMA sincronização e mostra o resultado
  python manage.py verificar [--todos]         sincroniza e imprime máquina a máquina, para
                                               conferir lado a lado com o console do Veeam
  python manage.py dump PASTA                  grava as respostas brutas da API (suporte/replay)
"""
from __future__ import annotations

import argparse
import getpass
import json
import logging
import sys
from collections import Counter
from datetime import timedelta
from pathlib import Path


def _settings():
    from app.config import ConfigError, load_settings
    try:
        return load_settings()
    except ConfigError as e:
        sys.exit(f"ERRO DE CONFIGURAÇÃO: {e}")


def cmd_servir(_):
    import uvicorn
    from app.web import create_app
    s = _settings()
    # workers=1 é obrigatório: o agendador vive dentro do processo.
    uvicorn.run(create_app(s), host=s.app_host, port=s.app_port, workers=1, log_level="info",
                proxy_headers=False, server_header=False)


def _ask_password() -> str:
    p1 = getpass.getpass("Senha: ")
    p2 = getpass.getpass("Repita a senha: ")
    if p1 != p2:
        sys.exit("As senhas não conferem.")
    return p1


def cmd_criar_usuario(a):
    from app import auth
    from app.db import Database
    db = Database(_settings().db_path)
    try:
        auth.create_user(db, a.nome, _ask_password(), "admin" if a.admin else "tecnico")
    except ValueError as e:
        sys.exit(f"Erro: {e}")
    print(f"Usuário {a.nome} criado ({'admin' if a.admin else 'tecnico'}).")


def cmd_senha(a):
    from app import auth
    from app.db import Database
    db = Database(_settings().db_path)
    try:
        auth.set_password(db, a.nome, _ask_password())
    except ValueError as e:
        sys.exit(f"Erro: {e}")
    print("Senha alterada.")


def cmd_testar(_):
    from app.sync import make_source
    from app.veeam_client import VeeamError
    s = _settings()
    src = make_source(s)
    try:
        src.login()
        print("Login OK.")
        for p in ("/api/v1/jobs/states", "/api/v1/backups"):
            print(f"{p}: {len(src.get_all(p))} itens")
        try:
            t = src.get_all("/api/v1/taskSessions", {"limit": 1})
            print(f"/api/v1/taskSessions: disponível ({len(t)} itens)")
        except VeeamError as e:
            print(f"/api/v1/taskSessions: {e} -> será usado o modo por sessão")
    except VeeamError as e:
        sys.exit(f"FALHOU: {e}")
    finally:
        src.close()


def cmd_sync(_):
    from app.db import Database
    from app.sync import run_sync
    s = _settings()
    r = run_sync(Database(s.db_path), s, trigger="linha de comando")
    print(json.dumps(r, indent=2, ensure_ascii=False))
    sys.exit(0 if r.get("ok") else 1)


def cmd_verificar(a):
    from app.db import Database
    from app.status import RESULT_PT
    from app.sync import run_sync
    from app.timeutil import fmt_local, from_db
    from app.views import build_rows
    s = _settings()
    db = Database(s.db_path)
    r = run_sync(db, s, trigger="verificar")
    if not r.get("ok"):
        sys.exit(f"Sincronização FALHOU: {r.get('error')}")
    rows = build_rows(db, s)
    print(f"\nRetrato OK. Lidos: {r['counts']}")
    for w in r.get("warnings", []):
        print(f"  AVISO: {w}")
    print("\n=== JOBS (compare com Home > Jobs no console) ===")
    for j in sorted(db.jobs(), key=lambda j: j["name"].lower()):
        state = "DESATIVADO" if j["disabled"] else ("RODANDO" if j["running"] else "parado")
        print(f"- {j['name']:<32} {state:<10} último: {RESULT_PT.get(j['last_result'], j['last_result'] or '—'):<13} "
              f"{fmt_local(from_db(j['last_end'])):<17} próximo: {fmt_local(from_db(j['next_run']))}  [{j['schedule']}]")
    print("\n=== MÁQUINAS ===")
    print(f"{'STATUS':<11} {'MÁQUINA':<34} {'JOB/BACKUP':<34} {'ÚLTIMO PONTO':<17} RESULTADO")
    for row in rows:
        if not a.todos and not row.status.is_problem:
            continue
        o = row.obj
        res = RESULT_PT.get(o.get("last_result"), "—") if o.get("last_result") else "—"
        print(f"{row.status.label:<11} {o['name'][:34]:<34} {o['job_name'][:34]:<34} "
              f"{fmt_local(from_db(o.get('last_rp_at'))):<17} {res}")
    c = Counter(r.status.label for r in rows)
    print("\nTotais: " + ", ".join(f"{k}={v}" for k, v in c.most_common()))
    if not a.todos:
        print("(só problemas; use --todos para listar tudo)")


def cmd_dump(a):
    from app.collector import _iso
    from app.sync import make_source
    from app.timeutil import now_utc
    from app.veeam_client import EndpointNotFound, ReplaySource
    s = _settings()
    out = Path(a.pasta)
    out.mkdir(parents=True, exist_ok=True)
    since = _iso(now_utc() - timedelta(days=s.history_days))
    src = make_source(s)
    src.login()
    params = {"/api/v1/sessions": {"createdAfterFilter": since}, "/api/v1/taskSessions": {"createdAfterFilter": since}}
    try:
        for path, fname in ReplaySource.FILES.items():
            try:
                data = src.get_all(path, params.get(path))
                body = {"data": data, "pagination": {"total": len(data), "count": len(data), "skip": 0}}
                print(f"{path}: {len(data)}")
            except EndpointNotFound as e:
                body = {"error": str(e), "status_code": 404}
                print(f"{path}: 404")
            (out / fname).write_text(json.dumps(body, ensure_ascii=False, indent=1), encoding="utf-8")
    finally:
        src.close()
    print(f"Dump gravado em {out.resolve()} (contém nomes de clientes/máquinas; não publique).")


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("servir").set_defaults(f=cmd_servir)
    p = sub.add_parser("criar-usuario"); p.add_argument("nome"); p.add_argument("--admin", action="store_true"); p.set_defaults(f=cmd_criar_usuario)
    p = sub.add_parser("senha"); p.add_argument("nome"); p.set_defaults(f=cmd_senha)
    sub.add_parser("testar-conexao").set_defaults(f=cmd_testar)
    sub.add_parser("sync").set_defaults(f=cmd_sync)
    p = sub.add_parser("verificar"); p.add_argument("--todos", action="store_true"); p.set_defaults(f=cmd_verificar)
    p = sub.add_parser("dump"); p.add_argument("pasta"); p.set_defaults(f=cmd_dump)
    a = ap.parse_args()
    a.f(a)


if __name__ == "__main__":
    main()
