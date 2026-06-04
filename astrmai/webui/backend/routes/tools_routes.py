from fastapi import APIRouter, Depends

from ..adapters.plugin_api import PluginApiAdapter
from ..auth import get_current_user
from ..db import get_db
from ..services.toolsservice import ToolsService

router = APIRouter()


def _service() -> ToolsService:
    return ToolsService(PluginApiAdapter(), get_db)


@router.get("/status")
async def get_tools_status(user: str = Depends(get_current_user)):
    return await _service().tools_status()


@router.get("/recent-calls")
async def list_recent_tool_calls(limit: int = 50, user: str = Depends(get_current_user)):
    return await _service().recent_tool_traces(limit=limit)


@router.get("/chats/{chat_id}/recent-calls")
async def list_chat_recent_tool_calls(chat_id: str, limit: int = 50, user: str = Depends(get_current_user)):
    return await _service().recent_tool_traces(chat_id=chat_id, limit=limit)


@router.get("/policy")
async def get_tools_policy(user: str = Depends(get_current_user)):
    return await _service().tools_policy()
