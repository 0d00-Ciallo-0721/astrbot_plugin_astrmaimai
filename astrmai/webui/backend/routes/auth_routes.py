import time
from collections import defaultdict

from fastapi import APIRouter, HTTPException, Depends, Header, Request
from pydantic import BaseModel
from typing import Optional
from ..auth import verify_token, create_token, get_astrmai_password, get_current_user, check_webui_password

router = APIRouter()

# ── Rate limiter for login ──────────────────────────────────────────
_LOGIN_ATTEMPTS: dict[str, list[float]] = defaultdict(list)
_LOGIN_WINDOW_SEC = 300   # 5 minutes
_LOGIN_MAX_ATTEMPTS = 10


def _check_login_rate(client_ip: str) -> None:
    """Raise 429 if client has exceeded login attempt limit."""
    now = time.time()
    cutoff = now - _LOGIN_WINDOW_SEC
    attempts = [t for t in _LOGIN_ATTEMPTS.get(client_ip, []) if t > cutoff]
    if attempts:
        _LOGIN_ATTEMPTS[client_ip] = attempts
    else:
        _LOGIN_ATTEMPTS.pop(client_ip, None)
    if len(attempts) >= _LOGIN_MAX_ATTEMPTS:
        retry_after = int(attempts[0] + _LOGIN_WINDOW_SEC - now) + 1
        raise HTTPException(
            status_code=429,
            detail="Too many login attempts. Please try again later.",
            headers={"Retry-After": str(retry_after)},
        )


def _record_login_attempt(client_ip: str) -> None:
    _LOGIN_ATTEMPTS[client_ip].append(time.time())


def _clear_login_attempts(client_ip: str) -> None:
    _LOGIN_ATTEMPTS.pop(client_ip, None)


class LoginRequest(BaseModel):
    password: str

@router.post("/login")
async def login(req: LoginRequest, request: Request):
    client_ip = request.client.host if request.client else "unknown"
    _check_login_rate(client_ip)

    if not check_webui_password(req.password):
        _record_login_attempt(client_ip)
        raise HTTPException(status_code=401, detail="Incorrect password")

    _clear_login_attempts(client_ip)
    token = create_token(sub="admin")
    return {"token": token}

@router.get("/verify")
async def verify(user: str = Depends(get_current_user)):
    return {"status": "ok", "user": user}
