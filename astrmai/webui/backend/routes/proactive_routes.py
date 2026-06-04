from fastapi import APIRouter, Depends, HTTPException

from ..adapters.plugin_api import PluginApiAdapter
from ..auth import get_current_user
from ..db import get_db
from ..services.chatruntimeservice import ChatRuntimeService

router = APIRouter()


def _service() -> ChatRuntimeService:
    return ChatRuntimeService(PluginApiAdapter(), get_db)


def _raise_if_error(result: dict):
    if result.get("status") == "error":
        raise HTTPException(status_code=409, detail=result.get("message", "Dependency unavailable"))
    return result


@router.get("/status")
async def get_proactive_status(user: str = Depends(get_current_user)):
    return await _service().proactive_status()


@router.get("/dream/status")
async def get_dream_status(user: str = Depends(get_current_user)):
    return await _service().dream_status()


@router.post("/dream/run-once")
async def run_dream_once(user: str = Depends(get_current_user)):
    return _raise_if_error(await _service().run_dream_once())


@router.get("/diary/status")
async def get_diary_status(user: str = Depends(get_current_user)):
    return await _service().diary_status()


@router.post("/diary/run-once")
async def run_diary_once(user: str = Depends(get_current_user)):
    return _raise_if_error(await _service().run_diary_once())


@router.get("/wakeup/status")
async def get_wakeup_status(user: str = Depends(get_current_user)):
    return await _service().wakeup_status()
