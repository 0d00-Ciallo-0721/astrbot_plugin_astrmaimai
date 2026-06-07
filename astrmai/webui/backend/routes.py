from __future__ import annotations

from fastapi import APIRouter

from .routes.chats_routes import router as chats_router
from .routes.cognition_routes import router as cognition_router
from .routes.config_routes import router as config_router
from .routes.dashboard_routes import router as dashboard_router
from .routes.heartflow_routes import router as heartflow_router
from .routes.learning_routes import router as learning_router
from .routes.memory_feedback_routes import router as memory_feedback_router
from .routes.memory_routes import router as memory_router
from .routes.persona_routes import router as persona_router
from .routes.proactive_routes import router as proactive_router
from .routes.review_routes import router as review_router
from .routes.runtime_routes import router as runtime_router
from .routes.tools_routes import router as tools_router
from .routes.users_routes import router as users_router


def build_api_router() -> APIRouter:
    router = APIRouter()
    router.include_router(config_router, prefix="/config", tags=["config"])
    router.include_router(review_router, prefix="/reviews", tags=["reviews"])
    router.include_router(memory_router, prefix="/memories", tags=["memories"])
    router.include_router(users_router, prefix="/users", tags=["users"])
    router.include_router(persona_router, prefix="/persona", tags=["persona"])
    router.include_router(dashboard_router, prefix="/dashboard", tags=["dashboard"])
    router.include_router(runtime_router, prefix="/runtime", tags=["runtime"])
    router.include_router(heartflow_router, prefix="/heartflow", tags=["heartflow"])
    router.include_router(cognition_router, prefix="/cognition", tags=["cognition"])
    router.include_router(tools_router, prefix="/tools", tags=["tools"])
    router.include_router(memory_feedback_router, prefix="/memory-feedback", tags=["memory-feedback"])
    router.include_router(proactive_router, prefix="/proactive", tags=["proactive"])
    router.include_router(learning_router, prefix="/learning", tags=["learning"])
    router.include_router(chats_router, prefix="/chats", tags=["chats"])
    return router


api_router = build_api_router()

__all__ = ["api_router", "build_api_router"]
