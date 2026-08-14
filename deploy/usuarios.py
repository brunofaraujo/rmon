#!/usr/bin/env python3
"""Gerencia usuarios do painel RMonitor (Postgres).

Exemplos (na VM):
  sudo /opt/rmon/.venv/bin/python /opt/rmon/deploy/usuarios.py list
  sudo /opt/rmon/.venv/bin/python /opt/rmon/deploy/usuarios.py add joao --role admin
  sudo /opt/rmon/.venv/bin/python /opt/rmon/deploy/usuarios.py passwd joao
  sudo /opt/rmon/.venv/bin/python /opt/rmon/deploy/usuarios.py role joao viewer
  sudo /opt/rmon/.venv/bin/python /opt/rmon/deploy/usuarios.py disable joao
"""
import argparse
import getpass
import os
import sys

sys.path.insert(0, "/opt/rmon")
from app.config import _load_dotenv  # noqa: E402

_load_dotenv("/opt/rmon/.env")
from app import db  # noqa: E402
from app.security import hash_password  # noqa: E402

db.init(os.environ["RMON_DB_DSN"])


def _ask_pw() -> str:
    p1 = getpass.getpass("Senha: ")
    p2 = getpass.getpass("Confirme: ")
    if p1 != p2 or not p1:
        sys.exit("Senhas nao conferem ou vazias.")
    return p1


def main() -> None:
    ap = argparse.ArgumentParser(description="Usuarios do RMonitor")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    a = sub.add_parser("add"); a.add_argument("username"); a.add_argument("--role", default="viewer", choices=["admin", "viewer"])
    p = sub.add_parser("passwd"); p.add_argument("username")
    r = sub.add_parser("role"); r.add_argument("username"); r.add_argument("role", choices=["admin", "viewer"])
    d = sub.add_parser("disable"); d.add_argument("username")
    e = sub.add_parser("enable"); e.add_argument("username")
    args = ap.parse_args()

    if args.cmd == "list":
        for u in db.list_users():
            print(f"{u['username']:20} {u['role']:7} {'ativo' if u['active'] else 'INATIVO':8} ult.login={u['last_login']}")
    elif args.cmd == "add":
        db.create_user(args.username, hash_password(_ask_pw()), args.role)
        print(f"usuario '{args.username}' ({args.role}) criado/atualizado.")
    elif args.cmd == "passwd":
        if not db.get_user(args.username):
            sys.exit("usuario nao existe.")
        db.set_password(args.username, hash_password(_ask_pw()))
        print("senha atualizada.")
    elif args.cmd == "role":
        db.set_role(args.username, args.role); print("papel atualizado.")
    elif args.cmd == "disable":
        db.set_active(args.username, False); print("usuario desativado.")
    elif args.cmd == "enable":
        db.set_active(args.username, True); print("usuario ativado.")


if __name__ == "__main__":
    main()
