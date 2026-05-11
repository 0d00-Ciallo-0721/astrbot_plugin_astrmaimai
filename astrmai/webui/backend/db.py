import os
import aiosqlite
from contextlib import asynccontextmanager

from .paths import default_db_path


DB_PATH = default_db_path()

@asynccontextmanager
async def get_db():
    dir_name = os.path.dirname(os.path.abspath(DB_PATH))
    if dir_name:
        os.makedirs(dir_name, exist_ok=True)
    conn = await aiosqlite.connect(DB_PATH)
    # Enable dict rows so we get column names
    conn.row_factory = aiosqlite.Row
    try:
        yield conn
    finally:
        await conn.close()
