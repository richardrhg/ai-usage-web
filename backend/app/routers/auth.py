from __future__ import annotations

import re
import sqlite3
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from ..crypto import (
    generate_session_token,
    hash_password,
    hash_session_token,
    verify_password,
)
from ..database import Database
from ..deps import get_db, get_optional_user
from ..schemas import AuthCredentialsRequest, AuthSessionResponse, UserOut

router = APIRouter(prefix="/api/auth", tags=["auth"])

USERNAME_RE = re.compile(r"^[a-zA-Z0-9_.-]{2,64}$")


def _set_session_cookie(request: Request, response: Response, raw_token: str) -> None:
    settings = request.app.state.settings
    response.set_cookie(
        key=settings.cookie_name,
        value=raw_token,
        max_age=settings.session_ttl_seconds,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
    )


@router.get("/me", response_model=AuthSessionResponse, operation_id="getAuthSession")
async def get_me(
    request: Request,
    user: dict[str, Any] | None = Depends(get_optional_user),
) -> AuthSessionResponse:
    allow_reg = request.app.state.settings.allow_registration
    if user is None:
        return AuthSessionResponse(authenticated=False, user=None, allow_registration=allow_reg)
    return AuthSessionResponse(
        authenticated=True,
        user=UserOut(**user),
        allow_registration=allow_reg,
    )


@router.post("/register", response_model=AuthSessionResponse, operation_id="registerUser")
async def register(
    payload: AuthCredentialsRequest,
    request: Request,
    response: Response,
    db: Database = Depends(get_db),
) -> AuthSessionResponse:
    settings = request.app.state.settings
    if not settings.allow_registration:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="目前伺服器已關閉公開註冊",
        )
    username = payload.username.strip()
    if not USERNAME_RE.match(username):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="帳號僅允許英數字、底線、點與連字號 (2~64 字元)",
        )
    try:
        user = db.create_user(username, hash_password(payload.password))
    except sqlite3.IntegrityError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="此帳號名稱已被註冊",
        ) from None

    raw_session = generate_session_token()
    db.create_session(hash_session_token(raw_session), user["id"], settings.session_ttl_seconds)
    _set_session_cookie(request, response, raw_session)
    return AuthSessionResponse(
        authenticated=True,
        user=UserOut(**user),
        allow_registration=settings.allow_registration,
    )


@router.post("/login", response_model=AuthSessionResponse, operation_id="loginUser")
async def login(
    payload: AuthCredentialsRequest,
    request: Request,
    response: Response,
    db: Database = Depends(get_db),
) -> AuthSessionResponse:
    settings = request.app.state.settings
    row = db.get_user_by_username(payload.username.strip())
    if row is None or not verify_password(payload.password, str(row["password_hash"])):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="帳號或密碼錯誤",
        )

    user = {
        "id": int(row["id"]),
        "username": str(row["username"]),
        "created_at": int(row["created_at"]),
    }
    raw_session = generate_session_token()
    db.create_session(hash_session_token(raw_session), user["id"], settings.session_ttl_seconds)
    _set_session_cookie(request, response, raw_session)
    return AuthSessionResponse(
        authenticated=True,
        user=UserOut(**user),
        allow_registration=settings.allow_registration,
    )


@router.post("/logout", response_model=AuthSessionResponse, operation_id="logoutUser")
async def logout(
    request: Request,
    response: Response,
    db: Database = Depends(get_db),
) -> AuthSessionResponse:
    settings = request.app.state.settings
    raw_token = request.cookies.get(settings.cookie_name)
    if raw_token:
        db.delete_session(hash_session_token(raw_token))
    response.delete_cookie(key=settings.cookie_name, path="/")
    return AuthSessionResponse(
        authenticated=False,
        user=None,
        allow_registration=settings.allow_registration,
    )
