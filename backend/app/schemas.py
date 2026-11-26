from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

ProviderId = Literal["claude", "codex", "gemini"]
WindowKind = Literal["session", "weekly", "monthly", "opus", "fable", "sonnet", "model"]
OAuthMethod = Literal["pkce_code", "device_code", "localhost_url"]
RefreshStatus = Literal["ok", "expired", "error", "disconnected"]
OAuthPollStatus = Literal["pending", "completed", "failed"]

PROVIDER_ORDER: tuple[ProviderId, ...] = ("claude", "codex", "gemini")


class UserOut(BaseModel):
    id: int
    username: str
    created_at: int


class AuthCredentialsRequest(BaseModel):
    username: str = Field(..., min_length=2, max_length=64)
    password: str = Field(..., min_length=6, max_length=128)


class AuthSessionResponse(BaseModel):
    authenticated: bool
    user: UserOut | None = None
    allow_registration: bool = True


class UsageWindowOut(BaseModel):
    label: str
    kind: WindowKind | None = None
    win: str | None = None
    percent: float = Field(..., ge=0.0, le=100.0)
    reset_epoch: int | None = None


class ExtraUsageOut(BaseModel):
    enabled: bool = False
    used_amount: float | None = None
    limit_amount: float | None = None
    currency: str = "USD"
    percent: float | None = None


class ProviderUsageOut(BaseModel):
    id: ProviderId
    name: str
    ok: bool
    connected: bool = True
    error: str | None = None
    code: str | None = None
    arg: str | int | None = None
    source: str | None = None
    plan: str | None = None
    account_label: str | None = None
    windows: list[UsageWindowOut] = Field(default_factory=list)
    extra_usage: ExtraUsageOut | None = None


class ProviderConnectionOut(BaseModel):
    provider: ProviderId
    name: str
    oauth_method: OAuthMethod
    connected: bool
    account_label: str | None = None
    expires_at: int | None = None
    last_refreshed_at: int | None = None
    auto_refresh: bool = False
    refresh_status: RefreshStatus = "disconnected"
    refresh_error: str | None = None


class DashboardUsageResponse(BaseModel):
    providers: list[ProviderUsageOut]
    connections: list[ProviderConnectionOut]
    fetched_at: int | None = None
    refresh_cooldown_seconds: int = 0
    is_demo: bool = False


class OAuthStartResponse(BaseModel):
    provider: ProviderId
    oauth_method: OAuthMethod
    state_id: str
    authorize_url: str | None = None
    verification_url: str | None = None
    user_code: str | None = None
    poll_interval_seconds: int | None = None
    expires_at: int


class OAuthCodeSubmitRequest(BaseModel):
    state_id: str = Field(..., min_length=8, max_length=128)
    code_input: str = Field(..., min_length=4, max_length=4096)


class OAuthDevicePollRequest(BaseModel):
    state_id: str = Field(..., min_length=8, max_length=128)


class OAuthFlowStatusResponse(BaseModel):
    provider: ProviderId
    status: OAuthPollStatus
    account_label: str | None = None
    error: str | None = None


class DisconnectResponse(BaseModel):
    provider: ProviderId
    disconnected: bool
