from __future__ import annotations

import asyncio
import base64
import json
import sqlite3
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from app.config import Settings
from app.crypto import CredentialCipher
from app.database import Database
from app.main import create_app


def _make_jwt(payload: dict[str, Any]) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"none","typ":"JWT"}').decode().rstrip("=")
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"{header}.{body}.sig"


class MockUpstreamRouter:
    def __init__(self) -> None:
        self.claude_refresh_calls = 0
        self.codex_refresh_calls = 0
        self.claude_usage_calls = 0
        self.used_codex_refresh_tokens: set[str] = set()
        self.codex_device_approved = False
        self.gemini_ineligible = False

    async def handle(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        auth = request.headers.get("Authorization", "")

        # ── Anthropic OAuth Token Exchange / Refresh ──
        if url == "https://console.anthropic.com/v1/oauth/token":
            body = json.loads(request.content.decode())
            if body.get("grant_type") == "authorization_code":
                return httpx.Response(
                    200,
                    json={
                        "access_token": "claude_at_initial",
                        "refresh_token": "claude_rt_initial",
                        "expires_in": 7200,
                        "account": {"email_address": "alice@claude.ai"},
                    },
                )
            if body.get("grant_type") == "refresh_token":
                self.claude_refresh_calls += 1
                return httpx.Response(
                    200,
                    json={
                        "access_token": f"claude_at_refreshed_{self.claude_refresh_calls}",
                        "refresh_token": f"claude_rt_rotated_{self.claude_refresh_calls}",
                        "expires_in": 7200,
                        "account": {"email_address": "alice@claude.ai"},
                    },
                )

        # ── Anthropic OAuth Usage ──
        if url == "https://api.anthropic.com/api/oauth/usage":
            self.claude_usage_calls += 1
            if auth == "Bearer stale_but_unexpired_token":
                return httpx.Response(401, json={"error": {"message": "token revoked"}})
            return httpx.Response(
                200,
                json={
                    "five_hour": {"utilization": 18.4, "resets_at": "2026-11-26T12:00:00Z"},
                    "seven_day": {"utilization": 42.0, "resets_at": "2026-11-30T12:00:00Z"},
                    "seven_day_opus": {"utilization": 15.0, "resets_at": "2026-11-30T12:00:00Z"},
                    "extra_usage": {
                        "is_enabled": True,
                        "used_credits": 1250,
                        "monthly_limit": 5000,
                        "decimal_places": 2,
                        "currency": "USD",
                    },
                },
            )

        # ── OpenAI Codex Device Code & Token Refresh ──
        if url == "https://auth.openai.com/api/accounts/deviceauth/usercode":
            return httpx.Response(
                200,
                json={
                    "device_auth_id": "dev_auth_999",
                    "user_code": "ABCD-9999",
                    "interval": "4",
                },
            )

        if url == "https://auth.openai.com/api/accounts/deviceauth/token":
            if not self.codex_device_approved:
                return httpx.Response(403, json={"detail": "authorization_pending"})
            return httpx.Response(
                200,
                json={
                    "authorization_code": "oa_auth_code_1",
                    "code_verifier": "oa_pkce_verifier_1",
                    "code_challenge": "oa_pkce_challenge_1",
                },
            )

        if url == "https://auth.openai.com/oauth/token":
            raw = request.content.decode()
            if request.headers.get("Content-Type", "").startswith("application/json"):
                data = json.loads(raw)
            else:
                parsed_qs = parse_qs(raw)
                data = {k: v[0] for k, v in parsed_qs.items()}

            if data.get("grant_type") == "authorization_code":
                jwt_tok = _make_jwt(
                    {
                        "https://api.openai.com/auth": {"chatgpt_account_id": "acct_oa_1"},
                        "https://api.openai.com/profile": {"email": "alice@openai.com"},
                        "exp": int(time.time()) + 3600,
                    }
                )
                return httpx.Response(
                    200,
                    json={
                        "access_token": jwt_tok,
                        "refresh_token": "codex_rt_initial",
                        "id_token": jwt_tok,
                        "expires_in": 3600,
                    },
                )

            if data.get("grant_type") == "refresh_token":
                await asyncio.sleep(0.03)  # Simulate network latency to test Single-flight lock
                rt = data["refresh_token"]
                if rt in self.used_codex_refresh_tokens:
                    return httpx.Response(
                        400,
                        json={"error": {"code": "refresh_token_reused"}},
                    )
                self.used_codex_refresh_tokens.add(rt)
                self.codex_refresh_calls += 1
                jwt_tok = _make_jwt(
                    {
                        "https://api.openai.com/auth": {"chatgpt_account_id": "acct_oa_1"},
                        "https://api.openai.com/profile": {"email": "alice@openai.com"},
                        "exp": int(time.time()) + 3600,
                    }
                )
                return httpx.Response(
                    200,
                    json={
                        "access_token": jwt_tok,
                        "refresh_token": f"codex_rt_v{self.codex_refresh_calls + 1}",
                        "id_token": jwt_tok,
                        "expires_in": 3600,
                    },
                )

        if url == "https://chatgpt.com/backend-api/wham/usage":
            return httpx.Response(
                200,
                json={
                    "plan_type": "plus",
                    "rate_limit": {
                        "primary_window": {
                            "used_percent": 62,
                            "limit_window_seconds": 18000,
                            "reset_at": int(time.time()) + 7200,
                        },
                        "secondary_window": {
                            "used_percent": 81,
                            "limit_window_seconds": 604800,
                            "reset_at": int(time.time()) + 172800,
                        },
                    },
                },
            )

        # ── Google Gemini OAuth & Quota ──
        if url == "https://oauth2.googleapis.com/token":
            return httpx.Response(
                200,
                json={
                    "access_token": "gemini_at_1",
                    "refresh_token": "gemini_rt_1",
                    "expires_in": 3600,
                },
            )

        if url.startswith("https://www.googleapis.com/oauth2/v1/userinfo"):
            return httpx.Response(200, json={"email": "alice@gmail.com"})

        if url == "https://cloudcode-pa.googleapis.com/v1internal:loadCodeAssist":
            if self.gemini_ineligible:
                return httpx.Response(
                    403,
                    json={
                        "error": {
                            "status": "UNSUPPORTED_CLIENT",
                            "message": "IneligibleTierError",
                        }
                    },
                )
            return httpx.Response(
                200,
                json={
                    "cloudaicompanionProject": "gemini-proj-123",
                    "paidTier": {"name": "standard-tier"},
                },
            )

        if url == "https://cloudcode-pa.googleapis.com/v1internal:retrieveUserQuota":
            return httpx.Response(
                200,
                json={
                    "buckets": [
                        {
                            "modelId": "models/gemini-2.5-pro",
                            "remainingFraction": 0.65,
                            "resetTime": "2026-11-27T00:00:00Z",
                        },
                        {
                            "modelId": "models/gemini-2.5-flash",
                            "remainingFraction": 0.9,
                            "resetTime": "2026-11-27T00:00:00Z",
                        },
                    ]
                },
            )

        return httpx.Response(404, text=f"Unhandled mock URL: {url}")


def _make_settings(tmp_path: Path, *, cooldown: int = 30) -> Settings:
    return Settings(
        db_path=tmp_path / "test.db",
        secret_key="unit-test-secret-key-xyz",
        enable_background_poller=False,
        refresh_cooldown_seconds=cooldown,
        gemini_client_id="test-gemini-client-id",
        gemini_client_secret="test-gemini-client-secret",
    )


def test_startup_auto_refreshes_expired_token(tmp_path: Path) -> None:
    async def _run() -> None:
        cfg = _make_settings(tmp_path)
        mock = MockUpstreamRouter()
        transport = httpx.MockTransport(mock.handle)

        # Pre-seed an expired Claude token on disk before server boots
        db = Database(cfg.db_path, CredentialCipher(cfg.secret_key))
        user = db.create_user("richard", "dummy_hash")
        db.upsert_credential(
            user_id=user["id"],
            provider="claude",
            access_token="expired_claude_at",
            refresh_token="claude_rt_seed",
            account_label="richard@claude.ai",
            expires_at=int(time.time()) - 900,
        )

        # Verify encrypted at rest in raw SQLite
        with sqlite3.connect(str(cfg.db_path)) as raw_conn:
            raw_text = raw_conn.execute(
                "SELECT encrypted_payload FROM provider_credentials"
            ).fetchone()[0]
            assert "expired_claude_at" not in raw_text
            assert "claude_rt_seed" not in raw_text

        app = create_app(cfg, http_transport=transport)
        async with app.router.lifespan_context(app):
            assert mock.claude_refresh_calls == 1
            refreshed = app.state.db.get_credential(user["id"], "claude")
            assert refreshed is not None
            assert refreshed.access_token == "claude_at_refreshed_1"
            assert refreshed.refresh_token == "claude_rt_rotated_1"
            assert refreshed.expires_at is not None and refreshed.expires_at > int(time.time()) + 3600

    asyncio.run(_run())


def test_single_flight_refresh_prevents_rotation_collision(tmp_path: Path) -> None:
    async def _run() -> None:
        cfg = _make_settings(tmp_path)
        mock = MockUpstreamRouter()
        transport = httpx.MockTransport(mock.handle)
        app = create_app(cfg, http_transport=transport)

        async with app.router.lifespan_context(app):
            db: Database = app.state.db
            user = db.create_user("concurrent_user", "dummy_hash")
            db.upsert_credential(
                user_id=user["id"],
                provider="codex",
                access_token="expiring_soon_at",
                refresh_token="codex_rt_v1",
                expires_at=int(time.time()) + 30,  # Inside 300s proactive buffer
            )

            tm = app.state.usage_service.token_manager
            results = await asyncio.gather(
                *(tm.ensure_valid_token(user["id"], "codex") for _ in range(6))
            )
            assert mock.codex_refresh_calls == 1
            assert all(r is not None and r.refresh_token == "codex_rt_v2" for r in results)

    asyncio.run(_run())


def test_multi_user_isolation_and_all_three_oauth_flows(tmp_path: Path) -> None:
    async def _run() -> None:
        cfg = _make_settings(tmp_path)
        mock = MockUpstreamRouter()
        transport = httpx.MockTransport(mock.handle)
        app = create_app(cfg, http_transport=transport)

        async with app.router.lifespan_context(app):
            asgi = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=asgi, base_url="http://testserver") as alice, \
                       httpx.AsyncClient(transport=asgi, base_url="http://testserver") as bob:

                # 1. Register Alice & Bob
                r_alice = await alice.post(
                    "/api/auth/register",
                    json={"username": "alice", "password": "password123"},
                )
                assert r_alice.status_code == 200
                assert r_alice.json()["authenticated"] is True

                r_bob = await bob.post(
                    "/api/auth/register",
                    json={"username": "bob", "password": "password456"},
                )
                assert r_bob.status_code == 200

                # 2. Alice links Claude via PKCE code#state
                start_claude = (await alice.post("/api/oauth/claude/start")).json()
                assert start_claude["oauth_method"] == "pkce_code"
                comp_claude = await alice.post(
                    "/api/oauth/claude/complete",
                    json={
                        "state_id": start_claude["state_id"],
                        "code_input": "auth_code_xyz#custom_state",
                    },
                )
                assert comp_claude.status_code == 200
                assert comp_claude.json()["status"] == "completed"

                # 3. Alice links Codex via Device Code flow (pending -> approved)
                start_codex = (await alice.post("/api/oauth/codex/start")).json()
                assert start_codex["user_code"] == "ABCD-9999"

                poll_pending = await alice.post(
                    "/api/oauth/codex/poll",
                    json={"state_id": start_codex["state_id"]},
                )
                assert poll_pending.json()["status"] == "pending"

                mock.codex_device_approved = True
                poll_done = await alice.post(
                    "/api/oauth/codex/poll",
                    json={"state_id": start_codex["state_id"]},
                )
                assert poll_done.json()["status"] == "completed"

                # 4. Alice links Gemini via pasted localhost callback URL
                start_gem = (await alice.post("/api/oauth/gemini/start")).json()
                parsed_auth = urlparse(start_gem["authorize_url"])
                expected_state = parse_qs(parsed_auth.query)["state"][0]
                pasted_url = (
                    f"http://localhost:8085/oauth2callback?code=4/0AeanS_test&state={expected_state}"
                )
                comp_gem = await alice.post(
                    "/api/oauth/gemini/complete",
                    json={"state_id": start_gem["state_id"], "code_input": pasted_url},
                )
                assert comp_gem.status_code == 200
                assert comp_gem.json()["status"] == "completed"

                # 5. Single-shot GET /api/usage for Alice returns all 3 providers!
                alice_dash = (await alice.get("/api/usage")).json()
                assert [p["id"] for p in alice_dash["providers"]] == [
                    "claude",
                    "codex",
                    "gemini",
                ]
                assert all(p["ok"] is True for p in alice_dash["providers"])
                assert alice_dash["providers"][2]["windows"][0]["percent"] == 35.0

                # 6. Multi-user isolation: Bob sees 0 linked providers!
                bob_dash = (await bob.get("/api/usage")).json()
                assert bob_dash["providers"] == []
                assert all(c["connected"] is False for c in bob_dash["connections"])

                # 7. Manual refresh cooldown prevents upstream spam
                calls_before = mock.claude_usage_calls
                ref1 = (await alice.post("/api/usage/refresh")).json()
                assert mock.claude_usage_calls == calls_before + 1
                assert ref1["refresh_cooldown_seconds"] > 0

                ref2 = (await alice.post("/api/usage/refresh")).json()
                assert mock.claude_usage_calls == calls_before + 1
                assert ref2["refresh_cooldown_seconds"] > 0

    asyncio.run(_run())


def test_reactive_401_triggers_auto_refresh_and_retry(tmp_path: Path) -> None:
    async def _run() -> None:
        cfg = _make_settings(tmp_path)
        mock = MockUpstreamRouter()
        transport = httpx.MockTransport(mock.handle)
        app = create_app(cfg, http_transport=transport)

        async with app.router.lifespan_context(app):
            db: Database = app.state.db
            user = db.create_user("revoked_user", "dummy_hash")
            db.upsert_credential(
                user_id=user["id"],
                provider="claude",
                access_token="stale_but_unexpired_token",
                refresh_token="claude_rt_recover",
                expires_at=int(time.time()) + 3600,
            )

            usage = await app.state.usage_service.fetch_and_store_provider(user["id"], "claude")
            assert usage is not None
            assert usage.ok is True
            assert mock.claude_refresh_calls == 1
            assert usage.windows[0].percent == 18.4

    asyncio.run(_run())
