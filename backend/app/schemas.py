from __future__ import annotations

from datetime import date as dt_date
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

ProviderId = Literal["claude", "codex", "gemini"]
WindowKind = Literal["session", "weekly", "monthly", "opus", "fable", "sonnet", "model"]
OAuthMethod = Literal["pkce_code", "device_code", "localhost_url"]
RefreshStatus = Literal["ok", "expired", "error", "disconnected"]
OAuthPollStatus = Literal["pending", "completed", "failed"]
AlertLevel = Literal[
    "warn",
    "critical",
    "burn_rate",
    "needs_reauth",
    "budget_75",
    "budget_90",
    "budget_100",
    "test",
]
WebhookChannel = Literal["discord", "slack", "telegram"]
CostKeyProvider = Literal["anthropic_admin", "openai_org", "openrouter", "custom_ingest"]
CostDays = Literal[7, 30, 90]
CostGroupBy = Literal["provider", "model", "key"]
CostSubBreakdownKind = Literal["model", "key"]

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


# ── API Key Token & USD Cost Platform Schemas ───────────────────────────────


class CostApiKeyCreateRequest(BaseModel):
    provider: CostKeyProvider
    label: str = Field(..., min_length=1, max_length=80)
    api_key: str = Field(default="", max_length=512)

    @model_validator(mode="after")
    def _validate_api_key(self) -> CostApiKeyCreateRequest:
        cleaned = self.api_key.strip()
        if self.provider != "custom_ingest" and len(cleaned) < 8:
            raise ValueError("請輸入有效的平台計費／Admin API 金鑰（至少 8 個字元）")
        if self.provider == "custom_ingest" and cleaned and len(cleaned) < 8:
            raise ValueError("自訂遙測金鑰若手動指定，長度至少須為 8 個字元")
        return self


class CostApiKeyOut(BaseModel):
    id: int
    provider: CostKeyProvider
    label: str
    masked_key: str
    enabled: bool = True
    last_synced_at: int | None = None
    last_error: str | None = None
    created_at: int


class CostApiKeyCreateResponse(BaseModel):
    key: CostApiKeyOut
    generated_secret: str | None = None


class CostApiKeyListResponse(BaseModel):
    keys: list[CostApiKeyOut] = Field(default_factory=list)
    monthly_budget_usd: float = 0.0


class CostApiKeyDeleteResponse(BaseModel):
    id: int
    deleted: bool


class CostBudgetUpdateRequest(BaseModel):
    monthly_budget_usd: float = Field(..., ge=0.0, le=1_000_000.0)


class MonthlyBudgetStatusOut(BaseModel):
    year_month: str
    monthly_budget_usd: float = 0.0
    month_to_date_usd: float = 0.0
    projected_month_usd: float = 0.0
    utilization_pct: float | None = None
    projected_pct: float | None = None
    will_exceed_budget: bool = False
    active_threshold_pct: int | None = None


class CostIngestItem(BaseModel):
    usage_date: str = Field(..., min_length=10, max_length=10)
    provider: str = Field(..., min_length=1, max_length=40)
    model: str = Field(..., min_length=1, max_length=120)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cached_tokens: int = Field(default=0, ge=0)
    cost_usd: float | None = Field(default=None, ge=0.0)

    @field_validator("usage_date")
    @classmethod
    def _validate_iso_date(cls, value: str) -> str:
        cleaned = value.strip()
        try:
            parsed = dt_date.fromisoformat(cleaned)
        except ValueError as exc:
            raise ValueError("usage_date 必須為合法的 YYYY-MM-DD 日期格式") from exc
        return parsed.isoformat()

    @field_validator("provider")
    @classmethod
    def _normalize_provider(cls, value: str) -> str:
        cleaned = value.strip().lower()
        if not cleaned:
            raise ValueError("provider 不可為空白")
        return cleaned

    @field_validator("model")
    @classmethod
    def _normalize_model(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("model 不可為空白")
        return cleaned


class CostIngestRequest(BaseModel):
    key_id: int | None = None
    records: list[CostIngestItem] = Field(..., min_length=1, max_length=500)


class CostIngestResponse(BaseModel):
    ok: bool = True
    key_id: int
    upserted_count: int
    budget: MonthlyBudgetStatusOut


class CostTotalsOut(BaseModel):
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    total_tokens: int = 0


class CostDailyPointOut(BaseModel):
    date: str
    total_cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    by_provider: dict[str, float] = Field(default_factory=dict)


class CostGroupRowOut(BaseModel):
    group_key: str
    group_label: str
    provider: str | None = None
    key_id: int | None = None
    masked_key: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0
    share_pct: float = 0.0
    record_days: int = 0


class CostSummaryResponse(BaseModel):
    days: CostDays
    start_date: str
    end_date: str
    group_by: CostGroupBy
    provider_filter: str | None = None
    orderby: str
    top: int
    skip: int
    total_count: int
    totals: CostTotalsOut
    budget: MonthlyBudgetStatusOut
    daily_series: list[CostDailyPointOut] = Field(default_factory=list)
    groups: list[CostGroupRowOut] = Field(default_factory=list)
    available_providers: list[str] = Field(default_factory=list)
    bound_keys_count: int = 0
    last_synced_at: int | None = None
    sync_cooldown_seconds: int = 0
    sync_cooldown_remaining: int = 0
    is_demo: bool = False


class CostSyncResponse(BaseModel):
    ok: bool = True
    cooldown_active: bool = False
    synced_keys: int = 0
    upserted_rows: int = 0
    key_errors: dict[str, str] = Field(default_factory=dict)
    sync_cooldown_seconds: int = 0
    sync_cooldown_remaining: int = 0
    summary: CostSummaryResponse


class CostDrilldownDayOut(BaseModel):
    date: str
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0


class CostDrilldownSubItemOut(BaseModel):
    key: str
    label: str
    provider: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0
    share_pct: float = 0.0


class CostDrilldownResponse(BaseModel):
    group_by: CostGroupBy
    group_value: str
    group_label: str
    days: CostDays
    total_cost_usd: float = 0.0
    total_tokens: int = 0
    sub_breakdown_kind: CostSubBreakdownKind
    sub_breakdown: list[CostDrilldownSubItemOut] = Field(default_factory=list)
    daily_breakdown: list[CostDrilldownDayOut] = Field(default_factory=list)
    is_demo: bool = False
