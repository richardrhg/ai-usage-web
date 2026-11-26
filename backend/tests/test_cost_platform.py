from __future__ import annotations

import asyncio
from datetime import date as dt_date, datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any, Sequence

import httpx
import pytest

from app.config import Settings
from app.crypto import CredentialCipher, mask_api_key
from app.database import DailyCostRecord, Database
from app.main import create_app


def _public_dns_resolver(host: str, port: int) -> Sequence[str]:
    if "internal" in host or "rebind" in host:
        return ["10.24.0.15"]
    return ["162.159.135.232"]


def test_mask_api_key_never_exposes_secret_middle() -> None:
    raw_ant = "sk-ant-admin01-SUPER_SECRET_MIDDLE_9f2a"
    masked_ant = mask_api_key(raw_ant)
    assert masked_ant == "sk-ant-...9f2a"
    assert "SUPER_SECRET" not in masked_ant

    raw_oa = "sk-admin-OPENAI_SECRET_VALUE_4c91"
    masked_oa = mask_api_key(raw_oa)
    assert masked_oa == "sk-...4c91"
    assert "OPENAI_SECRET" not in masked_oa

    raw_or = "sk-or-v1-OPENROUTER_SECRET_e71b"
    masked_or = mask_api_key(raw_or)
    assert masked_or == "sk-or-...e71b"
    assert "OPENROUTER_SECRET" not in masked_or


def test_query_cost_summary_is_strictly_o1_sql_statements(tmp_path: Path) -> None:
    db = Database(tmp_path / "o1_cost.db", CredentialCipher("o1-cost-secret"))
    user = db.create_user("perf_tester", "hash")
    uid = user["id"]
    today = dt_date(2026, 11, 26)
    db.upsert_monthly_budget(uid, 500.0, today=today)

    # Small dataset: 2 keys, 2 models
    for idx in range(2):
        k_out, _ = db.create_cost_api_key(
            user_id=uid,
            provider="openai_org",
            label=f"Small Key {idx}",
            raw_api_key=f"sk-admin-smallsecretkey{idx:04d}",
        )
        db.upsert_daily_cost_records(
            user_id=uid,
            key_id=k_out.id,
            records=[
                DailyCostRecord(
                    usage_date="2026-11-25",
                    provider="openai",
                    model=f"gpt-model-{idx}",
                    input_tokens=10_000,
                    output_tokens=2_000,
                    cached_tokens=1_000,
                    cost_usd=1.25,
                )
            ],
        )

    small_selects: list[str] = []
    db.sql_trace_callback = lambda sql: (
        small_selects.append(sql) if sql.lstrip().upper().startswith("SELECT") else None
    )
    small_summary = db.query_cost_summary(uid, days=30, group_by="model", today=today)
    db.sql_trace_callback = None
    assert small_summary.total_count == 2
    assert len(small_selects) == 3

    # Scale dataset 20x–60x: add 40 keys & 120 distinct models across multiple days
    for k_idx in range(40):
        k_out, _ = db.create_cost_api_key(
            user_id=uid,
            provider="anthropic_admin" if k_idx % 2 == 0 else "openrouter",
            label=f"Scale Key {k_idx}",
            raw_api_key=f"sk-ant-admin-scalesecret{k_idx:04d}",
        )
        batch = [
            DailyCostRecord(
                usage_date=f"2026-11-{(m_idx % 25) + 1:02d}",
                provider="anthropic" if k_idx % 2 == 0 else "openrouter",
                model=f"scaled-model-{k_idx * 3 + m_idx}",
                input_tokens=5_000 * (m_idx + 1),
                output_tokens=1_200 * (m_idx + 1),
                cached_tokens=800 * (m_idx + 1),
                cost_usd=0.35 * (m_idx + 1),
            )
            for m_idx in range(3)
        ]
        db.upsert_daily_cost_records(user_id=uid, key_id=k_out.id, records=batch)

    for gby in ("provider", "model", "key"):
        large_selects: list[str] = []
        db.sql_trace_callback = lambda sql: (
            large_selects.append(sql) if sql.lstrip().upper().startswith("SELECT") else None
        )
        large_summary = db.query_cost_summary(
            uid,
            days=30,
            group_by=gby,  # type: ignore[arg-type]
            orderby="cost_usd desc",
            top=15,
            skip=0,
            today=today,
        )
        db.sql_trace_callback = None
        # Exact same O(1) SQL statement count (3) regardless of 42 keys & 122 models!
        assert len(large_selects) == len(small_selects) == 3
        assert large_summary.totals.cost_usd > small_summary.totals.cost_usd
        if gby == "model":
            assert large_summary.total_count == 122
            assert len(large_summary.groups) == 15
        elif gby == "key":
            assert large_summary.total_count == 42
            assert len(large_summary.groups) == 15


