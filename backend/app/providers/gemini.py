from __future__ import annotations

import time
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from ..config import Settings
from ..crypto import decode_jwt_claims
from ..database import StoredCredential
from ..schemas import ProviderId, ProviderUsageOut, UsageWindowOut
from ..token_manager import RefreshedTokens, TokenRefreshError
from .base import OAuthExchangeError, UpstreamUnauthorizedError, clamp_pct, to_epoch

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v1/userinfo?alt=json"
GEMINI_REDIRECT_URI = "http://localhost:8085/oauth2callback"
GEMINI_CODE_ASSIST_BASE = "https://cloudcode-pa.googleapis.com/v1internal"
GEMINI_SCOPES = (
    "https://www.googleapis.com/auth/cloud-platform "
    "https://www.googleapis.com/auth/userinfo.email "
    "https://www.googleapis.com/auth/userinfo.profile"
)


def build_gemini_authorize_url(
    *, client_id: str, code_challenge: str, oauth_state: str
) -> str:
    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": GEMINI_REDIRECT_URI,
        "scope": GEMINI_SCOPES,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "state": oauth_state,
        "access_type": "offline",
        "prompt": "consent",
    }
    return f"{GOOGLE_AUTH_URL}?{urlencode(params)}"


def parse_gemini_callback_input(raw_input: str, expected_state: str) -> str:
    cleaned = raw_input.strip()
    if "://" in cleaned or cleaned.startswith("localhost:") or "?" in cleaned:
        normalized = cleaned if "://" in cleaned else f"http://{cleaned}"
        parsed = urlparse(normalized)
        qs = parse_qs(parsed.query)
        err = (qs.get("error") or [""])[0].strip()
        if err:
            raise OAuthExchangeError(f"Google OAuth 回傳錯誤: {err}")
        state = (qs.get("state") or [""])[0].strip()
        if state and state != expected_state:
            raise OAuthExchangeError("OAuth state 驗證不符，請重新發起連結")
        code = (qs.get("code") or [""])[0].strip()
        if not code:
            raise OAuthExchangeError("貼上的網址中找不到 code 參數")
        return code
    return cleaned


def _format_gemini_tier(assist_body: dict[str, Any], id_token: str | None) -> str | None:
    paid = assist_body.get("paidTier")
    curr = assist_body.get("currentTier")
    raw = (
        (paid.get("name") if isinstance(paid, dict) else None)
        or (curr.get("name") if isinstance(curr, dict) else None)
        or (curr.get("id") if isinstance(curr, dict) else None)
    )
    if not isinstance(raw, str) or not raw.strip():
        return None
    lower = raw.strip().lower()
    if lower == "standard-tier":
        return "Paid"
    if lower == "legacy-tier":
        return "Legacy"
    if "free" in lower:
        claims = decode_jwt_claims(id_token)
        return "Workspace" if claims.get("hd") else "Free"
    return raw.strip()


def _is_retired_gemini_tier(body: Any) -> bool:
    if not isinstance(body, dict):
        return False
    err = body.get("error")
    if not isinstance(err, dict):
        return False
    status = str(err.get("status") or "")
    msg = str(err.get("message") or "")
    return status == "UNSUPPORTED_CLIENT" or "IneligibleTierError" in msg


def parse_gemini_quota_buckets(body: Any) -> list[UsageWindowOut]:
    if not isinstance(body, dict):
        return []
    buckets = body.get("buckets")
    if not isinstance(buckets, list):
        return []

    grouped: dict[str, UsageWindowOut] = {}
    for item in buckets:
        if not isinstance(item, dict):
            continue
        model_id = item.get("modelId")
        rem = item.get("remainingFraction")
        if not isinstance(model_id, str) or not model_id or not isinstance(rem, (int, float)):
            continue
        used_pct = clamp_pct((1.0 - max(0.0, min(1.0, float(rem)))) * 100.0)
        reset_epoch = to_epoch(item.get("resetTime"))
        short_label = model_id.removeprefix("models/")
        prev = grouped.get(short_label)
        if prev is None or used_pct > prev.percent:
            grouped[short_label] = UsageWindowOut(
                label=short_label,
                kind="model",
                win="24h",
                percent=used_pct,
                reset_epoch=reset_epoch,
            )

    ordered = sorted(grouped.values(), key=lambda w: (-w.percent, w.label))
    return ordered[:6]


