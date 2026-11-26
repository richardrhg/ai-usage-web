from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

ProviderId = Literal["claude", "codex", "gemini"]
WindowKind = Literal["session", "weekly", "monthly", "opus", "fable", "sonnet", "model"]
OAuthMethod = Literal["pkce_code", "device_code", "localhost_url"]
RefreshStatus = Literal["ok", "expired", "error", "disconnected"]
OAuthPollStatus = Literal["pending", "completed", "failed"]
AlertLevel = Literal["warn", "critical", "burn_rate", "needs_reauth", "test"]
WebhookChannel = Literal["discord", "slack", "telegram"]

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


class TrendPointOut(BaseModel):
    t: int
    v: float = Field(..., ge=0.0, le=100.0)


class UsageWindowOut(BaseModel):
    label: str
    kind: WindowKind | None = None
    win: str | None = None
    percent: float = Field(..., ge=0.0, le=100.0)
    reset_epoch: int | None = None
    burn_rate_per_hour: float | None = None
    projected_exhaustion_at: int | None = None
    will_exhaust_before_reset: bool = False
    trend_points: list[TrendPointOut] = Field(default_factory=list)


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
    trend_24h: list[TrendPointOut] = Field(default_factory=list)
    peak_burn_rate_per_hour: float | None = None
    projected_exhaustion_at: int | None = None
    will_exhaust_before_reset: bool = False


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
    refresh_cooldown_remaining: int = 0
    is_demo: bool = False


UsageResponse = DashboardUsageResponse


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


class AlertSettingsPayload(BaseModel):
    enabled: bool = True
    discord_webhook_url: str = Field(default="", max_length=2048)
    slack_webhook_url: str = Field(default="", max_length=2048)
    telegram_webhook_url: str = Field(default="", max_length=2048)
    telegram_chat_id: str = Field(default="", max_length=128)
    warn_threshold: float = Field(default=75.0, ge=1.0, le=99.0)
    critical_threshold: float = Field(default=90.0, ge=1.0, le=100.0)
    burn_rate_threshold_per_hour: float = Field(default=25.0, ge=1.0, le=500.0)
    notify_on_burn_rate: bool = True
    notify_on_reauth: bool = True

    @model_validator(mode="after")
    def _validate_threshold_order(self) -> AlertSettingsPayload:
        if self.critical_threshold < self.warn_threshold:
            raise ValueError("嚴重門檻 (critical_threshold) 必須大於或等於警告門檻 (warn_threshold)")
        return self


class AlertEventOut(BaseModel):
    id: int
    provider: str
    window_key: str
    alert_level: str
    resets_at: int
    message: str
    delivered_channels: list[str] = Field(default_factory=list)
    created_at: int


class AlertSettingsResponse(AlertSettingsPayload):
    updated_at: int | None = None
    recent_events: list[AlertEventOut] = Field(default_factory=list)


class AlertTestRequest(BaseModel):
    discord_webhook_url: str | None = Field(default=None, max_length=2048)
    slack_webhook_url: str | None = Field(default=None, max_length=2048)
    telegram_webhook_url: str | None = Field(default=None, max_length=2048)
    telegram_chat_id: str | None = Field(default=None, max_length=128)


class AlertTestResponse(BaseModel):
    ok: bool
    delivered_channels: list[WebhookChannel] = Field(default_factory=list)
    errors: dict[str, str] = Field(default_factory=dict)
