"""Postgres 접속 헬퍼 (psycopg3)."""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

import psycopg

from src.config import settings


@contextmanager
def connect() -> Iterator[psycopg.Connection]:
    """트랜잭션 컨텍스트. with 블록 정상 종료 시 commit, 예외 시 rollback."""
    conn = psycopg.connect(settings.postgres_dsn)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
