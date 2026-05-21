from __future__ import annotations

import os

from tests.helpers.astrbot_stubs import install_astrbot_stubs


def main() -> None:
    from .scheduler_webui_fixture import (
        FIXTURE_CONFIG_PATH,
        FIXTURE_DB_PATH,
        FIXTURE_PERSONA_CACHE_PATH,
        FIXTURE_ROOT,
        build_scheduler_fixture_facade_sync,
        ensure_fixture_files,
    )

    profile = os.getenv("ASTRMAI_SCHEDULER_FIXTURE_PROFILE", "admin_full")
    ensure_fixture_files(profile=profile)
    install_astrbot_stubs(str(FIXTURE_ROOT))
    os.environ["ASTRMAI_DB_PATH"] = str(FIXTURE_DB_PATH)
    os.environ["ASTRMAI_CONFIG_PATH"] = str(FIXTURE_CONFIG_PATH)
    os.environ["ASTRMAI_PERSONA_CACHE_PATH"] = str(FIXTURE_PERSONA_CACHE_PATH)
    os.environ.setdefault("ASTRMAI_WEBUI_SECRET", "scheduler-fixture-secret")

    from astrmai.webui.backend.adapters.plugin_api import set_active_facade

    facade = build_scheduler_fixture_facade_sync(profile=profile)
    set_active_facade(facade)

    import os as _os
    from fastapi import Depends, FastAPI
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.staticfiles import StaticFiles
    import uvicorn
    from astrmai.webui.backend.adapters.plugin_api import PluginApiAdapter
    from astrmai.webui.backend.auth import get_current_user
    from astrmai.webui.backend.routes import api_router
    from astrmai.webui.backend.services.persona_ui_service import PersonaUiService

    app = FastAPI(title="AstrMai Fixture WebUI")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(api_router, prefix="/api")

    @app.get("/api/persona/slices", tags=["fixture-dev"])
    async def fixture_persona_slices(user: str = Depends(get_current_user)):
        adapter = PluginApiAdapter(
            facade=facade,
            config_path=str(FIXTURE_CONFIG_PATH),
            persona_cache_path=str(FIXTURE_PERSONA_CACHE_PATH),
        )
        return await PersonaUiService(adapter).get_persona_slices()

    backend_base_dir = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
    frontend_dir = _os.path.join(_os.path.dirname(backend_base_dir), "astrmai", "webui", "frontend")
    app.mount("/", StaticFiles(directory=frontend_dir, html=True), name="static")

    uvicorn.run(
        app,
        host="127.0.0.1",
        port=8765,
        reload=False,
        log_level="warning",
    )


if __name__ == "__main__":
    main()
