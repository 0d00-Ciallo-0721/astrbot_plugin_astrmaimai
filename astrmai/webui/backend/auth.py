import os
from datetime import datetime, timedelta
from typing import Optional
from jose import jwt, JWTError
from fastapi import HTTPException, Header

from .adapters.plugin_api import PluginApiAdapter

# Read secret from env, or fallback for dev
SECRET_KEY = os.getenv("ASTRMAI_WEBUI_SECRET", "super-secret-default-key")
ALGORITHM = "HS256"

def get_astrmai_password() -> str:
    try:
        return PluginApiAdapter().get_webui_password()
    except Exception as e:
        print(f"Failed to read webui password from config: {e}")
    return "astrmai_admin"

def create_token(sub: str, expire_hours: int = 24) -> str:
    expire = datetime.utcnow() + timedelta(hours=expire_hours)
    to_encode = {"sub": sub, "exp": expire}
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt

def verify_token(token: str) -> dict:
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

async def get_current_user(authorization: Optional[str] = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
    token = authorization.split(" ")[1]
    payload = verify_token(token)
    return payload["sub"]
