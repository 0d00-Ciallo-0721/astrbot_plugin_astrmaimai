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


@router.get("/chats/{chat_id}/trace-events")
async def list_chat_trace_events(chat_id: str, limit: int = 80, user: str = Depends(get_current_user)):
    return await _service().chat_trace_events(chat_id=chat_id, limit=limit)


@router.get("/context-economy")
async def get_context_economy(limit: int = 20, user: str = Depends(get_current_user)):
    return await _service().context_economy_overview_view(limit=limit)


@router.get("/context-economy/templates")
async def list_context_economy_templates(
    limit: int = 50,
    template_id: str | None = None,
    workload_family: str | None = None,
    sort_by: str = "rotate",
    sort_dir: str | None = None,
    user: str = Depends(get_current_user),
):
    return await _service().context_economy_templates_view(
        limit=limit,
        template_id=template_id,
        workload_family=workload_family,
        sort_by=sort_by,
        sort_dir=sort_dir,
    )


@router.get("/scheduler/status")
async def get_scheduler_status(user: str = Depends(get_current_user)):
    return await _service().scheduler_status_view()


@router.get("/scheduler/due-selection")
async def get_scheduler_due_selection(user: str = Depends(get_current_user)):
    return await _service().scheduler_due_selection_view()


@router.get("/scheduler/chats/{chat_id}")
async def get_scheduler_chat(chat_id: str, user: str = Depends(get_current_user)):
    return await _service().scheduler_chat_view(chat_id)
