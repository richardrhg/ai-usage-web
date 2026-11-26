from __future__ import annotations

import asyncio
import ipaddress
import socket
import time
from typing import Callable, Sequence
from urllib.parse import parse_qs, urlparse

import httpx

from .analytics import make_window_key
from .config import Settings
from .database import Database
from .schemas import (
    AlertSettingsPayload,
    AlertTestRequest,
    AlertTestResponse,
    ProviderId,
    ProviderUsageOut,
    WebhookChannel,
)

FORBIDDEN_HOSTNAMES = {
    "localhost",
    "localhost.localdomain",
    "ip6-localhost",
    "ip6-loopback",
    "metadata",
    "metadata.google.internal",
}

DnsResolver = Callable[[str, int], Sequence[str]]


class WebhookValidationError(ValueError):
    """Raised when a user-supplied webhook URL violates HTTPS or SSRF rules."""


def _is_disallowed_ip_address(raw_ip: str) -> bool:
    cleaned = raw_ip.strip().split("%", 1)[0]
    addr = ipaddress.ip_address(cleaned)
    if isinstance(addr, ipaddress.IPv6Address):
        if addr.ipv4_mapped is not None:
            addr = addr.ipv4_mapped
        elif addr.sixtofour is not None:
            addr = addr.sixtofour
        elif addr.teredo is not None:
            server_ip, client_ip = addr.teredo
            if _is_disallowed_ip_address(str(server_ip)) or _is_disallowed_ip_address(str(client_ip)):
                return True
    return bool(
        not addr.is_global
        or addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
    )


def _default_dns_resolver(hostname: str, port: int) -> list[str]:
    infos = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    ips: list[str] = []
    for info in infos:
        sockaddr = info[4]
        if sockaddr and isinstance(sockaddr[0], str):
            ips.append(sockaddr[0])
    return ips


def validate_webhook_url(
    url: str,
    *,
    allow_private_targets: bool = False,
    dns_resolver: DnsResolver | None = None,
    resolve_dns: bool = True,
) -> str:
    cleaned = url.strip()
    if not cleaned:
        return ""

    parsed = urlparse(cleaned)
    if parsed.scheme.lower() != "https":
        raise WebhookValidationError("Webhook 網址必須使用 https:// 加密協定")
    if parsed.username or parsed.password:
        raise WebhookValidationError("Webhook 網址不可包含內嵌帳號密碼")

    host = (parsed.hostname or "").strip().rstrip(".").lower()
    if not host:
        raise WebhookValidationError("Webhook 網址缺少有效的主機名稱")

    if allow_private_targets:
        return cleaned

    if (
        host in FORBIDDEN_HOSTNAMES
        or host.endswith(".localhost")
        or host.endswith(".local")
        or host.endswith(".internal")
    ):
        raise WebhookValidationError(f"禁止使用本機或內部主機名稱 ({host})")

    # Check direct IPv4 / IPv6 literals (including packed integer IPv4 notations)
    literal_checked = False
    try:
        ip_dead = _is_disallowed_ip_address(host)
        literal_checked = True
        if ip_dead:
            raise WebhookValidationError(f"禁止指向私有、迴路或鏈路本機 IP ({host})")
    except WebhookValidationError:
        raise
    except ValueError:
        # Check packed/octal/hex IPv4 literals accepted by inet_aton (e.g. 2130706433, 0x7f000001)
        if all(part.isdigit() or part.startswith("0x") for part in host.split(".")):
            try:
                packed = socket.inet_aton(host)
                canonical = socket.inet_ntoa(packed)
                literal_checked = True
                if _is_disallowed_ip_address(canonical):
                    raise WebhookValidationError(f"禁止指向私有或本機 IP ({canonical})")
            except WebhookValidationError:
                raise
            except OSError:
                pass

    if not literal_checked and resolve_dns:
        resolver = dns_resolver or _default_dns_resolver
        port = parsed.port or 443
        try:
            resolved_ips = list(resolver(host, port))
        except OSError as exc:
            raise WebhookValidationError(f"無法解析 Webhook 網域名稱 ({host}): {exc}") from exc
        if not resolved_ips:
            raise WebhookValidationError(f"Webhook 網域名稱未傳回任何 IP ({host})")
        for ip_text in resolved_ips:
            try:
                disallowed = _is_disallowed_ip_address(ip_text)
            except ValueError as exc:
                raise WebhookValidationError(f"無效的解析 IP ({ip_text})") from exc
            if disallowed:
                raise WebhookValidationError(
                    f"禁止 Webhook 網域解析至私有、本機或 Metadata IP ({ip_text})"
                )

    return cleaned