class GeminiAdapter:
    provider_id: ProviderId = "gemini"

    async def exchange_code(
        self,
        *,
        code_input: str,
        code_verifier: str,
        expected_state: str,
        client: httpx.AsyncClient,
        settings: Settings,
    ) -> RefreshedTokens:
        code = parse_gemini_callback_input(code_input, expected_state)
        resp = await client.post(
            GOOGLE_TOKEN_URL,
            data={
                "client_id": settings.gemini_client_id,
                "client_secret": settings.gemini_client_secret,
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": GEMINI_REDIRECT_URI,
                "code_verifier": code_verifier,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
        )
        if resp.status_code >= 400:
            raise OAuthExchangeError(
                f"Gemini Token 交換失敗 (HTTP {resp.status_code}): {resp.text[:200]}"
            )

        tok = resp.json()
        access_token = tok.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise OAuthExchangeError("Gemini 回應缺少 access_token")

        email: str | None = None
        try:
            u_resp = await client.get(
                GOOGLE_USERINFO_URL,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if u_resp.status_code == 200:
                email = u_resp.json().get("email")
        except Exception:
            pass

        project_id, _ = await self._load_code_assist(access_token, client, ignore_errors=True)
        expires_in = int(tok.get("expires_in") or 3600)
        return RefreshedTokens(
            access_token=access_token,
            refresh_token=tok.get("refresh_token"),
            id_token=tok.get("id_token"),
            project_id=project_id,
            account_label=email or "Google Gemini",
            expires_at=int(time.time()) + expires_in,
        )

    async def refresh_credential(
        self, cred: StoredCredential, client: httpx.AsyncClient, settings: Settings
    ) -> RefreshedTokens:
        if not cred.refresh_token:
            raise TokenRefreshError("缺少 Gemini refresh_token", terminal=True)

        resp = await client.post(
            GOOGLE_TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": cred.refresh_token,
                "client_id": settings.gemini_client_id,
                "client_secret": settings.gemini_client_secret,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
        )
        if resp.status_code in (400, 401, 403):
            terminal = "invalid_grant" in resp.text or resp.status_code in (401, 403)
            raise TokenRefreshError(
                f"Gemini refresh_token 已失效 (HTTP {resp.status_code})",
                terminal=terminal,
            )
        if resp.status_code >= 400:
            raise TokenRefreshError(
                f"Gemini Token 續期失敗 (HTTP {resp.status_code})",
                terminal=False,
            )

        data = resp.json()
        access_token = data.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise TokenRefreshError("Gemini 續期回應缺少 access_token", terminal=False)

        expires_in = int(data.get("expires_in") or 3600)
        return RefreshedTokens(
            access_token=access_token,
            refresh_token=data.get("refresh_token") or cred.refresh_token,
            id_token=data.get("id_token") or cred.id_token,
            project_id=cred.project_id,
            account_label=cred.account_label,
            expires_at=int(time.time()) + expires_in,
        )

    async def _load_code_assist(
        self,
        access_token: str,
        client: httpx.AsyncClient,
        *,
        ignore_errors: bool = False,
    ) -> tuple[str | None, dict[str, Any]]:
        resp = await client.post(
            f"{GEMINI_CODE_ASSIST_BASE}:loadCodeAssist",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "ai-usage-web/2.0",
            },
            json={"metadata": {"ideType": "GEMINI_CLI", "pluginType": "GEMINI"}},
        )
        if resp.status_code == 401 and not ignore_errors:
            raise UpstreamUnauthorizedError("Gemini access_token unauthorized")

        try:
            body = resp.json()
        except Exception:
            body = {}

        proj = body.get("cloudaicompanionProject") if isinstance(body, dict) else None
        if isinstance(proj, dict):
            proj = proj.get("id")
        project_id = str(proj).strip() if isinstance(proj, str) and proj.strip() else None
        return project_id, body if isinstance(body, dict) else {}

    async def fetch_usage(
        self, cred: StoredCredential, client: httpx.AsyncClient
    ) -> ProviderUsageOut:
        project_id, assist_body = await self._load_code_assist(cred.access_token, client)
        if _is_retired_gemini_tier(assist_body):
            return ProviderUsageOut(
                id="gemini",
                name="Gemini",
                ok=False,
                connected=True,
                error="Google 已限制此個人方案層級使用 Gemini CLI OAuth (IneligibleTierError)",
                code="ineligible_gemini",
                source="oauth",
                account_label=cred.account_label,
            )

        effective_project = project_id or cred.project_id
        payload: dict[str, Any] = {"project": effective_project} if effective_project else {}
        resp = await client.post(
            f"{GEMINI_CODE_ASSIST_BASE}:retrieveUserQuota",
            headers={
                "Authorization": f"Bearer {cred.access_token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "ai-usage-web/2.0",
            },
            json=payload,
        )
        if resp.status_code == 401:
            raise UpstreamUnauthorizedError("Gemini retrieveUserQuota unauthorized")
        if resp.status_code >= 400:
            try:
                err_body = resp.json()
            except Exception:
                err_body = {}
            if _is_retired_gemini_tier(err_body):
                return ProviderUsageOut(
                    id="gemini",
                    name="Gemini",
                    ok=False,
                    connected=True,
                    error="Google 已限制此帳號層級查詢 Gemini CLI 配額",
                    code="ineligible_gemini",
                    source="oauth",
                    account_label=cred.account_label,
                )
            return ProviderUsageOut(
                id="gemini",
                name="Gemini",
                ok=False,
                connected=True,
                error=f"端點回應 HTTP {resp.status_code}",
                code="http",
                arg=resp.status_code,
                source="oauth",
                account_label=cred.account_label,
            )

        windows = parse_gemini_quota_buckets(resp.json())
        if not windows:
            return ProviderUsageOut(
                id="gemini",
                name="Gemini",
                ok=False,
                connected=True,
                error="回應裡沒有用量資料",
                code="no_usage",
                source="oauth",
                account_label=cred.account_label,
            )

        return ProviderUsageOut(
            id="gemini",
            name="Gemini",
            ok=True,
            connected=True,
            source="oauth",
            plan=_format_gemini_tier(assist_body, cred.id_token),
            account_label=cred.account_label,
            windows=windows,
        )
