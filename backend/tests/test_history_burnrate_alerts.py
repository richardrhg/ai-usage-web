from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Sequence

import httpx
import pytest

from app.alerts import WebhookValidationError, validate_webhook_url
from app.analytics import (
    HistorySample,
    compute_burn_rate_projection,
    downsample_trend_points,
)
from app.config import Settings
from app.crypto import CredentialCipher
from app.database import Database
from app.main import create_app
from app.schemas import ProviderUsageOut, UsageWindowOut


def _public_dns_resolver(host: str, port: int) -> Sequence[str]:
    if "internal" in host or "rebind" in host:
        return ["10.24.0.15"]
    if "metadata-alias" in host:
        return ["169.254.169.254"]
    return ["162.159.135.232"]


def test_history_spacing_pruning_and_24h_downsampling(tmp_path: Path) -> None:
    db = Database(tmp_path / "hist.db", CredentialCipher("hist-secret-key"))
    user = db.create_user("alice", "hash")
    uid = user["id"]
    now = 1_795_700_000
    reset_at = now + 18_000

    # Insert an ancient >30d row directly, then verify record_usage_history prunes it
    stale_usage = ProviderUsageOut(
        id="claude",
        name="Claude",
        ok=True,
        windows=[UsageWindowOut(label="Session (5h)", kind="session", win="5h", percent=10.0, reset_epoch=now - 40 * 86400)],
    )
    db.record_usage_history(uid, stale_usage, now - 35 * 86400, min_interval_seconds=60, retention_days=30)

    # Record 48 samples 30 minutes apart across 24 hours
    for step in range(48):
        ts = (now - 47 * 1800) + step * 1800
        pct = min(96.0, 4.0 + step * 1.8)
        sample_usage = ProviderUsageOut(
            id="claude",
            name="Claude",
            ok=True,
            windows=[
                UsageWindowOut(
                    label="Session (5h)",
                    kind="session",
                    win="5h",
                    percent=pct,
                    reset_epoch=reset_at,
                )
            ],
        )
        inserted = db.record_usage_history(
            uid,
            sample_usage,
            ts,
            min_interval_seconds=60,
            retention_days=30,
        )
        assert inserted == 1

    # Rapid duplicate read 15 seconds later (< 60s min_interval_seconds) must NOT bloat DB
    rapid_dup = db.record_usage_history(
        uid,
        sample_usage,
        now + 15,
        min_interval_seconds=60,
        retention_days=30,
    )
    assert rapid_dup == 0

    # Verify ancient >30d row was pruned
    with sqlite3.connect(str(tmp_path / "hist.db")) as raw:
        oldest = raw.execute("SELECT MIN(recorded_at), COUNT(*) FROM usage_history").fetchone()
        assert oldest[0] >= now - 30 * 86400
        assert oldest[1] == 48

    grouped = db.list_user_history_since(uid, now - 86400)
    claude_samples = grouped["claude"]["session:5h"]
    assert len(claude_samples) == 48

    downsampled = downsample_trend_points(claude_samples, max_points=24)
    assert len(downsampled) == 24
    assert downsampled[0].t == claude_samples[0].recorded_at
    assert downsampled[-1].t == claude_samples[-1].recorded_at
    assert downsampled[-1].v == round(claude_samples[-1].percent, 1)


