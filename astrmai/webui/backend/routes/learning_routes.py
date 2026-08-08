from typing import Annotated

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel

from ..adapters.plugin_api import PluginApiAdapter
from ..access import get_current_user
from ..services.learningservice import LearningService

router = APIRouter()


class ReflectRunOncePayload(BaseModel):
    chat_id: str | None = None


class ExpressionBackfillPayload(BaseModel):
    chat_id: str
    limit: int = 120
    max_age_seconds: float = 604800
    dry_run: bool = True


class LearningPipelineRetryPayload(BaseModel):
    pipeline: str
    chat_id: str


def _service() -> LearningService:
    return LearningService(PluginApiAdapter())


@router.get("/status")
async def get_learning_status(user: str = Depends(get_current_user)):
    return await _service().learning_status()


@router.get("/expression-stats")
async def get_expression_stats(user: str = Depends(get_current_user)):
    return await _service().expression_stats()


@router.get("/pipeline-diagnostics")
async def get_pipeline_diagnostics(
    pipeline: str = Query(default=""),
    chat_id: str = Query(default=""),
    status: str = Query(default=""),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    user: str = Depends(get_current_user),
):
    normalized_pipeline = str(pipeline or "").strip().lower()
    if normalized_pipeline and normalized_pipeline not in {"expression", "jargon"}:
        raise HTTPException(status_code=422, detail="pipeline must be expression or jargon")
    return await _service().pipeline_diagnostics(
        pipeline=normalized_pipeline,
        chat_id=str(chat_id or "").strip(),
        status=str(status or "").strip(),
        limit=limit,
        offset=offset,
    )


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


@router.post("/expression-backfill")
async def run_expression_backfill(
    payload: Annotated[ExpressionBackfillPayload, Body()],
    user: str = Depends(get_current_user),
):
    result = await _service().run_expression_backfill(
        payload.chat_id,
        limit=payload.limit,
        max_age_seconds=payload.max_age_seconds,
        dry_run=payload.dry_run,
    )
    if result.get("status") == "error":
        raise HTTPException(status_code=409, detail=result.get("message", "Expression backfill unavailable"))
    return result


@router.post("/pipeline/retry-now")
async def retry_pipeline_now(
    payload: Annotated[LearningPipelineRetryPayload, Body()],
    user: str = Depends(get_current_user),
):
    pipeline = str(payload.pipeline or "").strip().lower()
    chat_id = str(payload.chat_id or "").strip()
    if pipeline not in {"expression", "jargon"}:
        raise HTTPException(status_code=422, detail="pipeline must be expression or jargon")
    if not chat_id:
        raise HTTPException(status_code=422, detail="chat_id is required")
    result = await _service().retry_pipeline(pipeline, chat_id)
    if result.get("status") == "error":
        raise HTTPException(status_code=409, detail=result.get("message", "Learning pipeline unavailable"))
    return result


@router.post("/pipeline/runs/purge")
async def purge_pipeline_runs(user: str = Depends(get_current_user)):
    result = await _service().purge_pipeline_runs()
    if result.get("status") == "error":
        raise HTTPException(status_code=409, detail=result.get("message", "Learning pipeline unavailable"))
    return result
