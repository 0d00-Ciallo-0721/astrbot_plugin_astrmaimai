import os
import aiosqlite
from contextlib import asynccontextmanager

from .paths import default_db_path


def current_db_path() -> str:
    return default_db_path()

@asynccontextmanager
async def get_db():
    db_path = current_db_path()
    dir_name = os.path.dirname(os.path.abspath(db_path))
    if dir_name:
        os.makedirs(dir_name, exist_ok=True)
    conn = await aiosqlite.connect(db_path)
    # Enable dict rows so we get column names
    conn.row_factory = aiosqlite.Row
    try:
        yield conn
    finally:
        await conn.close()
