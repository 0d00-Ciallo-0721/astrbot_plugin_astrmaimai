from typing import Annotated

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel

from ..adapters.plugin_api import PluginApiAdapter
from ..access import get_current_user
from ..services.learningservice import LearningService

router = APIRouter()


class ReflectRunOncePayload(BaseModel):
    chat_id: str | None = None


def _service() -> LearningService:
    return LearningService(PluginApiAdapter())


@router.get("/status")
async def get_learning_status(user: str = Depends(get_current_user)):
    return await _service().learning_status()


@router.get("/expression-stats")
async def get_expression_stats(user: str = Depends(get_current_user)):
    return await _service().expression_stats()


@router.get("/cooldowns")
async def get_learning_cooldowns(user: str = Depends(get_current_user)):
    return await _service().expression_cooldowns()


@router.post("/reflect/run-once")
async def run_reflect_once(
    chat_id: str | None = None,
    payload: Annotated[ReflectRunOncePayload | None, Body()] = None,
    user: str = Depends(get_current_user),
):
    effective_chat_id = (payload.chat_id if payload else None) or chat_id
    if not effective_chat_id:
        raise HTTPException(status_code=422, detail="chat_id is required")
    result = await _service().run_reflect_once(str(effective_chat_id))
    if result.get("status") == "error":
        raise HTTPException(status_code=409, detail=result.get("message", "Reflector unavailable"))
    return result
