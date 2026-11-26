from __future__ import annotations

import re
import time
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from ..config import Settings
from ..crypto import decode_jwt_claims
from ..database import StoredCredential
from ..schemas import ExtraUsageOut, ProviderId, ProviderUsageOut, UsageWindowOut, WindowKind
from ..token_manager import RefreshedTokens, TokenRefreshError
from .base import OAuthExchangeError, UpstreamUnauthorizedError, clamp_pct, to_epoch

CLAUDE_AUTHORIZE_URL = "https://claude.ai/oauth/authorize"
CLAUDE_TOKEN_URL = "https://console.anthropic.com/v1/oauth/token"
CLAUDE_REDIRECT_URI = "https://console.anthropic.com/oauth/code/callback"
CLAUDE_SCOPES = "user:profile user:inference"
CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
CLAUDE_MESSAGES_URL = "https://api.anthropic.com/v1/messages"
CLAUDE_OAUTH_BETA = "oauth-2025-04-20"
CLAUDE_PROBE_MODEL = "claude-haiku-4-5-20251001"

UTIL_RE = re.compile(r"^anthropic-ratelimit-unified-(.+)-utilization$", re.I)

CLAUDE_SLUG_META: dict[str, tuple[str, WindowKind | None, str | None]] = {
    "5h": ("Session (5h)", "session", "5h"),
    "five_hour": ("Session (5h)", "session", "5h"),
    "7d": ("Weekly (7d)", "weekly", "7d"),
    "7d-oauth": ("Weekly (7d)", "weekly", "7d"),
    "seven_day": ("Weekly (7d)", "weekly", "7d"),
    "7d-opus": ("Opus (7d)", "opus", "7d"),
    "seven_day_opus": ("Opus (7d)", "opus", "7d"),
    "7d-fable": ("Fable (7d)", "fable", "7d"),
    "seven_day_fable": ("Fable (7d)", "fable", "7d"),
    "7d-sonnet": ("Sonnet (7d)", "sonnet", "7d"),
    "seven_day_sonnet": ("Sonnet (7d)", "sonnet", "7d"),
}


def build_claude_authorize_url(
    *, client_id: str, code_challenge: str, oauth_state: str
) -> str:
    params = {
        "code": "true",
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": CLAUDE_REDIRECT_URI,
        "scope": CLAUDE_SCOPES,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "state": oauth_state,
    }
    return f"{CLAUDE_AUTHORIZE_URL}?{urlencode(params)}"


def parse_claude_code_input(raw_input: str, expected_state: str) -> tuple[str, str]:
    cleaned = raw_input.strip()
    if cleaned.startswith("http://") or cleaned.startswith("https://"):
        parsed = urlparse(cleaned)
        qs = parse_qs(parsed.query)
        code = (qs.get("code") or [""])[0].strip()
        state = (qs.get("state") or [expected_state])[0].strip()
        if not code and parsed.fragment:
            code = cleaned.split("#", 1)[0]
        if not code:
            raise OAuthExchangeError("找不到網址內的 code 參數")
        return code, state

    if "#" in cleaned:
        code_part, state_part = cleaned.split("#", 1)
        return code_part.strip(), state_part.strip() or expected_state

    return cleaned, expected_state


def _extract_claude_label(payload: dict[str, Any], access_token: str) -> str | None:
    account = payload.get("account")
    if isinstance(account, dict):
        email = account.get("email_address") or account.get("email")
        if isinstance(email, str) and email.strip():
            return email.strip()
    claims = decode_jwt_claims(access_token)
    email = claims.get("email")
    return str(email).strip() if isinstance(email, str) and email.strip() else "Claude OAuth"


