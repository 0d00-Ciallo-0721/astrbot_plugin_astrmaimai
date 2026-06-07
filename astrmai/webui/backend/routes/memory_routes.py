from fastapi import APIRouter, Depends
from typing import Dict, Any
from ..adapters.plugin_api import PluginApiAdapter
from ..access import get_current_user
from ..db import get_db
from ..services.memory_ui_service import MemoryUiService

router = APIRouter()


def _service() -> MemoryUiService:
    return MemoryUiService(get_db, PluginApiAdapter())


@router.get("/canonical")
async def list_canonical(
    session_id: str = "",
    persona_id: str = "",
    kind: str = "",
    status: str = "",
    limit: int = 100,
    offset: int = 0,
    user: str = Depends(get_current_user),
):
    return await _service().list_canonical(
        session_id=session_id,
        persona_id=persona_id,
        kind=kind,
        status=status,
        limit=limit,
        offset=offset,
    )


@router.get("/canonical/{memory_id}")
async def get_canonical(memory_id: str, user: str = Depends(get_current_user)):
    return await _service().get_canonical(memory_id)


@router.delete("/canonical/{memory_id}")
async def delete_canonical(memory_id: str, user: str = Depends(get_current_user)):
    return await _service().delete_canonical(memory_id)


@router.post("/canonical/{memory_id}/restore")
async def restore_canonical(memory_id: str, user: str = Depends(get_current_user)):
    return await _service().restore_canonical(memory_id)


@router.post("/canonical/{memory_id}/stale")
async def mark_canonical_stale(memory_id: str, user: str = Depends(get_current_user)):
    return await _service().mark_canonical_stale(memory_id)


@router.post("/canonical/{memory_id}/merge")
async def merge_canonical(memory_id: str, data: Dict[str, Any], user: str = Depends(get_current_user)):
    return await _service().merge_canonical(memory_id, target_id=str((data or {}).get("target_id") or ""))


@router.get("/diagnostics/migrations")
async def migration_report(user: str = Depends(get_current_user)):
    return await _service().migration_report()


@router.get("/diagnostics/index")
async def index_status(user: str = Depends(get_current_user)):
    return await _service().index_status()


@router.get("/observability/runtime")
async def observability_runtime(user: str = Depends(get_current_user)):
    return await _service().observability_runtime()


@router.get("/observability/chats/{chat_id}")
async def observability_chat(chat_id: str, user: str = Depends(get_current_user)):
    return await _service().observability_chat(chat_id)


@router.get("/observability/events")
async def observability_events(
    chat_id: str = "",
    component: str = "",
    level: str = "",
    limit: int = 50,
    user: str = Depends(get_current_user),
):
    return await _service().observability_events(
        chat_id=chat_id,
        component=component,
        level=level,
        limit=limit,
    )


@router.get("/observability/errors")
async def observability_errors(chat_id: str = "", limit: int = 50, user: str = Depends(get_current_user)):
    return await _service().observability_errors(chat_id=chat_id, limit=limit)


@router.post("/diagnostics/index/repair")
async def repair_index(user: str = Depends(get_current_user)):
    return await _service().repair_index()


@router.post("/index/rebuild")
async def rebuild_index(data: Dict[str, Any] | None = None, user: str = Depends(get_current_user)):
    return await _service().rebuild_index(session_id=str((data or {}).get("session_id") or ""))


@router.post("/maintenance/run")
async def run_maintenance(data: Dict[str, Any] | None = None, user: str = Depends(get_current_user)):
    return await _service().run_maintenance(policy=data or {})


@router.post("/migration/dry-run")
async def migration_dry_run(data: Dict[str, Any] | None = None, user: str = Depends(get_current_user)):
    return await _service().migration_dry_run(sources=list((data or {}).get("import_sources") or []))


@router.post("/migration/execute")
async def migration_execute(data: Dict[str, Any] | None = None, user: str = Depends(get_current_user)):
    return await _service().migration_execute(sources=list((data or {}).get("import_sources") or []))


@router.get("/migration/verify")
async def migration_verify(user: str = Depends(get_current_user)):
    return await _service().migration_verify()


@router.post("/migration/repair")
async def migration_repair(data: Dict[str, Any] | None = None, user: str = Depends(get_current_user)):
    return await _service().migration_repair(report=(data or {}).get("report"))

# -----------------
# 1. MemoryEvent
# -----------------
@router.get("/events")
async def list_events(user: str = Depends(get_current_user)):
    return await _service().list_events()

@router.post("/events")
async def create_event(data: Dict[str, Any], user: str = Depends(get_current_user)):
    return await _service().create_event(data)

@router.delete("/events/{id}")
async def delete_event(id: int, user: str = Depends(get_current_user)):
    return await _service().delete_event(id)

# -----------------
# 2. DailyReflection
# -----------------
@router.get("/reflections")
async def list_reflections(month: str, user: str = Depends(get_current_user)):
    return await _service().list_reflections(month)

@router.post("/reflections")
async def create_reflection(data: Dict[str, Any], user: str = Depends(get_current_user)):
    return await _service().create_reflection(data)

@router.put("/reflections/{date}")
async def update_reflection(date: str, data: Dict[str, Any], user: str = Depends(get_current_user)):
    return await _service().update_reflection(date, data)

@router.delete("/reflections/{date}")
async def delete_reflection(date: str, user: str = Depends(get_current_user)):
    return await _service().delete_reflection(date)

# -----------------
# 3. MemoryNode
# -----------------
@router.get("/nodes")
async def list_nodes(user: str = Depends(get_current_user)):
    return await _service().list_nodes()

@router.post("/nodes")
async def create_node(data: Dict[str, Any], user: str = Depends(get_current_user)):
    return await _service().create_node(data)
        
@router.put("/nodes/{id}")
async def update_node(id: int, data: Dict[str, Any], user: str = Depends(get_current_user)):
    return await _service().update_node(id, data)

@router.delete("/nodes/{id}")
async def delete_node(id: int, user: str = Depends(get_current_user)):
    return await _service().delete_node(id)

# -----------------
# 4. Jargon
# -----------------
@router.get("/jargon")
async def list_jargon(
    status: str = "",
    group_id: str = "",
    query: str = "",
    user: str = Depends(get_current_user),
):
    return await _service().list_jargon(status=status, group_id=group_id, query=query)

@router.post("/jargon")
async def create_jargon(data: Dict[str, Any], user: str = Depends(get_current_user)):
    return await _service().create_jargon(data)

@router.post("/jargon/{id}/approve")
async def approve_jargon(id: str, user: str = Depends(get_current_user)):
    return await _service().approve_jargon(id)

@router.post("/jargon/{id}/reject")
async def reject_jargon(id: str, user: str = Depends(get_current_user)):
    return await _service().reject_jargon(id)

@router.put("/jargon/{id}")
async def update_jargon(id: str, data: Dict[str, Any], user: str = Depends(get_current_user)):
    return await _service().update_jargon(id, data)

@router.delete("/jargon/{id}")
async def delete_jargon(id: str, user: str = Depends(get_current_user)):
    return await _service().delete_jargon(id)
