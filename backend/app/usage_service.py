from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

from .alerts import AlertManager, DnsResolver, credential_fingerprint
from .analytics import enrich_provider_with_history
from .config import Settings
from .database import Database, StoredCredential
from .providers.base import UpstreamUnauthorizedError
from .providers.claude import ClaudeAdapter
from .providers.codex import CodexAdapter
from .providers.gemini import GeminiAdapter
from .schemas import (
    PROVIDER_ORDER,
    DashboardUsageResponse,
    ExtraUsageOut,
    OAuthMethod,
    ProviderConnectionOut,
    ProviderId,
    ProviderUsageOut,
    TrendPointOut,
    UsageWindowOut,
)
from .token_manager import TokenManager, TokenRefreshError

PROVIDER_NAMES: dict[ProviderId, str] = {
    "claude": "Claude",
    "codex": "Codex",
    "gemini": "Gemini",
}

PROVIDER_OAUTH_METHODS: dict[ProviderId, OAuthMethod] = {
    "claude": "pkce_code",
    "codex": "device_code",
    "gemini": "localhost_url",
}


def _demo_sparkline(now: int, values: list[float]) -> list[TrendPointOut]:
    count = len(values)
    start = now - (count - 1) * 3600
    return [
        TrendPointOut(t=start + idx * 3600, v=round(max(0.0, min(100.0, val)), 1))
        for idx, val in enumerate(values)
    ]


