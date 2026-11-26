from __future__ import annotations

import secrets
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status

from ..crypto import generate_pkce_pair
from ..database import Database
from ..deps import get_current_user, get_db, get_usage_service
from ..providers.base import OAuthExchangeError
from ..providers.claude import build_claude_authorize_url
from ..providers.gemini import build_gemini_authorize_url
from ..schemas import (
    DisconnectResponse,
    OAuthCodeSubmitRequest,
    OAuthDevicePollRequest,
    OAuthFlowStatusResponse,
    OAuthStartResponse,
    ProviderId,
)
from ..usage_service import UsageService

router = APIRouter(prefix="/api/oauth", tags=["oauth"])


@router.post(
    "/{provider}/start",
    response_model=OAuthStartResponse,
    operation_id="startProviderOAuth",
)
async def start_oauth(
    provider: ProviderId,
    user: dict[str, Any] = Depends(get_current_user),
    db: Database = Depends(get_db),
    svc: UsageService = Depends(get_usage_service),
) -> OAuthStartResponse:
    state_id = secrets.token_urlsafe(24)
    user_id = int(user["id"])

    if provider == "claude":
        verifier, challenge = generate_pkce_pair()
        oauth_state = secrets.token_urlsafe(24)
        url = build_claude_authorize_url(
            client_id=svc.settings.claude_client_id,
            code_challenge=challenge,
            oauth_state=oauth_state,
        )
        expires_at = db.create_oauth_state(
            state_id=state_id,
            user_id=user_id,
            provider="claude",
            flow_data={"code_verifier": verifier, "oauth_state": oauth_state},
        )
        return OAuthStartResponse(
            provider="claude",
            oauth_method="pkce_code",
            state_id=state_id,
            authorize_url=url,
            expires_at=expires_at,
        )

    if provider == "codex":
        try:
            dc = await svc.codex.start_device_code(svc.http_client, svc.settings)
        except OAuthExchangeError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=str(exc),
            ) from exc
        expires_at = db.create_oauth_state(
            state_id=state_id,
            user_id=user_id,
            provider="codex",
            flow_data={
                "device_auth_id": dc.device_auth_id,
                "user_code": dc.user_code,
                "interval_seconds": dc.interval_seconds,
            },
        )
        return OAuthStartResponse(
            provider="codex",
            oauth_method="device_code",
            state_id=state_id,
            verification_url=dc.verification_url,
            user_code=dc.user_code,
            poll_interval_seconds=dc.interval_seconds,
            expires_at=expires_at,
        )

    # provider == "gemini"
    if not svc.settings.gemini_client_id or not svc.settings.gemini_client_secret:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="尚未設定 GEMINI_OAUTH_CLIENT_ID 與 GEMINI_OAUTH_CLIENT_SECRET 環境變數",
        )
    verifier, challenge = generate_pkce_pair()
    oauth_state = secrets.token_urlsafe(24)
    url = build_gemini_authorize_url(
        client_id=svc.settings.gemini_client_id,
        code_challenge=challenge,
        oauth_state=oauth_state,
    )
    expires_at = db.create_oauth_state(
        state_id=state_id,
        user_id=user_id,
        provider="gemini",
        flow_data={"code_verifier": verifier, "oauth_state": oauth_state},
    )
    return OAuthStartResponse(
        provider="gemini",
        oauth_method="localhost_url",
        state_id=state_id,
        authorize_url=url,
        expires_at=expires_at,
    )


@router.post(
    "/{provider}/complete",
    response_model=OAuthFlowStatusResponse,
    operation_id="completeProviderOAuth",
)
async def complete_code_oauth(
    provider: ProviderId,
    payload: OAuthCodeSubmitRequest,
    user: dict[str, Any] = Depends(get_current_user),
    db: Database = Depends(get_db),
    svc: UsageService = Depends(get_usage_service),
) -> OAuthFlowStatusResponse:
    if provider == "codex":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="ChatGPT 使用裝置碼輪詢端點 /api/oauth/codex/poll",
        )

    user_id = int(user["id"])
    flow = db.get_oauth_state(payload.state_id, user_id, provider)
    if flow is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="授權階段不存在或已過期，請重新點擊連結",
        )

    adapter = svc.claude if provider == "claude" else svc.gemini
    try:
        tokens = await adapter.exchange_code(
            code_input=payload.code_input,
            code_verifier=str(flow.flow_data.get("code_verifier") or ""),
            expected_state=str(flow.flow_data.get("oauth_state") or ""),
            client=svc.http_client,
            settings=svc.settings,
        )
    except OAuthExchangeError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    now = int(time.time())
    db.upsert_credential(
        user_id=user_id,
        provider=provider,
        access_token=tokens.access_token,
        refresh_token=tokens.refresh_token,
        id_token=tokens.id_token,
        account_id=tokens.account_id,
        project_id=tokens.project_id,
        account_label=tokens.account_label,
        expires_at=tokens.expires_at,
        last_refreshed_at=now,
        refresh_status="ok",
        refresh_error=None,
    )
    db.delete_oauth_state(flow.state_id)
    await svc.fetch_and_store_provider(user_id, provider)

    return OAuthFlowStatusResponse(
        provider=provider,
        status="completed",
        account_label=tokens.account_label,
    )


@router.post(
    "/codex/poll",
    response_model=OAuthFlowStatusResponse,
    operation_id="pollCodexDeviceOAuth",
)
async def poll_codex_device(
    payload: OAuthDevicePollRequest,
    user: dict[str, Any] = Depends(get_current_user),
    db: Database = Depends(get_db),
    svc: UsageService = Depends(get_usage_service),
) -> OAuthFlowStatusResponse:
    user_id = int(user["id"])
    flow = db.get_oauth_state(payload.state_id, user_id, "codex")
    if flow is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="裝置碼已過期或不存在，請重新產生驗證碼",
        )

    try:
        tokens = await svc.codex.poll_device_code(
            device_auth_id=str(flow.flow_data.get("device_auth_id") or ""),
            user_code=str(flow.flow_data.get("user_code") or ""),
            client=svc.http_client,
            settings=svc.settings,
        )
    except OAuthExchangeError as exc:
        db.update_oauth_state_status(flow.state_id, status="failed", error=str(exc))
        return OAuthFlowStatusResponse(
            provider="codex",
            status="failed",
            error=str(exc),
        )

    if tokens is None:
        return OAuthFlowStatusResponse(provider="codex", status="pending")

    now = int(time.time())
    db.upsert_credential(
        user_id=user_id,
        provider="codex",
        access_token=tokens.access_token,
        refresh_token=tokens.refresh_token,
        id_token=tokens.id_token,
        account_id=tokens.account_id,
        project_id=tokens.project_id,
        account_label=tokens.account_label,
        expires_at=tokens.expires_at,
        last_refreshed_at=now,
        refresh_status="ok",
        refresh_error=None,
    )
    db.delete_oauth_state(flow.state_id)
    await svc.fetch_and_store_provider(user_id, "codex")

    return OAuthFlowStatusResponse(
        provider="codex",
        status="completed",
        account_label=tokens.account_label,
    )


@router.delete(
    "/{provider}",
    response_model=DisconnectResponse,
    operation_id="disconnectProviderOAuth",
)
async def disconnect_provider(
    provider: ProviderId,
    user: dict[str, Any] = Depends(get_current_user),
    db: Database = Depends(get_db),
) -> DisconnectResponse:
    removed = db.delete_credential(int(user["id"]), provider)
    return DisconnectResponse(provider=provider, disconnected=removed)