def test_burn_rate_math_and_reset_cycle_isolation() -> None:
    now = 1_795_700_000
    reset_in_3h = now + 3 * 3600

    # 1. Steep linear burn: 20% -> 40% -> 60% over 1 hour => 40%/hr => exhausts in 1h (< 3h reset)
    steep_samples = [
        HistorySample(recorded_at=now - 3600, percent=20.0, reset_epoch=reset_in_3h),
        HistorySample(recorded_at=now - 1800, percent=40.0, reset_epoch=reset_in_3h),
        HistorySample(recorded_at=now, percent=60.0, reset_epoch=reset_in_3h),
    ]
    proj_steep = compute_burn_rate_projection(
        steep_samples,
        current_percent=60.0,
        reset_epoch=reset_in_3h,
        now=now,
    )
    assert proj_steep.burn_rate_per_hour == pytest.approx(40.0, rel=1e-2)
    assert proj_steep.projected_exhaustion_at == pytest.approx(now + 3600, abs=5)
    assert proj_steep.will_exhaust_before_reset is True

    # 2. Gentle burn: 10% -> 14% over 1 hour => 4%/hr => exhausts in 21.5h (> 3h reset => SAFE)
    gentle_samples = [
        HistorySample(recorded_at=now - 3600, percent=10.0, reset_epoch=reset_in_3h),
        HistorySample(recorded_at=now, percent=14.0, reset_epoch=reset_in_3h),
    ]
    proj_safe = compute_burn_rate_projection(
        gentle_samples,
        current_percent=14.0,
        reset_epoch=reset_in_3h,
        now=now,
    )
    assert proj_safe.burn_rate_per_hour == pytest.approx(4.0, rel=1e-2)
    assert proj_safe.will_exhaust_before_reset is False

    # 3. Quota reset mid-series (88% -> 4% -> 14% over last 30m => 20%/hr post-reset, ignoring 88%)
    reset_series = [
        HistorySample(recorded_at=now - 3600, percent=88.0, reset_epoch=reset_in_3h),
        HistorySample(recorded_at=now - 1800, percent=4.0, reset_epoch=reset_in_3h),
        HistorySample(recorded_at=now, percent=14.0, reset_epoch=reset_in_3h),
    ]
    proj_after_reset = compute_burn_rate_projection(
        reset_series,
        current_percent=14.0,
        reset_epoch=reset_in_3h,
        now=now,
    )
    assert proj_after_reset.burn_rate_per_hour == pytest.approx(20.0, rel=1e-2)


def test_ssrf_webhook_validation_blocks_insecure_and_internal_targets() -> None:
    # Non-HTTPS rejected
    with pytest.raises(WebhookValidationError):
        validate_webhook_url("http://discord.com/api/webhooks/123/abc", dns_resolver=_public_dns_resolver)

    # Localhost & internal hostnames rejected
    for forbidden in (
        "https://localhost/webhook",
        "https://api.localhost/webhook",
        "https://nas.local/webhook",
        "https://metadata.google.internal/computeMetadata/v1/",
    ):
        with pytest.raises(WebhookValidationError):
            validate_webhook_url(forbidden, dns_resolver=_public_dns_resolver)

    # Loopback, link-local IMDS, RFC1918 private IPv4/IPv6 literals & IPv4-mapped IPv6 rejected
    for bad_ip_url in (
        "https://127.0.0.1/hook",
        "https://[::1]/hook",
        "https://[::ffff:127.0.0.1]/hook",
        "https://169.254.169.254/latest/meta-data/",
        "https://10.0.0.8/hook",
        "https://172.16.12.4/hook",
        "https://192.168.1.20/hook",
        "https://0.0.0.0/hook",
    ):
        with pytest.raises(WebhookValidationError):
            validate_webhook_url(bad_ip_url, dns_resolver=_public_dns_resolver)

    # Domain resolving to RFC1918 or 169.254.169.254 via DNS rejected
    with pytest.raises(WebhookValidationError):
        validate_webhook_url("https://rebind.example.com/hook", dns_resolver=_public_dns_resolver)
    with pytest.raises(WebhookValidationError):
        validate_webhook_url("https://metadata-alias.example.com/hook", dns_resolver=_public_dns_resolver)

    # Explicit unit-test override permits private target ONLY when flag is True
    allowed = validate_webhook_url(
        "https://127.0.0.1:9443/test-hook",
        allow_private_targets=True,
    )
    assert allowed == "https://127.0.0.1:9443/test-hook"

    # Valid public HTTPS webhook passes
    valid_discord = validate_webhook_url(
        "https://discord.com/api/webhooks/123456/token_xyz",
        dns_resolver=_public_dns_resolver,
    )
    assert valid_discord == "https://discord.com/api/webhooks/123456/token_xyz"


