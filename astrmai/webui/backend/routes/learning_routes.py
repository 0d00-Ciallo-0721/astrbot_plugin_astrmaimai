from fastapi import APIRouter, Depends, HTTPException

from ..adapters.plugin_api import PluginApiAdapter
from ..auth import get_current_user
from ..db import get_db
from ..services.admin_ui_service import AdminUiService

router = APIRouter()


def _service() -> AdminUiService:
    return AdminUiService(PluginApiAdapter(), get_db)


@router.get("/status")
async def get_learning_status(user: str = Depends(get_current_user)):
    return await _service().learning_status()


@router.get("/expression-stats")
async def get_expression_stats(user: str = Depends(get_current_user)):
    return await _service().expression_stats()


@router.get("/cooldowns")
async def get_learning_cooldowns(user: str = Depends(get_current_user)):
    return await _service().expression_cooldowns()


@router.post("/reflect/run-once")
async def run_reflect_once(chat_id: str, user: str = Depends(get_current_user)):
    result = await _service().run_reflect_once(chat_id)
    if result.get("status") == "error":
        raise HTTPException(status_code=409, detail=result.get("message", "Reflector unavailable"))
    return result