def build_demo_dashboard() -> DashboardUsageResponse:
    now = int(time.time())

    claude_s_trend = _demo_sparkline(
        now,
        [
            6, 9, 12, 14, 2, 4, 5, 7, 8, 10, 13, 15,
            3, 4, 6, 7, 8, 9, 11, 12, 14, 15, 16.5, 18.0,
        ],
    )
    claude_w_trend = _demo_sparkline(
        now,
        [
            19, 20, 20.5, 21, 21.5, 22, 23, 23.5, 24, 25, 25.5, 26,
            27, 27.5, 28, 29, 29.5, 30, 31, 31.5, 32, 33, 33.5, 34.0,
        ],
    )
    claude_opus_trend = _demo_sparkline(
        now,
        [
            28, 29, 30, 30.5, 31, 32, 33, 33.5, 34, 35, 36, 37,
            38, 38.5, 39, 40, 41, 41.5, 42, 43, 44, 44.5, 45.2, 46.0,
        ],
    )

    codex_s_trend = _demo_sparkline(
        now,
        [
            18, 22, 28, 31, 5, 8, 12, 16, 21, 25, 29, 34,
            6, 9, 12, 15, 19, 24, 31, 39, 48, 57, 66, 74.0,
        ],
    )
    codex_w_trend = _demo_sparkline(
        now,
        [
            62, 63, 64, 65, 66, 67, 68, 70, 71, 72, 74, 75,
            76, 78, 79, 80, 82, 83, 85, 86, 88, 89, 90.2, 91.0,
        ],
    )

    gemini_pro_trend = _demo_sparkline(
        now,
        [
            4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15,
            16, 17, 18, 19, 20, 21, 22, 23.5, 24.5, 26, 27, 28.0,
        ],
    )
    gemini_flash_trend = _demo_sparkline(
        now,
        [
            1, 1.5, 2, 2, 2.5, 3, 3, 3.5, 4, 4.2, 4.5, 5,
            5.2, 5.5, 6, 6.2, 6.5, 7, 7.2, 7.6, 8, 8.4, 8.7, 9.0,
        ],
    )

    codex_s_exhaust = now + 4800  # 1h 20m (< 2h 10m reset!)
    codex_w_exhaust = now + int((9.0 / 2.2) * 3600)  # ~4h 05m (< 3d reset!)

    providers = [
        ProviderUsageOut(
            id="claude",
            name="Claude",
            ok=True,
            connected=True,
            source="demo",
            plan="Max",
            account_label="demo@claude.ai",
            trend_24h=claude_s_trend,
            peak_burn_rate_per_hour=3.6,
            projected_exhaustion_at=None,
            will_exhaust_before_reset=False,
            windows=[
                UsageWindowOut(
                    label="Session (5h)",
                    kind="session",
                    win="5h",
                    percent=18.0,
                    reset_epoch=now + 4 * 3600 + 25 * 60,
                    burn_rate_per_hour=3.6,
                    projected_exhaustion_at=now + int((82.0 / 3.6) * 3600),
                    will_exhaust_before_reset=False,
                    trend_points=claude_s_trend,
                ),
                UsageWindowOut(
                    label="Weekly (7d)",
                    kind="weekly",
                    win="7d",
                    percent=34.0,
                    reset_epoch=now + 5 * 86400,
                    burn_rate_per_hour=0.9,
                    projected_exhaustion_at=now + int((66.0 / 0.9) * 3600),
                    will_exhaust_before_reset=False,
                    trend_points=claude_w_trend,
                ),
                UsageWindowOut(
                    label="Opus (7d)",
                    kind="opus",
                    win="7d",
                    percent=46.0,
                    reset_epoch=now + 5 * 86400,
                    burn_rate_per_hour=1.2,
                    projected_exhaustion_at=now + int((54.0 / 1.2) * 3600),
                    will_exhaust_before_reset=False,
                    trend_points=claude_opus_trend,
                ),
            ],
            extra_usage=ExtraUsageOut(
                enabled=True,
                used_amount=6.40,
                limit_amount=50.0,
                currency="USD",
                percent=12.8,
            ),
        ),
        ProviderUsageOut(
            id="codex",
            name="Codex",
            ok=True,
            connected=True,
            source="demo",
            plan="Plus",
            account_label="demo@openai.com",
            trend_24h=codex_s_trend,
            peak_burn_rate_per_hour=19.5,
            projected_exhaustion_at=codex_s_exhaust,
            will_exhaust_before_reset=True,
            windows=[
                UsageWindowOut(
                    label="Session (5h)",
                    kind="session",
                    win="5h",
                    percent=74.0,
                    reset_epoch=now + 2 * 3600 + 10 * 60,
                    burn_rate_per_hour=19.5,
                    projected_exhaustion_at=codex_s_exhaust,
                    will_exhaust_before_reset=True,
                    trend_points=codex_s_trend,
                ),
                UsageWindowOut(
                    label="Weekly (7d)",
                    kind="weekly",
                    win="7d",
                    percent=91.0,
                    reset_epoch=now + 3 * 86400,
                    burn_rate_per_hour=2.2,
                    projected_exhaustion_at=codex_w_exhaust,
                    will_exhaust_before_reset=True,
                    trend_points=codex_w_trend,
                ),
            ],
        ),
        ProviderUsageOut(
            id="gemini",
            name="Gemini",
            ok=True,
            connected=True,
            source="demo",
            plan="Paid",
            account_label="demo@gmail.com",
            trend_24h=gemini_pro_trend,
            peak_burn_rate_per_hour=2.4,
            projected_exhaustion_at=None,
            will_exhaust_before_reset=False,
            windows=[
                UsageWindowOut(
                    label="gemini-2.5-pro",
                    kind="model",
                    win="24h",
                    percent=28.0,
                    reset_epoch=now + 11 * 3600,
                    burn_rate_per_hour=2.4,
                    projected_exhaustion_at=now + int((72.0 / 2.4) * 3600),
                    will_exhaust_before_reset=False,
                    trend_points=gemini_pro_trend,
                ),
                UsageWindowOut(
                    label="gemini-2.5-flash",
                    kind="model",
                    win="24h",
                    percent=9.0,
                    reset_epoch=now + 11 * 3600,
                    burn_rate_per_hour=0.8,
                    projected_exhaustion_at=now + int((91.0 / 0.8) * 3600),
                    will_exhaust_before_reset=False,
                    trend_points=gemini_flash_trend,
                ),
            ],
        ),
    ]
    connections = [
        ProviderConnectionOut(
            provider=pid,
            name=PROVIDER_NAMES[pid],
            oauth_method=PROVIDER_OAUTH_METHODS[pid],
            connected=True,
            account_label=f"demo@{pid}.dev",
            expires_at=now + 7200,
            last_refreshed_at=now - 120,
            auto_refresh=True,
            refresh_status="ok",
        )
        for pid in PROVIDER_ORDER
    ]
    return DashboardUsageResponse(
        providers=providers,
        connections=connections,
        fetched_at=now,
        refresh_cooldown_seconds=0,
        refresh_cooldown_remaining=0,
        is_demo=True,
    )


