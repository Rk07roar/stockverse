"""
StockVest — api/auth.py
User registration, login, and token verification.
Uses utils/auth.py for JWT + bcrypt; users stored in SQLite.
"""
from fastapi import APIRouter, HTTPException, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, EmailStr
from typing import Optional
from datetime import datetime
import aiosqlite

from db import DB_PATH
from utils.auth import hash_password, verify_password, create_access_token, decode_token

router  = APIRouter()
_bearer = HTTPBearer(auto_error=False)


# ── Request / Response models ──────────────────────────────────
class RegisterRequest(BaseModel):
    username: str
    email:    str
    password: str

class LoginRequest(BaseModel):
    username: str   # accepts username OR email
    password: str

class TokenResponse(BaseModel):
    access_token: str
    token_type:   str = "bearer"
    username:     str
    email:        str


# ── Helpers ────────────────────────────────────────────────────
async def _get_user_by_username(username: str) -> Optional[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        row = await (await db.execute(
            "SELECT * FROM users WHERE username=? OR email=? COLLATE NOCASE LIMIT 1",
            (username, username)
        )).fetchone()
    return dict(row) if row else None


# ── Endpoints ──────────────────────────────────────────────────
@router.post("/register", response_model=TokenResponse, summary="Create a new account")
async def register(req: RegisterRequest):
    if len(req.username) < 3:
        raise HTTPException(400, "Username must be at least 3 characters")
    if len(req.password) < 6:
        raise HTTPException(400, "Password must be at least 6 characters")

    hashed = hash_password(req.password)
    now    = datetime.now().strftime("%Y-%m-%d")
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT INTO users (username, email, hashed_pw, created_at) VALUES (?,?,?,?)",
                (req.username.strip(), req.email.strip().lower(), hashed, now)
            )
            await db.commit()
    except aiosqlite.IntegrityError:
        raise HTTPException(400, "Username or email already exists")

    token = create_access_token({"sub": req.username, "email": req.email})
    return TokenResponse(access_token=token, username=req.username, email=req.email)


@router.post("/login", response_model=TokenResponse, summary="Log in and get a JWT token")
async def login(req: LoginRequest):
    user = await _get_user_by_username(req.username)
    if not user or not verify_password(req.password, user["hashed_pw"]):
        raise HTTPException(401, "Invalid username or password")
    if not user["is_active"]:
        raise HTTPException(403, "Account is disabled")

    token = create_access_token({"sub": user["username"], "email": user["email"]})
    return TokenResponse(access_token=token, username=user["username"], email=user["email"])


@router.get("/me", summary="Get current user info from JWT token")
async def me(creds: HTTPAuthorizationCredentials = Depends(_bearer)):
    if not creds:
        raise HTTPException(401, "Not authenticated")
    payload = decode_token(creds.credentials)
    if not payload:
        raise HTTPException(401, "Invalid or expired token")
    user = await _get_user_by_username(payload["sub"])
    if not user:
        raise HTTPException(404, "User not found")
    return {
        "username":   user["username"],
        "email":      user["email"],
        "created_at": user["created_at"],
    }


@router.get("/verify", summary="Verify a JWT token is valid")
async def verify(creds: HTTPAuthorizationCredentials = Depends(_bearer)):
    if not creds:
        raise HTTPException(401, "No token provided")
    payload = decode_token(creds.credentials)
    if not payload:
        raise HTTPException(401, "Invalid or expired token")
    return {"valid": True, "username": payload.get("sub"), "email": payload.get("email")}
