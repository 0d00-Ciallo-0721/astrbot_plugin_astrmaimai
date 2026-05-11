from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends

from ..adapters.plugin_api import PluginApiAdapter
from ..auth import get_current_user
from ..db import get_db
from ..schemas import BatchReviewRequest, ReviewActionRequest
from ..services.review_ui_service import ReviewUiService

router = APIRouter()

@router.get("/pending")
async def get_pending_reviews(user: str = Depends(get_current_user)):
    return await ReviewUiService(PluginApiAdapter(), get_db).list_pending()

@router.get("")
async def get_reviews(
    status: Optional[str] = None,
    group_id: Optional[str] = None,
    keyword: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
    user: str = Depends(get_current_user)
):
    service = ReviewUiService(PluginApiAdapter(), get_db)
    return await service.list_reviews(
        status=status,
        group_id=group_id,
        keyword=keyword,
        page=page,
        page_size=page_size,
    )

@router.post("/{id}/submit")
async def submit_review(id: int, req: ReviewActionRequest, user: str = Depends(get_current_user)):
    service = ReviewUiService(PluginApiAdapter(), get_db)
    return await service.submit_review(id, req.action, req.replacement, req.weight, req.reason)

@router.post("/batch")
async def batch_review(req: BatchReviewRequest, user: str = Depends(get_current_user)):
    service = ReviewUiService(PluginApiAdapter(), get_db)
    return await service.batch_review(req.ids, req.action)

@router.post("")
async def create_review(data: Dict[str, Any], user: str = Depends(get_current_user)):
    return await ReviewUiService(PluginApiAdapter(), get_db).create_review(data)

@router.put("/{id}")
async def update_review(id: int, data: Dict[str, Any], user: str = Depends(get_current_user)):
    return await ReviewUiService(PluginApiAdapter(), get_db).update_review_record(id, data)

@router.delete("/{id}")
async def delete_review(id: int, user: str = Depends(get_current_user)):
    return await ReviewUiService(PluginApiAdapter(), get_db).delete_review_record(id)
