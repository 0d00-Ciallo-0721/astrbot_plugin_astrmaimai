from fastapi import APIRouter, Depends, Body
from typing import Dict, Any
from ..adapters.plugin_api import PluginApiAdapter
from ..access import get_current_user
from ..services.persona_ui_service import PersonaUiService

router = APIRouter()

@router.get("")
async def get_persona(user: str = Depends(get_current_user)):
    return await PersonaUiService(PluginApiAdapter()).get_persona()

@router.put("")
async def update_persona(data: Dict[str, Any] = Body(...), user: str = Depends(get_current_user)):
    return await PersonaUiService(PluginApiAdapter()).update_persona(data)
