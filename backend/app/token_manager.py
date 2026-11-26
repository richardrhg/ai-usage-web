from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Protocol

import httpx

from .config import Settings
from .database import Database, StoredCredential
from .schemas import ProviderId


@dataclass
class RefreshedTokens:
    access_token: str
    refresh_token: str | None
    id_token: str | None = None
    account_id: str | None = None
    project_id: str | None = None
    account_label: str | None = None
    expires_at: int | None = None


class TokenRefreshError(Exception):
    def __init__(self, message: str, *, terminal: bool = False) -> None:
        super().__init__(message)
        self.terminal = terminal


class ProviderOAuthRefresher(Protocol):
    provider_id: ProviderId

    async def refresh_credential(
        self, cred: StoredCredential, client: httpx.AsyncClient, settings: Settings
    ) -> RefreshedTokens: ...


class TokenManager:
    """
    Centralized Single-flight OAuth token manager.

    Both Anthropic (Claude) and OpenAI (ChatGPT/Codex) rotate refresh tokens on
    every refresh. Concurrent refresh calls using the same refresh_token would
    cause the second request to fail with `refresh_token_reused` and revoke the
    session. Per-(user_id, provider) locks + inside-lock re-reads prevent that.
    """

    def __init__(
        self,
        db: Database,
        settings: Settings,
        refreshers: dict[ProviderId, ProviderOAuthRefresher],
        http_client: httpx.AsyncClient,
        on_terminal_failure: (
            Callable[[int, ProviderId, StoredCredential, str], Awaitable[None]] | None
        ) = None,
    ) -> None:
        self.db = db
        self.settings = settings
        self.refreshers = refreshers
        self.http_client = http_client
        self.on_terminal_failure = on_terminal_failure
        self._locks: dict[tuple[int, ProviderId], asyncio.Lock] = {}

    def _lock_for(self, user_id: int, provider: ProviderId) -> asyncio.Lock:
        key = (user_id, provider)
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    def _needs_refresh(self, cred: StoredCredential, now: int) -> bool:
        if not cred.refresh_token:
            return False
        if cred.expires_at is None:
            return False
        return cred.expires_at <= now + self.settings.proactive_refresh_buffer_seconds

    async def ensure_valid_token(
        self, user_id: int, provider: ProviderId, *, force: bool = False
    ) -> StoredCredential | None:
        cred = self.db.get_credential(user_id, provider)
        if cred is None:
            return None

        now = int(time.time())
        if not force and not self._needs_refresh(cred, now):
            return cred

        if not cred.refresh_token:
            if force or (cred.expires_at is not None and cred.expires_at <= now):
                self.db.mark_credential_status(
                    user_id,
                    provider,
                    refresh_status="expired",
                    refresh_error="Missing refresh_token; please reconnect account.",
                    expected_encrypted_payload=cred.encrypted_payload,
                )
            return self.db.get_credential(user_id, provider)

        wait_started_at = int(time.time())
        async with self._lock_for(user_id, provider):
            latest = self.db.get_credential(user_id, provider)
            if latest is None:
                return None

            now_locked = int(time.time())
            if not force and not self._needs_refresh(latest, now_locked):
                return latest
            if (
                force
                and latest.last_refreshed_at is not None
                and latest.last_refreshed_at >= wait_started_at
                and latest.access_token != cred.access_token
            ):
                return latest

            refresher = self.refreshers[provider]
            try:
                updated = await refresher.refresh_credential(latest, self.http_client, self.settings)
            except TokenRefreshError as exc:
                matched = self.db.mark_credential_status(
                    user_id,
                    provider,
                    refresh_status="expired" if exc.terminal else "error",
                    refresh_error=str(exc),
                    expected_encrypted_payload=latest.encrypted_payload,
                )
                if not matched:
                    return self.db.get_credential(user_id, provider)
                if exc.terminal and self.on_terminal_failure is not None:
                    await self.on_terminal_failure(user_id, provider, latest, str(exc))
                raise
            except Exception as exc:
                matched = self.db.mark_credential_status(
                    user_id,
                    provider,
                    refresh_status="error",
                    refresh_error=f"Token refresh failed: {exc}",
                    expected_encrypted_payload=latest.encrypted_payload,
                )
                if not matched:
                    return self.db.get_credential(user_id, provider)
                raise TokenRefreshError(f"Token refresh failed: {exc}", terminal=False) from exc

            refreshed_at = int(time.time())
            persisted = self.db.update_refreshed_credential(
                user_id=user_id,
                provider=provider,
                expected_encrypted_payload=latest.encrypted_payload,
                access_token=updated.access_token,
                refresh_token=updated.refresh_token or latest.refresh_token,
                id_token=updated.id_token or latest.id_token,
                account_id=updated.account_id or latest.account_id,
                project_id=updated.project_id or latest.project_id,
                account_label=updated.account_label or latest.account_label,
                expires_at=updated.expires_at,
                last_refreshed_at=refreshed_at,
                refresh_status="ok",
                refresh_error=None,
            )
            if persisted is None:
                return self.db.get_credential(user_id, provider)
            return persisted

    async def refresh_expiring_on_startup(self) -> int:
        threshold = int(time.time()) + self.settings.proactive_refresh_buffer_seconds
        targets = self.db.list_expiring_credential_keys(threshold)
        if not targets:
            return 0

        async def _refresh_one(uid: int, prov: ProviderId) -> bool:
            try:
                await self.ensure_valid_token(uid, prov, force=False)
                return True
            except Exception:
                return False

        results = await asyncio.gather(*(_refresh_one(u, p) for u, p in targets))
        return sum(1 for ok in results if ok)
