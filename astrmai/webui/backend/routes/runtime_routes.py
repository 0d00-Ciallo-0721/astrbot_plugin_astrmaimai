from fastapi import APIRouter, Depends

from ..adapters.plugin_api import PluginApiAdapter
from ..access import get_current_user
from ..db import get_db
from ..services.observabilityservice import ObservabilityService

router = APIRouter()


def _service() -> ObservabilityService:
    return ObservabilityService(PluginApiAdapter(), get_db)


@router.get("/status")
async def get_runtime_status(user: str = Depends(get_current_user)):
    return await _service().runtime_status()


@router.get("/capabilities")
async def get_runtime_capabilities(user: str = Depends(get_current_user)):
    return await _service().runtime_capabilities()


@router.get("/models")
async def get_runtime_models(user: str = Depends(get_current_user)):
    return await _service().runtime_models()


@router.get("/health")
async def get_runtime_health(user: str = Depends(get_current_user)):
    return await _service().runtime_health()
