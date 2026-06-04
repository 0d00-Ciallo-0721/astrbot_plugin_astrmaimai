from fastapi import APIRouter, Depends

from ..adapters.plugin_api import PluginApiAdapter
from ..auth import get_current_user
from ..db import get_db
from ..services.chatruntimeservice import ChatRuntimeService

router = APIRouter()


def _service() -> ChatRuntimeService:
    return ChatRuntimeService(PluginApiAdapter(), get_db)


@router.get("/active")
async def list_active_chats(max_age_seconds: float = 1800, user: str = Depends(get_current_user)):
    return await _service().active_chats(max_age_seconds=max_age_seconds)


@router.get("/{chat_id}/activity")
async def get_chat_activity(chat_id: str, user: str = Depends(get_current_user)):
    return await _service().chat_activity(chat_id)


@router.get("/{chat_id}/runtime")
async def get_chat_runtime(chat_id: str, user: str = Depends(get_current_user)):
    return await _service().chat_runtime(chat_id)


@router.post("/{chat_id}/runtime/clear")
async def clear_chat_runtime(chat_id: str, user: str = Depends(get_current_user)):
    return await _service().clear_chat_runtime(chat_id)