def test_refresh_query_cooldown_webhook_deduplication_and_reauth_alerts(tmp_path: Path) -> None:
    async def _run() -> None:
        state: dict[str, Any] = {
            "percent": 50.0,
            "resets_at": "2026-11-27T00:00:00Z",
            "usage_calls": 0,
            "force_reauth_fail": False,
        }
        delivered_Hooks: list[tuple[str, dict[str, Any]]] = []

        async def mock_handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if url == "https://api.anthropic.com/api/oauth/usage":
                state["usage_calls"] += 1
                if state["force_reauth_fail"]:
                    return httpx.Response(401, json={"error": "unauthorized"})
                return httpx.Response(
                    200,
                    json={
                        "five_hour": {
                            "utilization": state["percent"],
                            "resets_at": state["resets_at"],
                        }
                    },
                )
            if url == "https://console.anthropic.com/v1/oauth/token":
                if state["force_reauth_fail"]:
                    return httpx.Response(401, json={"error": "invalid_grant"})
                return httpx.Response(
                    200,
                    json={
                        "access_token": "fresh_at",
                        "refresh_token": "fresh_rt",
                        "expires_in": 7200,
                    },
                )
            if (
                url.startswith("https://162.159.135.232/")
                and request.headers.get("Host") in ("discord.com", "hooks.slack.com", "api.telegram.org")
            ):
                host_hdr = request.headers["Host"]
                assert request.extensions.get("sni_hostname") == host_hdr
                logical_url = f"https://{host_hdr}{request.url.path}"
                delivered_Hooks.append((logical_url, json.loads(request.content.decode())))
                return httpx.Response(200, json={"ok": True})

            return httpx.Response(404, text=f"Unhandled URL: {url}")

        cfg = Settings(
            db_path=tmp_path / "alerts_e2e.db",
            secret_key="alerts-e2e-secret-key",
            enable_background_poller=False,
            refresh_cooldown_seconds=30,
            history_min_interval_seconds=0,
        )
        app = create_app(
            cfg,
            http_transport=httpx.MockTransport(mock_handler),
            dns_resolver=_public_dns_resolver,
        )

        async with app.router.lifespan_context(app):
            db: Database = app.state.db
            svc = app.state.usage_service
            asgi = httpx.ASGITransport(app=app)

            async with httpx.AsyncClient(transport=asgi, base_url="http://testserver") as client:
                # 1. Verify Demo endpoint includes 24h trend sparklines & burn-rate projection in 1 shot
                demo_res = await client.get("/api/usage?demo=true")
                assert demo_res.status_code == 200
                demo_json = demo_res.json()
                assert demo_json["is_demo"] is True
                assert len(demo_json["providers"][0]["trend_24h"]) == 24
                assert demo_json["providers"][1]["will_exhaust_before_reset"] is True
                assert demo_json["providers"][1]["projected_exhaustion_at"] is not None

                # 2. Register user & seed Claude credential
                reg = await client.post(
                    "/api/auth/register",
                    json={"username": "monitor_admin", "password": "password123"},
                )
                uid = reg.json()["user"]["id"]
                db.upsert_credential(
                    user_id=uid,
                    provider="claude",
                    access_token="initial_at",
                    refresh_token="initial_rt",
                    account_label="admin@claude.ai",
                    expires_at=int(time.time()) + 7200,
                )

                # 3. Verify GET /api/usage?refresh=true enforces 30s cooldown & exposes refresh_cooldown_remaining
                r1 = await client.get("/api/usage?refresh=true")
                assert r1.status_code == 200
                assert state["usage_calls"] == 1
                assert r1.json()["refresh_cooldown_remaining"] > 0

                r2 = await client.get("/api/usage?refresh=true")
                assert r2.status_code == 200
                assert state["usage_calls"] == 1  # Cooldown blocked duplicate upstream fetch!
                assert r2.json()["refresh_cooldown_remaining"] > 0

                # 4. SSRF rejection via API when user submits private/non-HTTPS webhook URL
                bad_put = await client.put(
                    "/api/alerts/settings",
                    json={
                        "enabled": True,
                        "discord_webhook_url": "https://192.168.1.10/webhook",
                        "slack_webhook_url": "",
                        "telegram_webhook_url": "",
                        "telegram_chat_id": "",
                        "warn_threshold": 75,
                        "critical_threshold": 90,
                        "burn_rate_threshold_per_hour": 25,
                        "notify_on_burn_rate": False,
                        "notify_on_reauth": True,
                    },
                )
                assert bad_put.status_code == 400

                # 5. Save valid Discord + Slack + Telegram webhooks & verify encrypted at rest
                ok_put = await client.put(
                    "/api/alerts/settings",
                    json={
                        "enabled": True,
                        "discord_webhook_url": "https://discord.com/api/webhooks/111/disc_secret",
                        "slack_webhook_url": "https://hooks.slack.com/services/T1/B1/slack_secret",
                        "telegram_webhook_url": "https://api.telegram.org/bot999:tg_secret/sendMessage",
                        "telegram_chat_id": "-100555666",
                        "warn_threshold": 75,
                        "critical_threshold": 90,
                        "burn_rate_threshold_per_hour": 25,
                        "notify_on_burn_rate": False,
                        "notify_on_reauth": True,
                    },
                )
                assert ok_put.status_code == 200

                with sqlite3.connect(str(cfg.db_path)) as raw_conn:
                    enc_hooks = raw_conn.execute(
                        "SELECT encrypted_webhooks FROM alert_settings WHERE user_id = ?",
                        (uid,),
                    ).fetchone()[0]
                    assert "disc_secret" not in enc_hooks
                    assert "slack_secret" not in enc_hooks
                    assert "tg_secret" not in enc_hooks

                # 6. Test ping delivers to all 3 channels
                ping = await client.post("/api/alerts/test", json={})
                assert ping.status_code == 200
                assert ping.json()["ok"] is True
                assert ping.json()["delivered_channels"] == ["discord", "slack", "telegram"]
                assert len(delivered_Hooks) == 3
                tg_payload = next(b for u, b in delivered_Hooks if "telegram" in u)
                assert tg_payload["chat_id"] == "-100555666"
                delivered_Hooks.clear()

                # 7. Crossing 75% warn threshold fires once, then deduplicates on repeat poll!
                state["percent"] = 78.5
                await svc.fetch_and_store_provider(uid, "claude")
                assert len(delivered_Hooks) == 3  # 1 alert * 3 channels
                delivered_Hooks.clear()

                state["percent"] = 82.0
                await svc.fetch_and_store_provider(uid, "claude")
                assert len(delivered_Hooks) == 0  # Deduplicated!

                # 8. Crossing 90% critical threshold fires once, then deduplicates!
                state["percent"] = 93.0
                await svc.fetch_and_store_provider(uid, "claude")
                assert len(delivered_Hooks) == 3
                delivered_Hooks.clear()

                state["percent"] = 96.0
                await svc.fetch_and_store_provider(uid, "claude")
                assert len(delivered_Hooks) == 0  # Deduplicated!

                # 9. Next quota reset window fires critical again cleanly
                state["resets_at"] = "2026-11-28T00:00:00Z"
                state["percent"] = 91.0
                await svc.fetch_and_store_provider(uid, "claude")
                assert len(delivered_Hooks) == 3
                delivered_Hooks.clear()

                # 10. OAuth refresh_token rotation failure triggers needs_reauth once & deduplicates
                state["force_reauth_fail"] = True
                await svc.fetch_and_store_provider(uid, "claude")
                assert len(delivered_Hooks) == 3
                assert "refresh_token" in delivered_Hooks[0][1]["content"]
                delivered_Hooks.clear()

                await svc.fetch_and_store_provider(uid, "claude")
                assert len(delivered_Hooks) == 0  # Deduplicated!

                # Verify recent_events appear on GET /api/alerts/settings
                settings_get = await client.get("/api/alerts/settings")
                assert settings_get.status_code == 200
                levels = [ev["alert_level"] for ev in settings_get.json()["recent_events"]]
                assert "needs_reauth" in levels
                assert "critical" in levels
                assert "warn" in levels

    asyncio.run(_run())


