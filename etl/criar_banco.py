"""
Cria o banco `uberbus` e o usuário `uberbus` no PostgreSQL local. Rodar UMA vez por máquina.

Pede a senha do superusuário (normalmente `postgres`, definida na instalação do PostgreSQL),
gera uma senha aleatória para o usuário do projeto e grava a conexão em `.env`
(que não vai para o git).

Uso:  python etl/criar_banco.py [--host localhost] [--porta 5432] [--admin postgres]
"""
from __future__ import annotations

import argparse
import getpass
import secrets
from pathlib import Path

import psycopg
from psycopg import sql

RAIZ = Path(__file__).resolve().parent.parent
ENV = RAIZ / ".env"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="localhost")
    ap.add_argument("--porta", default="5432")
    ap.add_argument("--admin", default="postgres")
    ap.add_argument("--banco", default="uberbus")
    a = ap.parse_args()

    senha_admin = getpass.getpass(f"Senha do usuário '{a.admin}' do PostgreSQL: ")
    senha = secrets.token_urlsafe(18)
    with psycopg.connect(host=a.host, port=a.porta, user=a.admin, password=senha_admin,
                         dbname="postgres", autocommit=True) as c:
        existe = c.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (a.banco,)).fetchone()
        cmd = "ALTER ROLE {} LOGIN PASSWORD {}" if existe else "CREATE ROLE {} LOGIN PASSWORD {}"
        c.execute(sql.SQL(cmd).format(sql.Identifier(a.banco), sql.Literal(senha)))
        if not c.execute("SELECT 1 FROM pg_database WHERE datname = %s", (a.banco,)).fetchone():
            c.execute(sql.SQL("CREATE DATABASE {} OWNER {} ENCODING 'UTF8' TEMPLATE template0").format(
                sql.Identifier(a.banco), sql.Identifier(a.banco)))
    # extensões precisam de superusuário; ficam criadas dentro do banco do projeto
    with psycopg.connect(host=a.host, port=a.porta, user=a.admin, password=senha_admin,
                         dbname=a.banco, autocommit=True) as c:
        c.execute("CREATE EXTENSION IF NOT EXISTS unaccent")
        c.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
        c.execute(sql.SQL("GRANT ALL ON SCHEMA public TO {}").format(sql.Identifier(a.banco)))

    url = f"postgresql://{a.banco}:{senha}@{a.host}:{a.porta}/{a.banco}"
    linhas = [l for l in (ENV.read_text(encoding="utf-8").splitlines() if ENV.exists() else [])
              if not l.startswith("DATABASE_URL=")]
    ENV.write_text("\n".join(linhas + [f"DATABASE_URL={url}"]) + "\n", encoding="utf-8")
    print(f"Banco '{a.banco}' pronto. Conexão gravada em {ENV}")


if __name__ == "__main__":
    main()
