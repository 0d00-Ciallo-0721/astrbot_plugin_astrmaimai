from fastapi import APIRouter, HTTPException, Depends
from typing import Dict, Any

from ..adapters.plugin_api import PluginApiAdapter
from ..access import get_current_user
from ..services.settings_ui_service import SettingsUiService

router = APIRouter()

@router.get("")
async def get_config(user: str = Depends(get_current_user)):
    return await SettingsUiService(PluginApiAdapter()).get_config()

@router.get("/effective")
async def get_effective_config(user: str = Depends(get_current_user)):
    return await SettingsUiService(PluginApiAdapter()).get_effective_config()

@router.get("/meta")
async def get_config_meta(user: str = Depends(get_current_user)):
    return await SettingsUiService(PluginApiAdapter()).get_meta()

@router.get("/schema")
async def get_schema(user: str = Depends(get_current_user)):
    schema = await SettingsUiService(PluginApiAdapter()).get_schema()
    if not schema:
        # Fallback empty schema format if not found locally for some reason
        schema = {}
    return schema

@router.post("")
async def create_section(data: Dict[str, Any], user: str = Depends(get_current_user)):
    """Create a new section in config if not exists"""
    section = data.get("section")
    if not section:
        raise HTTPException(status_code=400, detail="Missing section name")
    
    service = SettingsUiService(PluginApiAdapter())
    config = await service.get_config()
    if section in config:
        raise HTTPException(status_code=400, detail="Section already exists")
    
    config[section] = data.get("value", {})
    await service.plugin_api.write_config(config)
    return {"status": "ok", section: config[section]}

@router.patch("/{section}")
async def update_section(section: str, data: Dict[str, Any], user: str = Depends(get_current_user)):
    result = await SettingsUiService(PluginApiAdapter()).update_section(section, data)
    if result.get("status") == "error":
        raise HTTPException(status_code=400, detail=result.get("errors", []))
    return result

@router.put("")
async def replace_config(data: Dict[str, Any], user: str = Depends(get_current_user)):
    result = await SettingsUiService(PluginApiAdapter()).replace_config(data)
    if result.get("status") == "error":
        raise HTTPException(status_code=400, detail=result.get("errors", []))
    return result

@router.post("/reset/{section}")
async def reset_section(section: str, user: str = Depends(get_current_user)):
    service = SettingsUiService(PluginApiAdapter())
    schema = await service.get_schema()
    if section not in schema:
        raise HTTPException(status_code=404, detail="Section not found in schema")
    return await service.reset_section(section)

@router.post("/reset")
async def reset_all(user: str = Depends(get_current_user)):
    return await SettingsUiService(PluginApiAdapter()).reset_all()

@router.post("/apply")
async def apply_config(user: str = Depends(get_current_user)):
    result = await SettingsUiService(PluginApiAdapter()).apply_config()
    if result.get("status") == "error":
        raise HTTPException(status_code=400, detail=result.get("errors", []))
    return result

@router.get("/apply-status")
async def get_apply_status(user: str = Depends(get_current_user)):
    return await SettingsUiService(PluginApiAdapter()).get_apply_status()