def validate_alert_settings_urls(
    payload: AlertSettingsPayload,
    *,
    allow_private_targets: bool = False,
    dns_resolver: DnsResolver | None = None,
) -> AlertSettingsPayload:
    discord_url = validate_webhook_url(
        payload.discord_webhook_url,
        allow_private_targets=allow_private_targets,
        dns_resolver=dns_resolver,
    )
    slack_url = validate_webhook_url(
        payload.slack_webhook_url,
        allow_private_targets=allow_private_targets,
        dns_resolver=dns_resolver,
    )
    telegram_url = validate_webhook_url(
        payload.telegram_webhook_url,
        allow_private_targets=allow_private_targets,
        dns_resolver=dns_resolver,
    )
    chat_id = payload.telegram_chat_id.strip()
    if telegram_url:
        parsed_tg = urlparse(telegram_url)
        qs_chat = (parse_qs(parsed_tg.query).get("chat_id") or [""])[0].strip()
        if (
            parsed_tg.hostname
            and parsed_tg.hostname.lower() == "api.telegram.org"
            and not chat_id
            and not qs_chat
        ):
            raise WebhookValidationError(
                "使用 api.telegram.org 時，請填寫 Telegram Chat ID 或於網址加上 ?chat_id=..."
            )

    return payload.model_copy(
        update={
            "discord_webhook_url": discord_url,
            "slack_webhook_url": slack_url,
            "telegram_webhook_url": telegram_url,
            "telegram_chat_id": chat_id,
        }
    )


