import logging
import os
import aiosqlite
from contextlib import asynccontextmanager

from .paths import default_db_path, plugin_data_path

_logger = logging.getLogger(__name__)


def current_db_path() -> str:
    raw = default_db_path()
    resolved = os.path.realpath(raw)
    # Validate that the resolved path is within the plugin data directory
    data_root = os.path.realpath(str(plugin_data_path()))
    resolved_norm = os.path.normpath(resolved)
    root_norm = os.path.normpath(data_root)
    if not (resolved_norm == root_norm or resolved_norm.startswith(root_norm + os.sep)):
        _logger.warning(
            "ASTRMAI_DB_PATH %r resolves to %r — outside plugin data dir %r. "
            "Falling back to default.",
            raw,
            resolved,
            data_root,
        )
        return str(plugin_data_path("astrmai.db"))
    return resolved

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
