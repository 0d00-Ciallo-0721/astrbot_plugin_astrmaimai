from __future__ import annotations

import os

from tests.helpers.astrbot_stubs import install_astrbot_stubs


def main() -> None:
    from .scheduler_webui_fixture import (
        FIXTURE_CONFIG_PATH,
        FIXTURE_DB_PATH,
        FIXTURE_ROOT,
        build_scheduler_fixture_facade_sync,
        ensure_fixture_files,
    )

    ensure_fixture_files()
    install_astrbot_stubs(str(FIXTURE_ROOT))
    os.environ["ASTRMAI_DB_PATH"] = str(FIXTURE_DB_PATH)
    os.environ["ASTRMAI_CONFIG_PATH"] = str(FIXTURE_CONFIG_PATH)
    os.environ.setdefault("ASTRMAI_WEBUI_SECRET", "scheduler-fixture-secret")

    from astrmai.webui.backend.adapters.plugin_api import set_active_facade

    facade = build_scheduler_fixture_facade_sync()
    set_active_facade(facade)

    import uvicorn

    uvicorn.run(
        "astrmai.webui.backend.server:app",
        host="127.0.0.1",
        port=8765,
        reload=False,
        log_level="warning",
    )


if __name__ == "__main__":
    main()
