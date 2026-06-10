from fastapi import APIRouter, Depends

from ..adapters.plugin_api import PluginApiAdapter
from ..access import get_current_user
from ..services.cognitionservice import CognitionService

router = APIRouter()


def _service() -> CognitionService:
    return CognitionService(PluginApiAdapter())


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


@router.get("/chats/{chat_id}/unified-timeline")
async def list_chat_unified_timeline(
    chat_id: str,
    limit: int = 80,
    level: str = "",
    include: str = "",
    user: str = Depends(get_current_user),
):
    include_values = [item.strip() for item in str(include or "").split(",") if item.strip()]
    return await _service().cognition_unified_timeline(
        chat_id=chat_id,
        limit=limit,
        level=level,
        include=include_values,
    )


@router.get("/observability/overview")
async def get_observability_overview(user: str = Depends(get_current_user)):
    return await _service().observability_overview()


@router.get("/observability/timeline")
async def get_observability_timeline(
    chat_id: str = "",
    domains: str = "",
    levels: str = "",
    kinds: str = "",
    limit: int = 80,
    user: str = Depends(get_current_user),
):
    return await _service().observability_timeline(
        chat_id=str(chat_id or "") or None,
        domains=[item.strip() for item in str(domains or "").split(",") if item.strip()],
        levels=[item.strip() for item in str(levels or "").split(",") if item.strip()],
        kinds=[item.strip() for item in str(kinds or "").split(",") if item.strip()],
        limit=limit,
    )


@router.get("/observability/chats/{chat_id}")
async def get_observability_chat(chat_id: str, user: str = Depends(get_current_user)):
    return await _service().observability_chat(chat_id)


@router.get("/observability/errors")
async def get_observability_errors(chat_id: str = "", limit: int = 50, user: str = Depends(get_current_user)):
    return await _service().observability_errors(chat_id=str(chat_id or "") or None, limit=limit)


@router.get("/observability/search")
async def get_observability_search(
    q: str = "",
    chat_id: str = "",
    domains: str = "",
    kinds: str = "",
    levels: str = "",
    tags: str = "",
    limit: int = 80,
    user: str = Depends(get_current_user),
):
    return await _service().observability_search(
        q=q,
        chat_id=chat_id,
        domains=[item.strip() for item in str(domains or "").split(",") if item.strip()],
        kinds=[item.strip() for item in str(kinds or "").split(",") if item.strip()],
        levels=[item.strip() for item in str(levels or "").split(",") if item.strip()],
        tags=[item.strip() for item in str(tags or "").split(",") if item.strip()],
        limit=limit,
    )


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
