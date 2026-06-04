from __future__ import annotations

import logging
import os
import secrets
import sys
from datetime import datetime, timedelta
from typing import Any, Optional

from fastapi import HTTPException, Header
from jose import JWTError, jwt

ALGORITHM = "HS256"

_SECRET_KEY: str | None = None


def get_secret_key() -> str:
    global _SECRET_KEY
    if _SECRET_KEY is not None:
        return _SECRET_KEY
    _SECRET_KEY = os.getenv("ASTRMAI_WEBUI_SECRET", "")
    if not _SECRET_KEY:
        _SECRET_KEY = secrets.token_hex(32)
        print(
            "[astrmai-webui] WARNING: ASTRMAI_WEBUI_SECRET not set — "
            "generated a random key for this session. "
            "All previously issued tokens are now invalid. "
            "Set ASTRMAI_WEBUI_SECRET in the environment for persistent keys.",
            file=sys.stderr,
        )
    return _SECRET_KEY


_password_adapter_factory: Any | None = None


def set_password_adapter_factory(factory: Any) -> None:
    """Inject a callable that returns an adapter with get_webui_password()."""
    global _password_adapter_factory
    _password_adapter_factory = factory


def _default_password_adapter():
    from .adapters.plugin_api import PluginApiAdapter, get_active_facade
    facade = get_active_facade()
    if facade is None:
        logger = logging.getLogger(__name__)
        logger.warning("auth: no active facade, password auth unavailable")
        return None
    return PluginApiAdapter(facade=facade)


def get_astrmai_password(adapter: Any | None = None) -> str:
    if adapter is not None:
        try:
            pwd = adapter.get_webui_password()
            if not pwd:
                raise ValueError("adapter returned empty password")
            return pwd
        except Exception as exc:
            print(f"Failed to read webui password from adapter: {exc}", file=sys.stderr)
    try:
        factory = _password_adapter_factory or _default_password_adapter
        pwd_adapter = factory()
        if pwd_adapter is None:
            raise RuntimeError("password adapter factory returned None (facade not bound)")
        pwd = pwd_adapter.get_webui_password()
        if not pwd:
            raise ValueError("factory adapter returned empty password — webui_password not configured")
        return pwd
    except Exception as exc:
        print(f"Failed to read webui password from config: {exc}", file=sys.stderr)
    # 所有认证路径均失败：拒绝启动
    msg = (
        "auth: ALL password sources failed — no password configured. "
        "Set ASTRMAI_WEBUI_SECRET or ensure the facade is bound."
    )
    logger = logging.getLogger(__name__)
    logger.error(msg)
    raise RuntimeError(msg)

def create_token(sub: str, expire_hours: int = 24) -> str:
    expire = datetime.utcnow() + timedelta(hours=expire_hours)
    to_encode = {"sub": sub, "exp": expire}
    encoded_jwt = jwt.encode(to_encode, get_secret_key(), algorithm=ALGORITHM)
    return encoded_jwt

def verify_token(token: str) -> dict:
    try:
        payload = jwt.decode(token, get_secret_key(), algorithms=[ALGORITHM])
        return payload
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

async def get_current_user(authorization: Optional[str] = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
    token = authorization.split(" ")[1]
    payload = verify_token(token)
    return payload["sub"]
