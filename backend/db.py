"""Acesso ao PostgreSQL. A conexão vem de DATABASE_URL (ambiente ou arquivo .env)."""
from __future__ import annotations

import os
from pathlib import Path

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

RAIZ = Path(__file__).resolve().parent.parent


def _url() -> str | None:
    if os.getenv("DATABASE_URL"):
        return os.environ["DATABASE_URL"]
    env = RAIZ / ".env"
    if env.exists():
        for linha in env.read_text(encoding="utf-8").splitlines():
            if linha.startswith("DATABASE_URL="):
                return linha.split("=", 1)[1].strip()
    return None


_pool: ConnectionPool | None = None


def pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        url = _url()
        if not url:
            raise RuntimeError("DATABASE_URL não definida (rode etl/criar_banco.py)")
        _pool = ConnectionPool(url, min_size=1, max_size=8, open=True,
                               kwargs={"row_factory": dict_row, "autocommit": True})
    return _pool


def consulta(sql: str, params: tuple | dict = ()) -> list[dict]:
    with pool().connection() as c:
        return c.execute(sql, params).fetchall()


def executa(sql: str, params: tuple | dict = ()) -> None:
    with pool().connection() as c:
        c.execute(sql, params)


def fechar() -> None:
    if _pool is not None:
        _pool.close()
