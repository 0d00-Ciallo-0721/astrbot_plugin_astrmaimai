from fastapi import APIRouter, Depends

from ..adapters.plugin_api import PluginApiAdapter
from ..auth import get_current_user
from ..db import get_db
from ..services.admin_ui_service import AdminUiService

router = APIRouter()


def _service() -> AdminUiService:
    return AdminUiService(PluginApiAdapter(), get_db)


@router.get("/recent-decisions")
async def list_recent_decisions(limit: int = 50, user: str = Depends(get_current_user)):
    return await _service().recent_decisions(limit=limit)


@router.get("/chats/{chat_id}/recent-decisions")
async def list_chat_recent_decisions(chat_id: str, limit: int = 50, user: str = Depends(get_current_user)):
    return await _service().recent_decisions(chat_id=chat_id, limit=limit)


@router.get("/recent-turns")
async def list_recent_turns(limit: int = 50, user: str = Depends(get_current_user)):
    return await _service().recent_turn_traces(limit=limit)


@router.get("/chats/{chat_id}/turns")
async def list_chat_recent_turns(chat_id: str, limit: int = 50, user: str = Depends(get_current_user)):
    return await _service().recent_turn_traces(chat_id=chat_id, limit=limit)
