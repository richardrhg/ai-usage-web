from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

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


def build_demo_dashboard() -> DashboardUsageResponse:
    now = int(time.time())
    providers = [
        ProviderUsageOut(
            id="claude",
            name="Claude",
            ok=True,
            connected=True,
            source="demo",
            plan="Max",
            account_label="demo@claude.ai",
            windows=[
                UsageWindowOut(
                    label="Session (5h)",
                    kind="session",
                    win="5h",
                    percent=18.0,
                    reset_epoch=now + 4 * 3600 + 25 * 60,
                ),
                UsageWindowOut(
                    label="Weekly (7d)",
                    kind="weekly",
                    win="7d",
                    percent=34.0,
                    reset_epoch=now + 5 * 86400,
                ),
                UsageWindowOut(
                    label="Opus (7d)",
                    kind="opus",
                    win="7d",
                    percent=46.0,
                    reset_epoch=now + 5 * 86400,
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
            windows=[
                UsageWindowOut(
                    label="Session (5h)",
                    kind="session",
                    win="5h",
                    percent=74.0,
                    reset_epoch=now + 2 * 3600 + 10 * 60,
                ),
                UsageWindowOut(
                    label="Weekly (7d)",
                    kind="weekly",
                    win="7d",
                    percent=91.0,
                    reset_epoch=now + 3 * 86400,
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
            windows=[
                UsageWindowOut(
                    label="gemini-2.5-pro",
                    kind="model",
                    win="24h",
                    percent=28.0,
                    reset_epoch=now + 11 * 3600,
                ),
                UsageWindowOut(
                    label="gemini-2.5-flash",
                    kind="model",
                    win="24h",
                    percent=9.0,
                    reset_epoch=now + 11 * 3600,
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
        is_demo=True,
    )


class UsageService:
    def __init__(
        self,
        *,
        db: Database,
        settings: Settings,
        http_client: httpx.AsyncClient,
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
        self.token_manager = TokenManager(
            db=db,
            settings=settings,
            refreshers={
                "claude": self.claude,
                "codex": self.codex,
                "gemini": self.gemini,
            },
            http_client=http_client,
        )
        self._user_refresh_locks: dict[int, asyncio.Lock] = {}
        self._last_manual_refresh: dict[int, int] = {}

    def _user_lock(self, user_id: int) -> asyncio.Lock:
        lock = self._user_refresh_locks.get(user_id)
        if lock is None:
            lock = asyncio.Lock()
            self._user_refresh_locks[user_id] = lock
        return lock

    async def fetch_and_store_provider(
        self, user_id: int, provider: ProviderId
    ) -> ProviderUsageOut | None:
        try:
            cred = await self.token_manager.ensure_valid_token(user_id, provider, force=False)
        except TokenRefreshError as exc:
            stored = self.db.get_credential(user_id, provider)
            err_usage = ProviderUsageOut(
                id=provider,
                name=PROVIDER_NAMES[provider],
                ok=False,
                connected=True,
                error=str(exc),
                code=f"expired_{provider}" if exc.terminal else "refresh_failed",
                source="oauth",
                account_label=stored.account_label if stored else None,
            )
            self.db.upsert_usage_snapshot(user_id, err_usage, int(time.time()))
            return err_usage

        if cred is None:
            return None

        adapter = self.adapters[provider]
        try:
            usage = await adapter.fetch_usage(cred, self.http_client)
        except UpstreamUnauthorizedError:
            try:
                refreshed = await self.token_manager.ensure_valid_token(
                    user_id, provider, force=True
                )
                if refreshed is None:
                    return None
                usage = await adapter.fetch_usage(refreshed, self.http_client)
            except (TokenRefreshError, UpstreamUnauthorizedError) as exc:
                self.db.mark_credential_status(
                    user_id,
                    provider,
                    refresh_status="expired",
                    refresh_error=str(exc),
                )
                usage = ProviderUsageOut(
                    id=provider,
                    name=PROVIDER_NAMES[provider],
                    ok=False,
                    connected=True,
                    error="憑證已失效且自動續期失敗，請重新連結帳號",
                    code=f"expired_{provider}",
                    source="oauth",
                    account_label=cred.account_label,
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
                account_label=cred.account_label,
            )

        self.db.upsert_usage_snapshot(user_id, usage, int(time.time()))
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

        providers_out = [snapshots[pid] for pid in PROVIDER_ORDER if pid in creds and pid in snapshots]
        connections_out = [self._build_connection_out(pid, creds.get(pid)) for pid in PROVIDER_ORDER]

        return DashboardUsageResponse(
            providers=providers_out,
            connections=connections_out,
            fetched_at=fetched_at,
            refresh_cooldown_seconds=self._remaining_cooldown(user_id),
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