class ClaudeAdapter:
    provider_id: ProviderId = "claude"

    async def exchange_code(
        self,
        *,
        code_input: str,
        code_verifier: str,
        expected_state: str,
        client: httpx.AsyncClient,
        settings: Settings,
    ) -> RefreshedTokens:
        code, state = parse_claude_code_input(code_input, expected_state)
        resp = await client.post(
            CLAUDE_TOKEN_URL,
            json={
                "grant_type": "authorization_code",
                "code": code,
                "state": state,
                "client_id": settings.claude_client_id,
                "redirect_uri": CLAUDE_REDIRECT_URI,
                "code_verifier": code_verifier,
            },
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        if resp.status_code >= 400:
            detail = resp.text[:200]
            raise OAuthExchangeError(f"Claude 授權碼交換失敗 (HTTP {resp.status_code}): {detail}")

        data = resp.json()
        access_token = data.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise OAuthExchangeError("Claude 回應缺少 access_token")

        expires_in = int(data.get("expires_in") or 28800)
        return RefreshedTokens(
            access_token=access_token,
            refresh_token=data.get("refresh_token"),
            account_label=_extract_claude_label(data, access_token),
            expires_at=int(time.time()) + expires_in,
        )

    async def refresh_credential(
        self, cred: StoredCredential, client: httpx.AsyncClient, settings: Settings
    ) -> RefreshedTokens:
        if not cred.refresh_token:
            raise TokenRefreshError("缺少 Claude refresh_token", terminal=True)

        resp = await client.post(
            CLAUDE_TOKEN_URL,
            json={
                "grant_type": "refresh_token",
                "refresh_token": cred.refresh_token,
                "client_id": settings.claude_client_id,
            },
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        if resp.status_code in (400, 401, 403):
            raise TokenRefreshError(
                f"Claude refresh_token 已失效或過期 (HTTP {resp.status_code})",
                terminal=True,
            )
        if resp.status_code >= 400:
            raise TokenRefreshError(
                f"Claude Token 續期暫時失敗 (HTTP {resp.status_code})",
                terminal=False,
            )

        data = resp.json()
        access_token = data.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise TokenRefreshError("Claude 續期回應缺少 access_token", terminal=False)

        expires_in = int(data.get("expires_in") or 28800)
        return RefreshedTokens(
            access_token=access_token,
            refresh_token=data.get("refresh_token") or cred.refresh_token,
            account_label=_extract_claude_label(data, access_token) or cred.account_label,
            expires_at=int(time.time()) + expires_in,
        )

    async def fetch_usage(
        self, cred: StoredCredential, client: httpx.AsyncClient
    ) -> ProviderUsageOut:
        headers = {
            "Authorization": f"Bearer {cred.access_token}",
            "anthropic-beta": CLAUDE_OAUTH_BETA,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "ai-usage-web/2.0",
        }
        resp = await client.get(CLAUDE_USAGE_URL, headers=headers)
        if resp.status_code == 401:
            raise UpstreamUnauthorizedError("Claude access_token unauthorized")

        if resp.status_code == 200:
            parsed = parse_claude_oauth_usage_json(resp.json(), cred.account_label)
            if parsed is not None:
                return parsed

        if resp.status_code in (404, 405):
            return await self._fetch_via_probe_headers(cred, client)

        return ProviderUsageOut(
            id="claude",
            name="Claude",
            ok=False,
            connected=True,
            error=f"端點回應 HTTP {resp.status_code}",
            code="http",
            arg=resp.status_code,
            source="oauth",
            account_label=cred.account_label,
        )

    async def _fetch_via_probe_headers(
        self, cred: StoredCredential, client: httpx.AsyncClient
    ) -> ProviderUsageOut:
        resp = await client.post(
            CLAUDE_MESSAGES_URL,
            headers={
                "Authorization": f"Bearer {cred.access_token}",
                "anthropic-version": "2023-06-01",
                "anthropic-beta": CLAUDE_OAUTH_BETA,
                "Content-Type": "application/json",
                "User-Agent": "claude-code/2.1.5",
            },
            json={
                "model": CLAUDE_PROBE_MODEL,
                "max_tokens": 1,
                "messages": [{"role": "user", "content": "."}],
            },
        )
        if resp.status_code == 401:
            raise UpstreamUnauthorizedError("Claude probe unauthorized")

        lower = {k.lower(): v for k, v in resp.headers.items()}
        windows: list[UsageWindowOut] = []
        for key, value in lower.items():
            m = UTIL_RE.match(key)
            if not m:
                continue
            slug = m.group(1).lower()
            try:
                pct = clamp_pct(float(value) * 100.0)
            except (TypeError, ValueError):
                continue
            label, kind, win = CLAUDE_SLUG_META.get(slug, (slug.upper(), None, None))
            windows.append(
                UsageWindowOut(
                    label=label,
                    kind=kind,
                    win=win,
                    percent=pct,
                    reset_epoch=to_epoch(lower.get(f"anthropic-ratelimit-unified-{slug}-reset")),
                )
            )

        if not windows:
            return ProviderUsageOut(
                id="claude",
                name="Claude",
                ok=False,
                connected=True,
                error=f"回應沒有用量 header (HTTP {resp.status_code})",
                code="no_headers",
                arg=resp.status_code,
                source="oauth",
                account_label=cred.account_label,
            )

        return ProviderUsageOut(
            id="claude",
            name="Claude",
            ok=True,
            connected=True,
            source="oauth",
            account_label=cred.account_label,
            windows=windows,
        )


def parse_claude_oauth_usage_json(
    data: Any, account_label: str | None = None
) -> ProviderUsageOut | None:
    if not isinstance(data, dict):
        return None

    windows: list[UsageWindowOut] = []

    limits = data.get("limits")
    if isinstance(limits, list) and limits:
        for item in limits:
            if not isinstance(item, dict):
                continue
            raw_pct = item.get("percent", item.get("utilization"))
            if not isinstance(raw_pct, (int, float)):
                continue
            item_kind = item.get("kind")
            resets_at = to_epoch(item.get("resets_at"))
            if item_kind == "session":
                windows.append(
                    UsageWindowOut(
                        label="Session (5h)",
                        kind="session",
                        win="5h",
                        percent=clamp_pct(raw_pct),
                        reset_epoch=resets_at,
                    )
                )
            elif item_kind == "weekly_all":
                windows.append(
                    UsageWindowOut(
                        label="Weekly (7d)",
                        kind="weekly",
                        win="7d",
                        percent=clamp_pct(raw_pct),
                        reset_epoch=resets_at,
                    )
                )
            elif item_kind == "weekly_scoped":
                model_name = (
                    ((item.get("scope") or {}).get("model") or {}).get("display_name") or "Scoped"
                )
                lower_model = str(model_name).lower()
                mapped_kind: WindowKind | None = (
                    lower_model  # type: ignore[assignment]
                    if lower_model in {"opus", "fable", "sonnet"}
                    else "model"
                )
                windows.append(
                    UsageWindowOut(
                        label=f"{model_name} (7d)",
                        kind=mapped_kind,
                        win="7d",
                        percent=clamp_pct(raw_pct),
                        reset_epoch=resets_at,
                    )
                )
    else:
        for key in ("five_hour", "seven_day", "seven_day_opus", "seven_day_fable", "seven_day_sonnet"):
            bucket = data.get(key)
            if not isinstance(bucket, dict):
                continue
            raw_util = bucket.get("utilization")
            if not isinstance(raw_util, (int, float)):
                continue
            label, kind, win = CLAUDE_SLUG_META[key]
            windows.append(
                UsageWindowOut(
                    label=label,
                    kind=kind,
                    win=win,
                    percent=clamp_pct(raw_util),
                    reset_epoch=to_epoch(bucket.get("resets_at")),
                )
            )

    if not windows:
        return None

    extra_out: ExtraUsageOut | None = None
    extra = data.get("extra_usage")
    if isinstance(extra, dict) and extra.get("is_enabled") is True:
        decimals = int(extra.get("decimal_places") or 2)
        scale = 10**decimals
        used_minor = float(extra.get("used_credits") or 0.0)
        limit_minor = float(extra.get("monthly_limit") or 0.0)
        pct_val = extra.get("utilization")
        if not isinstance(pct_val, (int, float)) and limit_minor > 0:
            pct_val = (used_minor / limit_minor) * 100.0
        extra_out = ExtraUsageOut(
            enabled=True,
            used_amount=round(used_minor / scale, 2),
            limit_amount=round(limit_minor / scale, 2),
            currency=str(extra.get("currency") or "USD"),
            percent=clamp_pct(pct_val) if isinstance(pct_val, (int, float)) else None,
        )

    return ProviderUsageOut(
        id="claude",
        name="Claude",
        ok=True,
        connected=True,
        source="oauth",
        account_label=account_label,
        windows=windows,
        extra_usage=extra_out,
    )
