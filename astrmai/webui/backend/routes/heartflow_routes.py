from fastapi import APIRouter, Depends

from ..adapters.plugin_api import PluginApiAdapter
from ..access import get_current_user
from ..db import get_db
from ..services.heartflowservice import HeartflowService

router = APIRouter()


def _service() -> HeartflowService:
    return HeartflowService(PluginApiAdapter(), get_db)


@router.get("/status")
async def get_heartflow_status(user: str = Depends(get_current_user)):
    return await _service().heartflow_status()


@router.get("/chats")
async def list_heartflow_chats(user: str = Depends(get_current_user)):
    return await _service().heartflow_chats()


@router.get("/impulses")
async def list_heartflow_impulses(limit: int = 50, user: str = Depends(get_current_user)):
    return await _service().heartflow_impulses(limit=limit)


@router.get("/timeline")
async def list_heartflow_timeline(limit: int = 80, user: str = Depends(get_current_user)):
    return await _service().heartflow_timeline(limit=limit)


@router.get("/topic-digests")
async def list_heartflow_topic_digests(limit: int = 50, user: str = Depends(get_current_user)):
    return await _service().heartflow_topic_digests(limit=limit)


@router.get("/chats/{chat_id}")
async def get_heartflow_chat(chat_id: str, user: str = Depends(get_current_user)):
    return await _service().heartflow_chat(chat_id)


@router.get("/chats/{chat_id}/impulses")
async def list_heartflow_chat_impulses(chat_id: str, limit: int = 20, user: str = Depends(get_current_user)):
    return await _service().heartflow_impulses(chat_id=chat_id, limit=limit)


@router.get("/chats/{chat_id}/timeline")
async def list_heartflow_chat_timeline(chat_id: str, limit: int = 50, user: str = Depends(get_current_user)):
    return await _service().heartflow_timeline(chat_id=chat_id, limit=limit)


@router.get("/chats/{chat_id}/hidden-context")
async def get_heartflow_hidden_context(chat_id: str, user: str = Depends(get_current_user)):
    return await _service().heartflow_hidden_context(chat_id)


@router.post("/chats/{chat_id}/cooldowns/clear")
async def clear_heartflow_cooldowns(chat_id: str, user: str = Depends(get_current_user)):
    return await _service().clear_heartflow_cooldowns(chat_id)
