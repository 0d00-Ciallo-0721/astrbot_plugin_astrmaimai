import os
from fastapi import APIRouter, Depends

from ..adapters.plugin_api import PluginApiAdapter
from ..auth import get_current_user
from ..db import get_db
from ..paths import default_db_path
from ..services.dashboard_service import DashboardService

router = APIRouter()

DB_PATH = default_db_path()

@router.get("")
async def get_dashboard(user: str = Depends(get_current_user)):
    service = DashboardService(PluginApiAdapter(), get_db)
    return await service.get_snapshot()