def test_dns_rebinding_pinning_and_stable_reauth_deduplication(tmp_path: Path) -> None:
    async def _run() -> None:
        dns_calls = 0

        def flipping_dns_resolver(host: str, port: int) -> Sequence[str]:
            nonlocal dns_calls
            dns_calls += 1
            # First lookup (at dispatch) returns public IP; any second lookup would rebind to loopback
            return ["162.159.135.232"] if dns_calls == 1 else ["127.0.0.1"]

        captured_requests: list[httpx.Request] = []

        async def mock_handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if url == "https://console.anthropic.com/v1/oauth/token":
                return httpx.Response(401, json={"error": "invalid_grant"})
            if request.url.host == "162.159.135.232":
                captured_requests.append(request)
                return httpx.Response(200, json={"ok": True})
            return httpx.Response(500, text=f"Unexpected host: {url}")

        cfg = Settings(
            db_path=tmp_path / "pin_reauth.db",
            secret_key="pin-reauth-secret-key",
            enable_background_poller=False,
        )
        app = create_app(
            cfg,
            http_transport=httpx.MockTransport(mock_handler),
            dns_resolver=_public_dns_resolver,
        )

        async with app.router.lifespan_context(app):
            db: Database = app.state.db
            svc = app.state.usage_service
            user = db.create_user("reauth_tester", "hash")
            uid = user["id"]

            from app.schemas import AlertSettingsPayload

            db.upsert_alert_settings(
                uid,
                AlertSettingsPayload(
                    enabled=True,
                    discord_webhook_url="https://hooks-gateway.example.com/webhook/abc",
                ),
            )

            # 1. Verify outgoing request pins literal IP + Host header + TLS sni_hostname in 1 lookup
            svc.alert_manager.dns_resolver = flipping_dns_resolver
            sent, errors = await svc.alert_manager.dispatch_message(
                db.get_alert_settings(uid),
                "pinning check",
            )
            assert sent == ["discord"]
            assert errors == {}
            assert dns_calls == 1
            assert len(captured_requests) == 1
            assert captured_requests[0].url.host == "162.159.135.232"
            assert captured_requests[0].headers["Host"] == "hooks-gateway.example.com"
            assert captured_requests[0].extensions.get("sni_hostname") == "hooks-gateway.example.com"

            # 2. Verify needs_reauth deduplicates permanently even when last_refreshed_at is None
            #    and updated_at changes by > 120 seconds across retries
            svc.alert_manager.dns_resolver = _public_dns_resolver
            captured_requests.clear()

            db.upsert_credential(
                user_id=uid,
                provider="claude",
                access_token="expired_at_v1",
                refresh_token="invalid_rt_v1",
                account_label="reauth@claude.ai",
                expires_at=int(time.time()) - 600,
                last_refreshed_at=None,
            )

            await svc.fetch_and_store_provider(uid, "claude")
            assert len(captured_requests) == 1

            # Simulate updated_at advancing by +3600s (> 120s) without replacing encrypted_payload
            with sqlite3.connect(str(cfg.db_path)) as raw:
                raw.execute(
                    "UPDATE provider_credentials SET updated_at = updated_at + 3600, refresh_status = 'error' WHERE user_id = ?",
                    (uid,),
                )
                raw.commit()

            await svc.fetch_and_store_provider(uid, "claude")
            assert len(captured_requests) == 1  # Still 1 — stable credential_fingerprint deduplicated!

            # Replacing credential with a brand-new OAuth connection resets fingerprint cleanly
            db.upsert_credential(
                user_id=uid,
                provider="claude",
                access_token="expired_at_v2",
                refresh_token="invalid_rt_v2",
                account_label="reauth@claude.ai",
                expires_at=int(time.time()) - 600,
                last_refreshed_at=None,
            )
            await svc.fetch_and_store_provider(uid, "claude")
            assert len(captured_requests) == 2

    asyncio.run(_run())
