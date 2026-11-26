from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import httpx

from ..config import Settings
from ..crypto import decode_jwt_claims
from ..database import StoredCredential
from ..schemas import ProviderId, ProviderUsageOut, UsageWindowOut, WindowKind
from ..token_manager import RefreshedTokens, TokenRefreshError
from .base import OAuthExchangeError, UpstreamUnauthorizedError, clamp_pct, fmt_window, to_epoch

CODEX_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
OPENAI_AUTH_CLAIM = "https://api.openai.com/auth"
OPENAI_PROFILE_CLAIM = "https://api.openai.com/profile"
TERMINAL_REFRESH_CODES = {
    "refresh_token_expired",
    "refresh_token_reused",
    "refresh_token_invalidated",
    "invalid_grant",
}


@dataclass
class CodexDeviceStart:
    verification_url: str
    user_code: str
    device_auth_id: str
    interval_seconds: int


def _extract_codex_claims(access_token: str, id_token: str | None) -> tuple[str | None, str | None]:
    access_claims = decode_jwt_claims(access_token)
    id_claims = decode_jwt_claims(id_token)
    auth_obj = access_claims.get(OPENAI_AUTH_CLAIM) or id_claims.get(OPENAI_AUTH_CLAIM) or {}
    profile_obj = access_claims.get(OPENAI_PROFILE_CLAIM) or id_claims
    account_id = auth_obj.get("chatgpt_account_id") if isinstance(auth_obj, dict) else None
    email = None
    if isinstance(profile_obj, dict):
        email = profile_obj.get("email")
    if not email and isinstance(id_claims.get("email"), str):
        email = id_claims["email"]
    return (
        str(account_id) if account_id else None,
        str(email) if email else "ChatGPT OAuth",
    )


def _extract_token_expiry(access_token: str, expires_in: Any) -> int:
    claims = decode_jwt_claims(access_token)
    exp = claims.get("exp")
    if isinstance(exp, (int, float)) and exp > time.time():
        return int(exp)
    sec = int(expires_in) if isinstance(expires_in, (int, float)) and expires_in > 0 else 3600
    return int(time.time()) + sec


def codex_window_name(seconds: int | float | None, fallback: str) -> str:
    if not seconds:
        return fallback
    if seconds < 86400:
        return "Session"
    if seconds <= 7 * 86400:
        return "Weekly"
    if seconds <= 31 * 86400:
        return "Monthly"
    return fallback


def parse_codex_window(
    raw: Any, name: str, *, derive: bool = False
) -> UsageWindowOut | None:
    if not isinstance(raw, dict):
        return None
    pct = raw.get("used_percent", raw.get("usedPercent"))
    if not isinstance(pct, (int, float)):
        return None
    seconds = (
        raw.get("limit_window_seconds")
        or raw.get("windowSeconds")
        or (raw.get("window_minutes") or 0) * 60
    )
    if derive:
        name = codex_window_name(seconds, name)
    win = fmt_window(seconds)
    reset = to_epoch(raw.get("reset_at") or raw.get("resetAt"))
    if reset is None and isinstance(raw.get("reset_after_seconds"), (int, float)):
        reset = int(time.time() + float(raw["reset_after_seconds"]))
    lower = name.lower()
    kind: WindowKind | None = lower if lower in {"session", "weekly", "monthly"} else None  # type: ignore[assignment]
    return UsageWindowOut(
        label=f"{name} ({win})" if win else name,
        kind=kind,
        win=win,
        percent=clamp_pct(pct),
        reset_epoch=reset,
    )