def test_cost_keys_encryption_ingest_sync_cooldown_drilldown_and_demo(tmp_path: Path) -> None:
    async def _run() -> None:
        upstream_calls = {"anthropic": 0, "openai": 0, "openrouter": 0}
        today_iso = datetime.now(timezone.utc).date().isoformat()
        today_epoch = int(
            datetime.combine(
                datetime.now(timezone.utc).date(),
                datetime.min.time(),
                tzinfo=timezone.utc,
            ).timestamp()
        )

        async def mock_handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if url.startswith("https://api.anthropic.com/v1/organizations/usage_report/messages"):
                upstream_calls["anthropic"] += 1
                assert request.headers.get("x-api-key") == "sk-ant-admin01-REAL_ANT_SECRET_8899"
                return httpx.Response(
                    200,
                    json={
                        "data": [
                            {
                                "starting_at": f"{today_iso}T00:00:00Z",
                                "results": [
                                    {
                                        "model": "claude-sonnet-4-20250514",
                                        "uncached_input_tokens": 200_000,
                                        "cache_read_input_tokens": 100_000,
                                        "output_tokens": 50_000,
                                    }
                                ],
                            }
                        ],
                        "has_more": False,
                    },
                )
            if url.startswith("https://api.anthropic.com/v1/organizations/cost_report"):
                return httpx.Response(
                    200,
                    json={
                        "data": [
                            {
                                "starting_at": f"{today_iso}T00:00:00Z",
                                "results": [
                                    {
                                        "description": "Claude Sonnet 4 Usage",
                                        "amount": "138.0",  # 138 cents = $1.38 USD
                                        "currency": "USD",
                                    }
                                ],
                            }
                        ],
                        "has_more": False,
                    },
                )
            if url.startswith("https://api.openai.com/v1/organization/usage/completions"):
                upstream_calls["openai"] += 1
                assert request.headers.get("authorization") == "Bearer sk-admin-REAL_OA_SECRET_7766"
                return httpx.Response(
                    200,
                    json={
                        "data": [
                            {
                                "start_time": today_epoch,
                                "results": [
                                    {
                                        "model": "gpt-4.1-2025-04-14",
                                        "input_tokens": 300_000,
                                        "output_tokens": 60_000,
                                        "input_cached_tokens": 50_000,
                                    }
                                ],
                            }
                        ],
                        "has_more": False,
                    },
                )
            if url.startswith("https://api.openai.com/v1/organization/costs"):
                return httpx.Response(
                    200,
                    json={
                        "data": [
                            {
                                "start_time": today_epoch,
                                "results": [
                                    {
                                        "line_item": "gpt-4.1-2025-04-14",
                                        "amount": {"value": 1.105, "currency": "usd"},
                                    }
                                ],
                            }
                        ],
                        "has_more": False,
                    },
                )
            if url.startswith("https://openrouter.ai/api/v1/activity"):
                upstream_calls["openrouter"] += 1
                assert request.headers.get("authorization") == "Bearer sk-or-v1-REAL_OR_SECRET_5544"
                return httpx.Response(
                    200,
                    json={
                        "data": [
                            {
                                "date": today_iso,
                                "model": "deepseek/deepseek-r1",
                                "prompt_tokens": 150_000,
                                "completion_tokens": 40_000,
                                "reasoning_tokens": 10_000,
                                "cached_tokens": 25_000,
                                "usage": 0.92,
                            }
                        ]
                    },
                )
            return httpx.Response(404, text=f"Unhandled URL: {url}")

        cfg = Settings(
            db_path=tmp_path / "costs_e2e.db",
            secret_key="costs-e2e-secret",
            enable_background_poller=False,
            refresh_cooldown_seconds=30,
        )
        app = create_app(
            cfg,
            http_transport=httpx.MockTransport(mock_handler),
            dns_resolver=_public_dns_resolver,
        )

        async with app.router.lifespan_context(app):
            asgi = httpx.ASGITransport(app=app)

            # 1. Anonymous Demo Mode works without authentication across summary & drilldown
            async with httpx.AsyncClient(transport=asgi, base_url="http://testserver") as anon:
                unauth = await anon.get("/api/costs/summary")
                assert unauth.status_code == 401

                demo_sum = await anon.get(
                    "/api/costs/summary?demo=true&days=30&group_by=provider"
                )
                assert demo_sum.status_code == 200
                djson = demo_sum.json()
                assert djson["is_demo"] is True
                assert djson["totals"]["cost_usd"] > 50.0
                assert djson["budget"]["monthly_budget_usd"] == 280.0
                assert len(djson["daily_series"]) == 30
                assert djson["total_count"] == 3  # anthropic, openai, openrouter

                demo_models = await anon.get(
                    "/api/costs/summary?demo=true&days=7&group_by=model&top=3&skip=1"
                )
                assert demo_models.status_code == 200
                assert len(demo_models.json()["daily_series"]) == 7
                assert len(demo_models.json()["groups"]) == 3
                assert demo_models.json()["total_count"] >= 7

                demo_drill = await anon.get(
                    "/api/costs/drilldown?demo=true&group_by=provider&group_value=anthropic&days=30"
                )
                assert demo_drill.status_code == 200
                assert demo_drill.json()["is_demo"] is True
                assert demo_drill.json()["sub_breakdown_kind"] == "model"
                assert len(demo_drill.json()["sub_breakdown"]) >= 3

            # 2. Authenticated user key creation, Fernet encryption at rest, masking, sync & ingest
            async with httpx.AsyncClient(transport=asgi, base_url="http://testserver") as client:
                await client.post(
                    "/api/auth/register",
                    json={"username": "billing_owner", "password": "password123"},
                )

                k_ant = await client.post(
                    "/api/costs/keys",
                    json={
                        "provider": "anthropic_admin",
                        "label": "Prod Anthropic",
                        "api_key": "sk-ant-admin01-REAL_ANT_SECRET_8899",
                    },
                )
                assert k_ant.status_code == 201
                assert k_ant.json()["key"]["masked_key"] == "sk-ant-...8899"
                assert k_ant.json()["generated_secret"] is None

                k_oa = await client.post(
                    "/api/costs/keys",
                    json={
                        "provider": "openai_org",
                        "label": "Prod OpenAI",
                        "api_key": "sk-admin-REAL_OA_SECRET_7766",
                    },
                )
                assert k_oa.status_code == 201
                assert k_oa.json()["key"]["masked_key"] == "sk-...7766"

                k_or = await client.post(
                    "/api/costs/keys",
                    json={
                        "provider": "openrouter",
                        "label": "Gateway OR",
                        "api_key": "sk-or-v1-REAL_OR_SECRET_5544",
                    },
                )
                assert k_or.status_code == 201
                assert k_or.json()["key"]["masked_key"] == "sk-or-...5544"

                k_ing = await client.post(
                    "/api/costs/keys",
                    json={
                        "provider": "custom_ingest",
                        "label": "CI Pipeline Hook",
                        "api_key": "",
                    },
                )
                assert k_ing.status_code == 201
                gen_secret = k_ing.json()["generated_secret"]
                ing_key_id = k_ing.json()["key"]["id"]
                assert gen_secret and gen_secret.startswith("cai_live_")

                # Verify raw SQLite table has zero plaintext secrets
                with sqlite3.connect(str(cfg.db_path)) as raw_conn:
                    enc_rows = raw_conn.execute(
                        "SELECT encrypted_key, masked_key FROM cost_api_keys"
                    ).fetchall()
                    assert len(enc_rows) == 4
                    for enc_val, mask_val in enc_rows:
                        for raw_frag in (
                            "REAL_ANT_SECRET",
                            "REAL_OA_SECRET",
                            "REAL_OR_SECRET",
                            gen_secret,
                        ):
                            assert raw_frag not in enc_val
                            assert raw_frag not in mask_val

                # Verify GET /api/costs/keys never returns generated_secret or raw keys
                keys_list = await client.get("/api/costs/keys")
                assert keys_list.status_code == 200
                raw_list_text = json.dumps(keys_list.json())
                assert gen_secret not in raw_list_text
                assert "REAL_ANT_SECRET" not in raw_list_text

                # 3. Sync upstream billing & verify 30s cooldown protection
                sync1 = await client.post("/api/costs/sync?days=30&group_by=provider")
                assert sync1.status_code == 200
                s1 = sync1.json()
                assert s1["ok"] is True
                assert s1["cooldown_active"] is False
                assert s1["synced_keys"] == 3
                assert s1["sync_cooldown_remaining"] > 0
                assert upstream_calls == {"anthropic": 1, "openai": 1, "openrouter": 1}
                assert s1["summary"]["totals"]["cost_usd"] == pytest.approx(
                    1.38 + 1.105 + 0.92, rel=1e-4
                )

                # Immediate second sync is blocked by 30s cooldown (0 extra upstream calls)
                sync2 = await client.post("/api/costs/sync?days=30&group_by=provider")
                assert sync2.status_code == 200
                assert sync2.json()["cooldown_active"] is True
                assert upstream_calls == {"anthropic": 1, "openai": 1, "openrouter": 1}

                # 4. Idempotent Bearer ingest via custom_ingest token (no session cookie needed!)
                async with httpx.AsyncClient(transport=asgi, base_url="http://testserver") as hook:
                    ingest_payload = {
                        "records": [
                            {
                                "usage_date": today_iso,
                                "provider": "custom",
                                "model": "internal-coder-v1",
                                "input_tokens": 80_000,
                                "output_tokens": 20_000,
                                "cached_tokens": 10_000,
                                "cost_usd": 0.595,
                            }
                        ]
                    }
                    ing1 = await hook.post(
                        "/api/costs/ingest",
                        headers={"Authorization": f"Bearer {gen_secret}"},
                        json=ingest_payload,
                    )
                    assert ing1.status_code == 200
                    assert ing1.json()["key_id"] == ing_key_id
                    assert ing1.json()["upserted_count"] == 1

                    # Re-sending exact same daily record must NOT double-count cost!
                    ing2 = await hook.post(
                        "/api/costs/ingest",
                        headers={"Authorization": f"Bearer {gen_secret}"},
                        json=ingest_payload,
                    )
                    assert ing2.status_code == 200

                sum_after = await client.get("/api/costs/summary?days=30&group_by=key")
                assert sum_after.status_code == 200
                # 1.38 + 1.105 + 0.92 + 0.595 = 4.00 USD exact!
                assert sum_after.json()["totals"]["cost_usd"] == pytest.approx(4.00, rel=1e-4)
                assert sum_after.json()["total_count"] == 4

                # 5. Lazy single-row drilldown on key & provider
                drill_prov = await client.get(
                    "/api/costs/drilldown?group_by=provider&group_value=anthropic&days=30"
                )
                assert drill_prov.status_code == 200
                assert drill_prov.json()["total_cost_usd"] == pytest.approx(1.38, rel=1e-4)
                assert drill_prov.json()["sub_breakdown"][0]["key"] == "claude-sonnet-4-20250514"

                # 6. Deleting custom_ingest key cascades its daily usage & isolates users
                del_res = await client.delete(f"/api/costs/keys/{ing_key_id}")
                assert del_res.status_code == 200
                sum_post_del = await client.get("/api/costs/summary?days=30&group_by=provider")
                assert sum_post_del.json()["totals"]["cost_usd"] == pytest.approx(3.405, rel=1e-4)

                # 7. Invalid query parameters are rejected cleanly (400)
                bad_days = await client.get("/api/costs/summary?days=15")
                assert bad_days.status_code == 400
                bad_order = await client.get("/api/costs/summary?orderby=malicious_col")
                assert bad_order.status_code == 400

            # 8. Multi-user isolation: second user sees zero keys/costs & cannot delete user 1's keys
            async with httpx.AsyncClient(transport=asgi, base_url="http://testserver") as client2:
                await client2.post(
                    "/api/auth/register",
                    json={"username": "second_tenant", "password": "password123"},
                )
                other_keys = await client2.get("/api/costs/keys")
                assert other_keys.json()["keys"] == []
                other_sum = await client2.get("/api/costs/summary")
                assert other_sum.json()["totals"]["cost_usd"] == 0.0
                forbid_del = await client2.delete(f"/api/costs/keys/{k_ant.json()['key']['id']}")
                assert forbid_del.status_code == 404

    asyncio.run(_run())


