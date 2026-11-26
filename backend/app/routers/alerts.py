from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, status

from ..alerts import WebhookValidationError, validate_alert_settings_urls
from ..database import Database
from ..deps import get_current_user, get_db, get_usage_service
from ..schemas import (
    AlertSettingsPayload,
    AlertSettingsResponse,
    AlertTestRequest,
    AlertTestResponse,
)
from ..usage_service import UsageService

router = APIRouter(prefix="/api/alerts", tags=["alerts"])


@router.get(
    "/settings",
    response_model=AlertSettingsResponse,
    operation_id="getAlertSettings",
)
async def get_alert_settings(
    user: dict[str, Any] = Depends(get_current_user),
    db: Database = Depends(get_db),
) -> AlertSettingsResponse:
    return db.get_alert_settings(int(user["id"]))


@router.put(
    "/settings",
    response_model=AlertSettingsResponse,
    operation_id="updateAlertSettings",
)
async def update_alert_settings(
    payload: AlertSettingsPayload,
    user: dict[str, Any] = Depends(get_current_user),
    db: Database = Depends(get_db),
    svc: UsageService = Depends(get_usage_service),
) -> AlertSettingsResponse:
    try:
        validated = await asyncio.to_thread(
            validate_alert_settings_urls,
            payload,
            allow_private_targets=svc.settings.allow_private_webhook_targets,
            dns_resolver=svc.alert_manager.dns_resolver,
        )
    except WebhookValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    return db.upsert_alert_settings(int(user["id"]), validated)


@router.post(
    "/test",
    response_model=AlertTestResponse,
    operation_id="sendTestAlert",
)
async def send_test_alert(
    payload: AlertTestRequest | None = Body(default=None),
    user: dict[str, Any] = Depends(get_current_user),
    svc: UsageService = Depends(get_usage_service),
) -> AlertTestResponse:
    try:
        return await svc.alert_manager.send_test_alert(int(user["id"]), payload)
    except WebhookValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