class CodexAdapter:
    provider_id: ProviderId = "codex"

    async def start_device_code(
        self, client: httpx.AsyncClient, settings: Settings
    ) -> CodexDeviceStart:
        issuer = settings.codex_issuer
        resp = await client.post(
            f"{issuer}/api/accounts/deviceauth/usercode",
            json={"client_id": settings.codex_client_id},
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        if resp.status_code >= 400:
            raise OAuthExchangeError(
                f"無法啟動 ChatGPT 裝置碼登入 (HTTP {resp.status_code}): {resp.text[:200]}"
            )
        data = resp.json()
        user_code = data.get("user_code") or data.get("usercode")
        device_auth_id = data.get("device_auth_id")
        if not user_code or not device_auth_id:
            raise OAuthExchangeError("OpenAI 裝置碼回應格式不完整")
        raw_interval = data.get("interval", 5)
        try:
            interval = max(3, int(str(raw_interval).strip()))
        except ValueError:
            interval = 5
        return CodexDeviceStart(
            verification_url=f"{issuer}/codex/device",
            user_code=str(user_code),
            device_auth_id=str(device_auth_id),
            interval_seconds=interval,
        )

    async def poll_device_code(
        self,
        *,
        device_auth_id: str,
        user_code: str,
        client: httpx.AsyncClient,
        settings: Settings,
    ) -> RefreshedTokens | None:
        issuer = settings.codex_issuer
        poll_resp = await client.post(
            f"{issuer}/api/accounts/deviceauth/token",
            json={"device_auth_id": device_auth_id, "user_code": user_code},
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        if poll_resp.status_code in (403, 404):
            return None
        if poll_resp.status_code >= 400:
            raise OAuthExchangeError(
                f"ChatGPT 裝置碼驗證失敗 (HTTP {poll_resp.status_code}): {poll_resp.text[:200]}"
            )

        code_data = poll_resp.json()
        auth_code = code_data.get("authorization_code")
        code_verifier = code_data.get("code_verifier")
        if not auth_code or not code_verifier:
            raise OAuthExchangeError("ChatGPT 裝置碼回應缺少 authorization_code")

        token_resp = await client.post(
            f"{issuer}/oauth/token",
            data={
                "grant_type": "authorization_code",
                "client_id": settings.codex_client_id,
                "code": auth_code,
                "redirect_uri": f"{issuer}/deviceauth/callback",
                "code_verifier": code_verifier,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
        )
        if token_resp.status_code >= 400:
            raise OAuthExchangeError(
                f"ChatGPT Token 交換失敗 (HTTP {token_resp.status_code}): {token_resp.text[:200]}"
            )

        tok = token_resp.json()
        access_token = tok.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise OAuthExchangeError("ChatGPT 回應缺少 access_token")

        id_token = tok.get("id_token")
        account_id, email = _extract_codex_claims(access_token, id_token)
        return RefreshedTokens(
            access_token=access_token,
            refresh_token=tok.get("refresh_token"),
            id_token=id_token,
            account_id=account_id,
            account_label=email,
            expires_at=_extract_token_expiry(access_token, tok.get("expires_in")),
        )

    async def refresh_credential(
        self, cred: StoredCredential, client: httpx.AsyncClient, settings: Settings
    ) -> RefreshedTokens:
        if not cred.refresh_token:
            raise TokenRefreshError("缺少 ChatGPT refresh_token", terminal=True)

        issuer = settings.codex_issuer
        resp = await client.post(
            f"{issuer}/oauth/token",
            json={
                "grant_type": "refresh_token",
                "client_id": settings.codex_client_id,
                "refresh_token": cred.refresh_token,
            },
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        if resp.status_code >= 400:
            err_code = ""
            try:
                body = resp.json()
                err_obj = body.get("error")
                if isinstance(err_obj, dict):
                    err_code = str(err_obj.get("code") or "")
                elif isinstance(err_obj, str):
                    err_code = err_obj
            except Exception:
                pass
            terminal = resp.status_code in (401, 403) or err_code in TERMINAL_REFRESH_CODES
            raise TokenRefreshError(
                f"ChatGPT Token 續期失敗 ({err_code or f'HTTP {resp.status_code}'})",
                terminal=terminal,
            )

        data = resp.json()
        access_token = data.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise TokenRefreshError("ChatGPT 續期回應缺少 access_token", terminal=False)

        id_token = data.get("id_token") or cred.id_token
        account_id, email = _extract_codex_claims(access_token, id_token)
        return RefreshedTokens(
            access_token=access_token,
            refresh_token=data.get("refresh_token") or cred.refresh_token,
            id_token=id_token,
            account_id=account_id or cred.account_id,
            account_label=email or cred.account_label,
            expires_at=_extract_token_expiry(access_token, data.get("expires_in")),
        )

    async def fetch_usage(
        self, cred: StoredCredential, client: httpx.AsyncClient
    ) -> ProviderUsageOut:
        headers = {
            "Authorization": f"Bearer {cred.access_token}",
            "Accept": "application/json",
            "User-Agent": "ai-usage-web/2.0",
        }
        if cred.account_id:
            headers["chatgpt-account-id"] = cred.account_id

        resp = await client.get(CODEX_USAGE_URL, headers=headers)
        if resp.status_code == 401:
            raise UpstreamUnauthorizedError("Codex access_token unauthorized")
        if resp.status_code >= 400:
            return ProviderUsageOut(
                id="codex",
                name="Codex",
                ok=False,
                connected=True,
                error=f"端點回應 HTTP {resp.status_code}",
                code="http",
                arg=resp.status_code,
                source="oauth",
                account_label=cred.account_label,
            )

        try:
            data = resp.json()
        except Exception:
            return ProviderUsageOut(
                id="codex",
                name="Codex",
                ok=False,
                connected=True,
                error="回應不是合法 JSON",
                code="bad_json",
                source="oauth",
                account_label=cred.account_label,
            )

        rl = data.get("rate_limit") or {}
        windows = [
            w
            for w in (
                parse_codex_window(rl.get("primary_window"), "Session", derive=True),
                parse_codex_window(rl.get("secondary_window"), "Weekly", derive=True),
            )
            if w is not None
        ]
        for item in data.get("additional_rate_limits") or []:
            if not isinstance(item, dict):
                continue
            limit_name = item.get("limit_name") or item.get("metered_feature") or "Extra"
            sub = item.get("rate_limit") or {}
            extra_win = parse_codex_window(
                sub.get("secondary_window") or sub.get("primary_window"),
                str(limit_name).title(),
            )
            if extra_win is not None:
                windows.append(extra_win)

        if not windows:
            return ProviderUsageOut(
                id="codex",
                name="Codex",
                ok=False,
                connected=True,
                error="回應裡沒有用量資料",
                code="no_usage",
                source="oauth",
                account_label=cred.account_label,
            )

        plan = str((data.get("credits") or {}).get("plan") or data.get("plan_type") or "") or None
        return ProviderUsageOut(
            id="codex",
            name="Codex",
            ok=True,
            connected=True,
            source="oauth",
            plan=plan,
            account_label=cred.account_label,
            windows=windows,
        )
