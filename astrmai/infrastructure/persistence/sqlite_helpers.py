from __future__ import annotations

import sqlite3
from contextlib import asynccontextmanager, contextmanager
from typing import AsyncIterator, Iterator

import aiosqlite


SQLITE_BUSY_TIMEOUT_SEC = 30.0
SQLITE_BUSY_TIMEOUT_MS = int(SQLITE_BUSY_TIMEOUT_SEC * 1000)


@contextmanager
def connect_sqlite(db_path) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(db_path, timeout=SQLITE_BUSY_TIMEOUT_SEC)
    try:
        conn.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
        yield conn
    finally:
        conn.close()


@asynccontextmanager
async def connect_aiosqlite(db_path) -> AsyncIterator[aiosqlite.Connection]:
    async with aiosqlite.connect(db_path, timeout=SQLITE_BUSY_TIMEOUT_SEC) as db:
        await db.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
        yield db


def sqlite_connect_args() -> dict[str, float]:
    return {"timeout": SQLITE_BUSY_TIMEOUT_SEC}
