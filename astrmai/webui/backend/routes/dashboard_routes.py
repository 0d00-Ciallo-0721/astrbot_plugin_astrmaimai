from fastapi import APIRouter, Depends

from ..adapters.plugin_api import PluginApiAdapter
from ..access import get_current_user
from ..db import get_db
from ..services.dashboard_service import DashboardService

router = APIRouter()

@router.get("")
async def get_dashboard(user: str = Depends(get_current_user)):
    service = DashboardService(PluginApiAdapter(), get_db)
    return await service.get_snapshot()
