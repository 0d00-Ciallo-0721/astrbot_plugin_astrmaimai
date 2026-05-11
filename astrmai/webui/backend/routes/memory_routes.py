from fastapi import APIRouter, Depends
from typing import Dict, Any
from ..auth import get_current_user
from ..db import get_db
from ..services.memory_ui_service import MemoryUiService

router = APIRouter()

# -----------------
# 1. MemoryEvent
# -----------------
@router.get("/events")
async def list_events(user: str = Depends(get_current_user)):
    return await MemoryUiService(get_db).list_events()

@router.post("/events")
async def create_event(data: Dict[str, Any], user: str = Depends(get_current_user)):
    return await MemoryUiService(get_db).create_event(data)

@router.delete("/events/{id}")
async def delete_event(id: int, user: str = Depends(get_current_user)):
    return await MemoryUiService(get_db).delete_event(id)

# -----------------
# 2. DailyReflection
# -----------------
@router.get("/reflections")
async def list_reflections(month: str, user: str = Depends(get_current_user)):
    return await MemoryUiService(get_db).list_reflections(month)

@router.post("/reflections")
async def create_reflection(data: Dict[str, Any], user: str = Depends(get_current_user)):
    return await MemoryUiService(get_db).create_reflection(data)

@router.put("/reflections/{date}")
async def update_reflection(date: str, data: Dict[str, Any], user: str = Depends(get_current_user)):
    return await MemoryUiService(get_db).update_reflection(date, data)

@router.delete("/reflections/{date}")
async def delete_reflection(date: str, user: str = Depends(get_current_user)):
    return await MemoryUiService(get_db).delete_reflection(date)

# -----------------
# 3. MemoryNode
# -----------------
@router.get("/nodes")
async def list_nodes(user: str = Depends(get_current_user)):
    return await MemoryUiService(get_db).list_nodes()

@router.post("/nodes")
async def create_node(data: Dict[str, Any], user: str = Depends(get_current_user)):
    return await MemoryUiService(get_db).create_node(data)
        
@router.put("/nodes/{id}")
async def update_node(id: int, data: Dict[str, Any], user: str = Depends(get_current_user)):
    return await MemoryUiService(get_db).update_node(id, data)

@router.delete("/nodes/{id}")
async def delete_node(id: int, user: str = Depends(get_current_user)):
    return await MemoryUiService(get_db).delete_node(id)

# -----------------
# 4. Jargon
# -----------------
@router.get("/jargon")
async def list_jargon(user: str = Depends(get_current_user)):
    return await MemoryUiService(get_db).list_jargon()

@router.post("/jargon")
async def create_jargon(data: Dict[str, Any], user: str = Depends(get_current_user)):
    return await MemoryUiService(get_db).create_jargon(data)

@router.put("/jargon/{id}")
async def update_jargon(id: int, data: Dict[str, Any], user: str = Depends(get_current_user)):
    return await MemoryUiService(get_db).update_jargon(id, data)

@router.delete("/jargon/{id}")
async def delete_jargon(id: int, user: str = Depends(get_current_user)):
    return await MemoryUiService(get_db).delete_jargon(id)