def test_monthly_budget_webhook_alerts_deduplicate_by_year_month_and_threshold(
    tmp_path: Path,
) -> None:
    async def _run() -> None:
        delivered_hooks: list[tuple[str, dict[str, Any]]] = []

        async def mock_handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if url.startswith("https://162.159.135.232/") and request.headers.get("Host") in (
                "discord.com",
                "hooks.slack.com",
            ):
                host_hdr = request.headers["Host"]
                assert request.extensions.get("sni_hostname") == host_hdr
                logical_url = f"https://{host_hdr}{request.url.path}"
                delivered_hooks.append((logical_url, json.loads(request.content.decode())))
                return httpx.Response(200, json={"ok": True})
            return httpx.Response(404)

        cfg = Settings(
            db_path=tmp_path / "budget_alerts.db",
            secret_key="budget-alerts-secret",
            enable_background_poller=False,
        )
        app = create_app(
            cfg,
            http_transport=httpx.MockTransport(mock_handler),
            dns_resolver=_public_dns_resolver,
        )

        async with app.router.lifespan_context(app):
            cost_svc = app.state.cost_service
            db: Database = app.state.db
            asgi = httpx.ASGITransport(app=app)

            async with httpx.AsyncClient(transport=asgi, base_url="http://testserver") as client:
                reg = await client.post(
                    "/api/auth/register",
                    json={"username": "finops_lead", "password": "password123"},
                )
                uid = reg.json()["user"]["id"]

                await client.put(
                    "/api/alerts/settings",
                    json={
                        "enabled": True,
                        "discord_webhook_url": "https://discord.com/api/webhooks/222/budget_disc",
                        "slack_webhook_url": "https://hooks.slack.com/services/T2/B2/budget_slack",
                        "telegram_webhook_url": "",
                        "telegram_chat_id": "",
                        "warn_threshold": 75,
                        "critical_threshold": 90,
                        "burn_rate_threshold_per_hour": 25,
                        "notify_on_burn_rate": True,
                        "notify_on_reauth": True,
                    },
                )

                bud_res = await client.put(
                    "/api/costs/budget",
                    json={"monthly_budget_usd": 100.0},
                )
                assert bud_res.status_code == 200
                assert bud_res.json()["monthly_budget_usd"] == 100.0

                key_id = db.get_or_create_default_ingest_key(uid)

                # Simulate mid-month Nov 15, 2026 (15 / 30 days = 2.0x linear projection!)
                nov_15 = dt_date(2026, 11, 15)
                from app.schemas import CostIngestItem

                # 1. Spend $39 on Nov 15 -> actual 39%, projected $78 (78% >= 75%) -> fires 75% ONCE!
                await cost_svc.ingest_records(
                    user_id=uid,
                    key_id=key_id,
                    items=[
                        CostIngestItem(
                            usage_date="2026-11-15",
                            provider="anthropic",
                            model="claude-sonnet-4-20250514",
                            input_tokens=1_000_000,
                            output_tokens=200_000,
                            cost_usd=39.0,
                        )
                    ],
                    today=nov_15,
                )
                assert len(delivered_hooks) == 2  # 1 alert * 2 channels (Discord + Slack)
                assert "75%" in delivered_hooks[0][1]["content"]
                delivered_hooks.clear()

                # Repeat ingest at $41 (projected $82, still in 75% tier) -> strictly deduplicated!
                await cost_svc.ingest_records(
                    user_id=uid,
                    key_id=key_id,
                    items=[
                        CostIngestItem(
                            usage_date="2026-11-15",
                            provider="anthropic",
                            model="claude-sonnet-4-20250514",
                            input_tokens=1_000_000,
                            output_tokens=200_000,
                            cost_usd=41.0,
                        )
                    ],
                    today=nov_15,
                )
                assert len(delivered_hooks) == 0

                # 2. End of month Nov 30: actual reaches $92 (92% >= 90%) -> fires 90% ONCE!
                nov_30 = dt_date(2026, 11, 30)
                await cost_svc.ingest_records(
                    user_id=uid,
                    key_id=key_id,
                    items=[
                        CostIngestItem(
                            usage_date="2026-11-30",
                            provider="openai",
                            model="gpt-4.1",
                            input_tokens=2_000_000,
                            output_tokens=500_000,
                            cost_usd=51.0,  # $41 + $51 = $92
                        )
                    ],
                    today=nov_30,
                )
                assert len(delivered_hooks) == 2
                assert "90%" in delivered_hooks[0][1]["content"]
                delivered_hooks.clear()

                # Repeat at $95 -> deduplicated!
                await cost_svc.ingest_records(
                    user_id=uid,
                    key_id=key_id,
                    items=[
                        CostIngestItem(
                            usage_date="2026-11-30",
                            provider="openai",
                            model="gpt-4.1",
                            input_tokens=2_000_000,
                            output_tokens=500_000,
                            cost_usd=54.0,  # $41 + $54 = $95
                        )
                    ],
                    today=nov_30,
                )
                assert len(delivered_hooks) == 0

                # 3. Cross 100% ($41 + $64 = $105) -> fires 100% ONCE!
                await cost_svc.ingest_records(
                    user_id=uid,
                    key_id=key_id,
                    items=[
                        CostIngestItem(
                            usage_date="2026-11-30",
                            provider="openai",
                            model="gpt-4.1",
                            input_tokens=2_000_000,
                            output_tokens=500_000,
                            cost_usd=64.0,
                        )
                    ],
                    today=nov_30,
                )
                assert len(delivered_hooks) == 2
                assert "100%" in delivered_hooks[0][1]["content"]
                delivered_hooks.clear()

                # Repeat at $112 -> deduplicated!
                await cost_svc.ingest_records(
                    user_id=uid,
                    key_id=key_id,
                    items=[
                        CostIngestItem(
                            usage_date="2026-11-30",
                            provider="openai",
                            model="gpt-4.1",
                            input_tokens=2_000_000,
                            output_tokens=500_000,
                            cost_usd=71.0,
                        )
                    ],
                    today=nov_30,
                )
                assert len(delivered_hooks) == 0

                # 4. Next calendar month (2026-12) resets deduplication cleanly!
                dec_31 = dt_date(2026, 12, 31)
                await cost_svc.ingest_records(
                    user_id=uid,
                    key_id=key_id,
                    items=[
                        CostIngestItem(
                            usage_date="2026-12-31",
                            provider="anthropic",
                            model="claude-opus-4-1-20250805",
                            input_tokens=1_000_000,
                            output_tokens=500_000,
                            cost_usd=108.0,
                        )
                    ],
                    today=dec_31,
                )
                assert len(delivered_hooks) == 2
                assert "2026-12" in delivered_hooks[0][1]["content"]

    asyncio.run(_run())