class UsageService:
    def __init__(
        self,
        *,
        db: Database,
        settings: Settings,
        http_client: httpx.AsyncClient,
        dns_resolver: DnsResolver | None = None,
    ) -> None:
        self.db = db
        self.settings = settings
        self.http_client = http_client
        self.claude = ClaudeAdapter()
        self.codex = CodexAdapter()
        self.gemini = GeminiAdapter()
        self.adapters: dict[ProviderId, Any] = {
            "claude": self.claude,
            "codex": self.codex,
            "gemini": self.gemini,
        }
        self.alert_manager = AlertManager(
            db=db,
            settings=settings,
            http_client=http_client,
            dns_resolver=dns_resolver,
        )
        self.token_manager = TokenManager(
            db=db,
            settings=settings,
            refreshers={
                "claude": self.claude,
                "codex": self.codex,
                "gemini": self.gemini,
            },
            http_client=http_client,
            on_terminal_failure=self._safe_notify_reauth,
        )
        self._user_refresh_locks: dict[int, asyncio.Lock] = {}
        self._last_manual_refresh: dict[int, int] = {}

    def _user_lock(self, user_id: int) -> asyncio.Lock:
        lock = self._user_refresh_locks.get(user_id)
        if lock is None:
            lock = asyncio.Lock()
            self._user_refresh_locks[user_id] = lock
        return lock

    async def _safe_notify_reauth(
        self,
        user_id: int,
        provider: ProviderId,
        cred: StoredCredential,
        reason: str,
    ) -> None:
        try:
            await self.alert_manager.notify_reauth_needed(
                user_id,
                provider,
                PROVIDER_NAMES[provider],
                account_label=cred.account_label,
                credential_key=credential_fingerprint(cred.encrypted_payload),
                reason=reason,
            )
        except Exception:
            pass

    async def fetch_and_store_provider(
        self, user_id: int, provider: ProviderId
    ) -> ProviderUsageOut | None:
        try:
            cred = await self.token_manager.ensure_valid_token(user_id, provider, force=False)
        except TokenRefreshError as exc:
            stored = self.db.get_credential(user_id, provider)
            if stored is None:
                return None
            err_usage = ProviderUsageOut(
                id=provider,
                name=PROVIDER_NAMES[provider],
                ok=False,
                connected=True,
                error=str(exc),
                code=f"expired_{provider}" if exc.terminal else "refresh_failed",
                source="oauth",
                account_label=stored.account_label,
            )
            if not self.db.upsert_usage_snapshot(
                user_id,
                err_usage,
                int(time.time()),
                expected_encrypted_payload=stored.encrypted_payload,
            ):
                return None
            if exc.terminal:
                await self._safe_notify_reauth(user_id, provider, stored, str(exc))
            return err_usage

        if cred is None:
            return None

        active_cred = cred
        adapter = self.adapters[provider]
        reauth_reason: str | None = None
        try:
            usage = await adapter.fetch_usage(cred, self.http_client)
        except UpstreamUnauthorizedError:
            refreshed: StoredCredential | None = None
            try:
                refreshed = await self.token_manager.ensure_valid_token(
                    user_id, provider, force=True
                )
                if refreshed is None:
                    return None
                active_cred = refreshed
                usage = await adapter.fetch_usage(refreshed, self.http_client)
            except (TokenRefreshError, UpstreamUnauthorizedError) as exc:
                target_cred = refreshed if refreshed is not None else cred
                matched = self.db.mark_credential_status(
                    user_id,
                    provider,
                    refresh_status="expired",
                    refresh_error=str(exc),
                    expected_encrypted_payload=target_cred.encrypted_payload,
                )
                if not matched:
                    return None
                active_cred = target_cred
                reauth_reason = str(exc)
                usage = ProviderUsageOut(
                    id=provider,
                    name=PROVIDER_NAMES[provider],
                    ok=False,
                    connected=True,
                    error="憑證已失效且自動續期失敗，請重新連結帳號",
                    code=f"expired_{provider}",
                    source="oauth",
                    account_label=target_cred.account_label,
                )
        except Exception as exc:
            usage = ProviderUsageOut(
                id=provider,
                name=PROVIDER_NAMES[provider],
                ok=False,
                connected=True,
                error=f"連線失敗: {exc}",
                code="connect",
                arg=str(exc),
                source="oauth",
                account_label=active_cred.account_label,
            )

        now = int(time.time())
        if usage.ok and usage.windows:
            self.db.record_usage_history(
                user_id,
                usage,
                now,
                min_interval_seconds=self.settings.history_min_interval_seconds,
                retention_days=self.settings.history_retention_days,
                expected_encrypted_payload=active_cred.encrypted_payload,
            )
            user_history = self.db.list_user_history_since(user_id, now - 86400)
            usage = enrich_provider_with_history(
                usage,
                user_history.get(provider, {}),
                now=now,
            )

        if not self.db.upsert_usage_snapshot(
            user_id,
            usage,
            now,
            expected_encrypted_payload=active_cred.encrypted_payload,
        ):
            return None

        if reauth_reason is not None:
            await self._safe_notify_reauth(user_id, provider, active_cred, reauth_reason)
        elif usage.ok:
            try:
                await self.alert_manager.evaluate_usage_alerts(user_id, usage, now=now)
            except Exception:
                pass

        return usage

    def _build_connection_out(
        self, provider: ProviderId, cred: StoredCredential | None
    ) -> ProviderConnectionOut:
        if cred is None:
            return ProviderConnectionOut(
                provider=provider,
                name=PROVIDER_NAMES[provider],
                oauth_method=PROVIDER_OAUTH_METHODS[provider],
                connected=False,
                refresh_status="disconnected",
            )
        return ProviderConnectionOut(
            provider=provider,
            name=PROVIDER_NAMES[provider],
            oauth_method=PROVIDER_OAUTH_METHODS[provider],
            connected=True,
            account_label=cred.account_label,
            expires_at=cred.expires_at,
            last_refreshed_at=cred.last_refreshed_at,
            auto_refresh=bool(cred.refresh_token),
            refresh_status=cred.refresh_status,
            refresh_error=cred.refresh_error,
        )

    def _remaining_cooldown(self, user_id: int) -> int:
        last = self._last_manual_refresh.get(user_id)
        if last is None:
            return 0
        rem = (last + self.settings.refresh_cooldown_seconds) - int(time.time())
        return max(0, rem)

    async def get_dashboard_for_user(self, user_id: int) -> DashboardUsageResponse:
        creds = self.db.list_user_credentials(user_id)
        snapshots, fetched_at = self.db.list_user_snapshots(user_id)

        missing = [pid for pid in PROVIDER_ORDER if pid in creds and pid not in snapshots]
        if missing:
            await asyncio.gather(*(self.fetch_and_store_provider(user_id, pid) for pid in missing))
            creds = self.db.list_user_credentials(user_id)
            snapshots, fetched_at = self.db.list_user_snapshots(user_id)

        now = int(time.time())
        history_by_provider = (
            self.db.list_user_history_since(user_id, now - 86400) if creds else {}
        )

        providers_out: list[ProviderUsageOut] = []
        for pid in PROVIDER_ORDER:
            if pid in creds and pid in snapshots:
                providers_out.append(
                    enrich_provider_with_history(
                        snapshots[pid],
                        history_by_provider.get(pid, {}),
                        now=now,
                    )
                )

        connections_out = [self._build_connection_out(pid, creds.get(pid)) for pid in PROVIDER_ORDER]
        rem_cooldown = self._remaining_cooldown(user_id)

        return DashboardUsageResponse(
            providers=providers_out,
            connections=connections_out,
            fetched_at=fetched_at,
            refresh_cooldown_seconds=rem_cooldown,
            refresh_cooldown_remaining=rem_cooldown,
            is_demo=False,
        )

    async def refresh_user_dashboard(
        self, user_id: int, *, bypass_cooldown: bool = False
    ) -> DashboardUsageResponse:
        async with self._user_lock(user_id):
            if not bypass_cooldown and self._remaining_cooldown(user_id) > 0:
                return await self.get_dashboard_for_user(user_id)

            creds = self.db.list_user_credentials(user_id)
            if creds:
                self._last_manual_refresh[user_id] = int(time.time())
                await asyncio.gather(
                    *(self.fetch_and_store_provider(user_id, pid) for pid in creds.keys())
                )
            return await self.get_dashboard_for_user(user_id)

    async def poll_all_active_credentials(self) -> None:
        await self.token_manager.refresh_expiring_on_startup()
        keys = self.db.list_all_credential_keys()
        if not keys:
            return
        sem = asyncio.Semaphore(6)

        async def _guarded(uid: int, prov: ProviderId) -> None:
            async with sem:
                await self.fetch_and_store_provider(uid, prov)

        await asyncio.gather(*(_guarded(u, p) for u, p in keys))
