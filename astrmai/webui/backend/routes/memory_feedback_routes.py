from fastapi import APIRouter, Depends

from ..adapters.plugin_api import PluginApiAdapter
from ..auth import get_current_user
from ..db import get_db
from ..services.admin_ui_service import AdminUiService

router = APIRouter()


def _service() -> AdminUiService:
    return AdminUiService(PluginApiAdapter(), get_db)


@router.get("")
async def list_memory_feedback(
    chat_id: str | None = None,
    source: str | None = None,
    limit: int = 50,
    user: str = Depends(get_current_user),
):
    return await _service().list_memory_feedback(chat_id=chat_id, source=source, limit=limit)


@router.get("/sources")
async def list_memory_feedback_sources(user: str = Depends(get_current_user)):
    return await _service().memory_feedback_sources()


@router.post("/{feedback_id}/disable")
async def disable_memory_feedback(feedback_id: str, user: str = Depends(get_current_user)):
    return await _service().disable_memory_feedback(feedback_id)


@router.delete("/{feedback_id}")
async def delete_memory_feedback(feedback_id: str, user: str = Depends(get_current_user)):
    return await _service().disable_memory_feedback(feedback_id)
