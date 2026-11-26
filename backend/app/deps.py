from __future__ import annotations

from typing import Any

from fastapi import Depends, HTTPException, Request, status

from .crypto import hash_session_token
from .database import Database
from .usage_service import UsageService


def get_db(request: Request) -> Database:
    return request.app.state.db


def get_usage_service(request: Request) -> UsageService:
    return request.app.state.usage_service


def get_optional_user(
    request: Request, db: Database = Depends(get_db)
) -> dict[str, Any] | None:
    cookie_name = request.app.state.settings.cookie_name
    raw_token = request.cookies.get(cookie_name)
    if not raw_token:
        return None
    return db.get_user_by_session(hash_session_token(raw_token))


def get_current_user(
    user: dict[str, Any] | None = Depends(get_optional_user),
) -> dict[str, Any]:
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="尚未登入或登入階段已過期",
        )
    return user
