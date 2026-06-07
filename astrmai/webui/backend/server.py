import logging
import os
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .routes import api_router

logger = logging.getLogger(__name__)

app = FastAPI(title="AstrMai WebUI")


def _cors_origins() -> list[str]:
    raw = os.getenv(
        "ASTRMAI_CORS_ORIGINS",
        "http://localhost:8765,http://127.0.0.1:8765,http://localhost:8787,http://127.0.0.1:8787",
    )
    return [origin.strip() for origin in raw.split(",") if origin.strip()]

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins(),
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["Content-Type", "Authorization"],
)

# API Routers
app.include_router(api_router, prefix="/api")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("backend.server:app", host="0.0.0.0", port=8765, reload=True)