class AlertManager:
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
        self.dns_resolver = dns_resolver

    def _has_any_channel(self, cfg: AlertSettingsPayload) -> bool:
        return bool(
            cfg.discord_webhook_url.strip()
            or cfg.slack_webhook_url.strip()
            or cfg.telegram_webhook_url.strip()
        )

    async def _validate_target_async(self, url: str) -> str:
        return await asyncio.to_thread(
            validate_webhook_url,
            url,
            allow_private_targets=self.settings.allow_private_webhook_targets,
            dns_resolver=self.dns_resolver,
            resolve_dns=True,
        )

    async def _post_channel(
        self,
        channel: WebhookChannel,
        url: str,
        message: str,
        *,
        telegram_chat_id: str = "",
    ) -> None:
        safe_url = await self._validate_target_async(url)
        if channel == "discord":
            body = {"content": message}
        elif channel == "slack":
            body = {"text": message}
        else:
            parsed = urlparse(safe_url)
            qs_chat = (parse_qs(parsed.query).get("chat_id") or [""])[0].strip()
            effective_chat_id = telegram_chat_id.strip() or qs_chat
            body = {"text": message}
            if effective_chat_id:
                body["chat_id"] = effective_chat_id

        resp = await self.http_client.post(
            safe_url,
            json=body,
            headers={"Content-Type": "application/json", "User-Agent": "ai-usage-web/2.0"},
            follow_redirects=False,
        )
        if 300 <= resp.status_code < 400:
            raise WebhookValidationError(f"禁止 Webhook 重新導向 (HTTP {resp.status_code})")
        if resp.status_code >= 400:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:160]}")

    async def dispatch_message(
        self,
        cfg: AlertSettingsPayload,
        message: str,
    ) -> tuple[list[WebhookChannel], dict[str, str]]:
        tasks: list[tuple[WebhookChannel, str]] = []
        if cfg.discord_webhook_url.strip():
            tasks.append(("discord", cfg.discord_webhook_url.strip()))
        if cfg.slack_webhook_url.strip():
            tasks.append(("slack", cfg.slack_webhook_url.strip()))
        if cfg.telegram_webhook_url.strip():
            tasks.append(("telegram", cfg.telegram_webhook_url.strip()))

        delivered: list[WebhookChannel] = []
        errors: dict[str, str] = {}

        async def _send_one(ch: WebhookChannel, target_url: str) -> None:
            try:
                await self._post_channel(
                    ch,
                    target_url,
                    message,
                    telegram_chat_id=cfg.telegram_chat_id,
                )
                delivered.append(ch)
            except Exception as exc:
                errors[ch] = str(exc)

        await asyncio.gather(*(_send_one(ch, u) for ch, u in tasks))
        delivered.sort(key=lambda c: ("discord", "slack", "telegram").index(c))
        return delivered, errors

    async def send_test_alert(
        self,
        user_id: int,
        override: AlertTestRequest | None = None,
    ) -> AlertTestResponse:
        stored = self.db.get_alert_settings(user_id)
        effective = AlertSettingsPayload(
            enabled=True,
            discord_webhook_url=(
                override.discord_webhook_url
                if override and override.discord_webhook_url is not None
                else stored.discord_webhook_url
            ),
            slack_webhook_url=(
                override.slack_webhook_url
                if override and override.slack_webhook_url is not None
                else stored.slack_webhook_url
            ),
            telegram_webhook_url=(
                override.telegram_webhook_url
                if override and override.telegram_webhook_url is not None
                else stored.telegram_webhook_url
            ),
            telegram_chat_id=(
                override.telegram_chat_id
                if override and override.telegram_chat_id is not None
                else stored.telegram_chat_id
            ),
            warn_threshold=stored.warn_threshold,
            critical_threshold=stored.critical_threshold,
            burn_rate_threshold_per_hour=stored.burn_rate_threshold_per_hour,
            notify_on_burn_rate=stored.notify_on_burn_rate,
            notify_on_reauth=stored.notify_on_reauth,
        )
        effective = await asyncio.to_thread(
            validate_alert_settings_urls,
            effective,
            allow_private_targets=self.settings.allow_private_webhook_targets,
            dns_resolver=self.dns_resolver,
        )
        if not self._has_any_channel(effective):
            raise WebhookValidationError("請至少設定一個 Discord、Slack 或 Telegram Webhook 網址")

        msg = "[AI 用量監測] 測試通知：Webhook 告警連線運作正常！"
        delivered, errors = await self.dispatch_message(effective, msg)
        return AlertTestResponse(
            ok=len(delivered) > 0 and len(errors) == 0,
            delivered_channels=delivered,
            errors=errors,
        )

    async def _emit_deduped_event(
        self,
        *,
        user_id: int,
        cfg: AlertSettingsPayload,
        provider: ProviderId,
        window_key: str,
        alert_level: str,
        resets_at: int,
        message: str,
        now: int,
    ) -> bool:
        event_id = self.db.claim_alert_event(
            user_id=user_id,
            provider=provider,
            window_key=window_key,
            alert_level=alert_level,
            resets_at=resets_at,
            message=message,
            created_at=now,
        )
        if event_id is None:
            return False

        delivered, _ = await self.dispatch_message(cfg, message)
        if not delivered:
            self.db.delete_alert_event(event_id)
            return False

        self.db.finalize_alert_event_channels(event_id, delivered)
        return True

    async def evaluate_usage_alerts(
        self,
        user_id: int,
        usage: ProviderUsageOut,
        *,
        now: int | None = None,
    ) -> int:
        if not usage.ok or not usage.windows:
            return 0
        cfg = self.db.get_alert_settings(user_id)
        if not cfg.enabled or not self._has_any_channel(cfg):
            return 0

        ts = now if now is not None else int(time.time())
        sent_count = 0
        acct_suffix = f" ({usage.account_label})" if usage.account_label else ""

        for win in usage.windows:
            wkey = make_window_key(win)
            resets_val = int(win.reset_epoch or 0)

            # (a) Threshold crossing: critical supersedes warn when crossed simultaneously
            if win.percent >= cfg.critical_threshold:
                msg = (
                    f"[AI 用量嚴重告警] {usage.name}{acct_suffix} · {win.label} "
                    f"已達 {win.percent:.1f}%（門檻 {cfg.critical_threshold:.0f}%）"
                )
                if await self._emit_deduped_event(
                    user_id=user_id,
                    cfg=cfg,
                    provider=usage.id,
                    window_key=wkey,
                    alert_level="critical",
                    resets_at=resets_val,
                    message=msg,
                    now=ts,
                ):
                    sent_count += 1
            elif win.percent >= cfg.warn_threshold:
                msg = (
                    f"[AI 用量警告] {usage.name}{acct_suffix} · {win.label} "
                    f"已達 {win.percent:.1f}%（門檻 {cfg.warn_threshold:.0f}%）"
                )
                if await self._emit_deduped_event(
                    user_id=user_id,
                    cfg=cfg,
                    provider=usage.id,
                    window_key=wkey,
                    alert_level="warn",
                    resets_at=resets_val,
                    message=msg,
                    now=ts,
                ):
                    sent_count += 1

            # (b) Short-term burn-rate spike / projected exhaustion before reset
            rate = win.burn_rate_per_hour or 0.0
            spike = rate >= cfg.burn_rate_threshold_per_hour
            if cfg.notify_on_burn_rate and (win.will_exhaust_before_reset or spike):
                eta_part = ""
                if win.projected_exhaustion_at is not None and win.projected_exhaustion_at > ts:
                    rem_h = max(0.1, (win.projected_exhaustion_at - ts) / 3600.0)
                    eta_part = f"，預計約 {rem_h:.1f} 小時後耗盡"
                msg = (
                    f"[AI 燃燒率預警] {usage.name}{acct_suffix} · {win.label} "
                    f"目前 {win.percent:.1f}%，消耗速率 {rate:.1f}%/hr{eta_part}"
                )
                if await self._emit_deduped_event(
                    user_id=user_id,
                    cfg=cfg,
                    provider=usage.id,
                    window_key=wkey,
                    alert_level="burn_rate",
                    resets_at=resets_val,
                    message=msg,
                    now=ts,
                ):
                    sent_count += 1

        return sent_count

    async def notify_reauth_needed(
        self,
        user_id: int,
        provider: ProviderId,
        provider_name: str,
        *,
        account_label: str | None = None,
        credential_epoch: int = 0,
        reason: str = "",
        now: int | None = None,
    ) -> bool:
        cfg = self.db.get_alert_settings(user_id)
        if not cfg.enabled or not cfg.notify_on_reauth or not self._has_any_channel(cfg):
            return False

        ts = now if now is not None else int(time.time())
        acct_suffix = f" ({account_label})" if account_label else ""
        detail_suffix = f"：{reason}" if reason else ""
        msg = (
            f"[AI 憑證失效通知] {provider_name}{acct_suffix} OAuth refresh_token 續期失敗，"
            f"請登入儀表板重新授權連結帳號{detail_suffix}"
        )
        return await self._emit_deduped_event(
            user_id=user_id,
            cfg=cfg,
            provider=provider,
            window_key="oauth_token",
            alert_level="needs_reauth",
            resets_at=int(credential_epoch or 0),
            message=msg,
            now=ts,
        )
