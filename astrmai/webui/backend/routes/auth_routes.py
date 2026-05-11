from fastapi import APIRouter, HTTPException, Depends, Header
from pydantic import BaseModel
from typing import Optional
from ..auth import verify_token, create_token, get_astrmai_password, get_current_user

router = APIRouter()

class LoginRequest(BaseModel):
    password: str

@router.post("/login")
async def login(req: LoginRequest):
    correct_password = get_astrmai_password()
    if req.password != correct_password:
        raise HTTPException(status_code=401, detail="Incorrect password")
    
    token = create_token(sub="admin")
    return {"token": token}

@router.get("/verify")
async def verify(user: str = Depends(get_current_user)):
    return {"status": "ok", "user": user}
