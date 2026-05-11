from fastapi import APIRouter, HTTPException, Depends
from typing import Dict, Any
from ..auth import get_current_user
from ..db import get_db
from ..services.user_ui_service import UserUiService

router = APIRouter()

@router.get("")
async def list_users(user: str = Depends(get_current_user)):
    return await UserUiService(get_db).list_users()

@router.get("/{user_id}")
async def get_user(user_id: str, user: str = Depends(get_current_user)):
    service = UserUiService(get_db)
    record = await service.get_user(user_id)
    if not record:
        raise HTTPException(status_code=404, detail="User not found")
    return record

@router.patch("/{user_id}")
async def update_user(user_id: str, data: Dict[str, Any], user: str = Depends(get_current_user)):
    return await UserUiService(get_db).update_user(user_id, data)

@router.delete("/{user_id}")
async def delete_user(user_id: str, user: str = Depends(get_current_user)):
    return await UserUiService(get_db).delete_user(user_id)

@router.post("/{user_id}/slices")
async def add_slice(user_id: str, data: Dict[str, Any], user: str = Depends(get_current_user)):
    service = UserUiService(get_db)
    try:
        result = await service.add_slice(user_id, data.get("type"), data.get("content"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not result:
        raise HTTPException(status_code=404, detail="User not found")
    return result

@router.put("/{user_id}/slices/{index}")
async def update_slice(user_id: str, index: int, data: Dict[str, Any], user: str = Depends(get_current_user)):
    service = UserUiService(get_db)
    try:
        result = await service.update_slice(user_id, index, data.get("type"), data.get("content"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except IndexError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not result:
        raise HTTPException(status_code=404, detail="User not found")
    return result

@router.delete("/{user_id}/slices/{index}")
async def delete_slice(user_id: str, index: int, type: str, user: str = Depends(get_current_user)):
    service = UserUiService(get_db)
    try:
        result = await service.delete_slice(user_id, index, type)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not result:
        raise HTTPException(status_code=404, detail="User not found")
    return result
