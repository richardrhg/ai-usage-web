from __future__ import annotations

import asyncio
import calendar
import math
import time
from dataclasses import dataclass
from datetime import date as dt_date, datetime, timedelta, timezone
from typing import Any, Sequence

import httpx

from .alerts import AlertManager
from .config import Settings
from .database import (
    COST_ORDER_BY_SQL,
    DailyCostRecord,
    Database,
    StoredCostApiKey,
    compute_monthly_budget_status,
    format_cost_provider_label,
)
from .schemas import (
    CostDailyPointOut,
    CostDays,
    CostDrilldownDayOut,
    CostDrilldownResponse,
    CostDrilldownSubItemOut,
    CostGroupBy,
    CostGroupRowOut,
    CostIngestItem,
    CostIngestResponse,
    CostKeyProvider,
    CostSubBreakdownKind,
    CostSummaryResponse,
    CostSyncResponse,
    CostTotalsOut,
    MonthlyBudgetStatusOut,
)

# Per-1M token USD pricing table (input_usd, output_usd, cached_input_usd)
MODEL_PRICING_PER_1M: tuple[tuple[str, tuple[float, float, float]], ...] = (
    ("claude-opus-4", (15.0, 75.0, 1.50)),
    ("claude-3-opus", (15.0, 75.0, 1.50)),
    ("claude-sonnet-4", (3.0, 15.0, 0.30)),
    ("claude-3-7-sonnet", (3.0, 15.0, 0.30)),
    ("claude-3-5-sonnet", (3.0, 15.0, 0.30)),
    ("claude-3-5-haiku", (0.80, 4.0, 0.08)),
    ("claude-haiku", (0.80, 4.0, 0.08)),
    ("o3-mini", (1.10, 4.40, 0.55)),
    ("o4-mini", (1.10, 4.40, 0.275)),
    ("o3", (10.0, 40.0, 2.50)),
    ("o1", (15.0, 60.0, 7.50)),
    ("gpt-4.1-nano", (0.10, 0.40, 0.025)),
    ("gpt-4.1-mini", (0.40, 1.60, 0.10)),
    ("gpt-4o-mini", (0.15, 0.60, 0.075)),
    ("gpt-4.1", (2.0, 8.0, 0.50)),
    ("gpt-4o", (2.50, 10.0, 1.25)),
    ("gemini-2.5-pro", (1.25, 10.0, 0.31)),
    ("gemini-2.5-flash", (0.15, 0.60, 0.04)),
    ("deepseek-r1", (0.55, 2.19, 0.14)),
    ("deepseek-chat", (0.27, 1.10, 0.07)),
)
DEFAULT_RATE_PER_1M = (2.0, 8.0, 0.25)


def estimate_model_cost_usd(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cached_tokens: int = 0,
) -> float:
    lower = model.strip().lower()
    rates = DEFAULT_RATE_PER_1M
    for needle, candidate in MODEL_PRICING_PER_1M:
        if needle in lower:
            rates = candidate
            break
    inp_rate, out_rate, cache_rate = rates
    usd = (
        (max(0, int(input_tokens)) / 1_000_000.0) * inp_rate
        + (max(0, int(output_tokens)) / 1_000_000.0) * out_rate
        + (max(0, int(cached_tokens)) / 1_000_000.0) * cache_rate
    )
    return round(usd, 6)


def _extract_iso_date(raw: Any) -> str | None:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return datetime.fromtimestamp(float(raw), tz=timezone.utc).date().isoformat()
    text = str(raw).strip()
    if len(text) >= 10 and text[4] == "-" and text[7] == "-":
        try:
            return dt_date.fromisoformat(text[:10]).isoformat()
        except ValueError:
            return None
    return None


def _normalize_anthropic_model_from_desc(desc: str) -> str:
    lower = desc.strip().lower()
    if "opus" in lower:
        return "claude-opus-4-1-20250805"
    if "haiku" in lower:
        return "claude-3-5-haiku-20241022"
    if "sonnet" in lower:
        return "claude-sonnet-4-20250514"
    cleaned = desc.strip()
    return cleaned if cleaned else "anthropic-unknown"


def _normalize_billing_model_prefix(raw_line: str) -> str:
    base = raw_line.strip().lower().split(",")[0].split("|")[0].strip()
    for suffix in (
        " cached input",
        " batch input",
        " batch output",
        " input",
        " output",
        " cached",
        " tokens",
        " usage",
    ):
        if base.endswith(suffix):
            base = base[: -len(suffix)].strip()
    return base


def _match_models_for_billing_line(
    raw_line: str,
    candidate_models: Sequence[str],
    *,
    provider: str,
) -> list[str]:
    base = _normalize_billing_model_prefix(raw_line)
    if not base:
        return []

    exact = [m for m in candidate_models if m.strip().lower() == base]
    if exact:
        return exact

    if provider == "anthropic":
        desc_lower = raw_line.strip().lower()
        matched: list[str] = []
        for m in candidate_models:
            ml = m.strip().lower()
            if (
                ml in desc_lower
                or ("opus" in desc_lower and "opus" in ml)
                or ("haiku" in desc_lower and "haiku" in ml)
                or ("sonnet" in desc_lower and "sonnet" in ml)
            ):
                matched.append(m)
        return matched

    family: list[str] = []
    for m in candidate_models:
        ml = m.strip().lower()
        if ("mini" in base) != ("mini" in ml):
            continue
        if ("nano" in base) != ("nano" in ml):
            continue
        if ml.startswith(base + "-") or base.startswith(ml + "-"):
            family.append(m)
    return family


def _allocate_billed_usd(
    usd_val: float,
    matched_models: Sequence[str],
    udate: str,
    token_lookup: dict[tuple[str, str], tuple[int, int, int]],
    allocated_costs: dict[tuple[str, str], float],
) -> None:
    clean_val = round(max(0.0, float(usd_val)), 6)
    if len(matched_models) == 1:
        m = matched_models[0]
        allocated_costs[(udate, m)] = round(allocated_costs.get((udate, m), 0.0) + clean_val, 6)
        return

    weights: list[float] = []
    for m in matched_models:
        inp, outp, cached = token_lookup.get((udate, m), (0, 0, 0))
        est = estimate_model_cost_usd(m, inp, outp, cached)
        weights.append(est if est > 0.0 else float(max(1, inp + outp + cached)))

    total_w = sum(weights)
    remaining = clean_val
    for idx, m in enumerate(matched_models):
        if idx == len(matched_models) - 1 or total_w <= 0.0:
            share = round(max(0.0, remaining), 6)
        else:
            share = round(clean_val * (weights[idx] / total_w), 6)
            remaining = round(remaining - share, 6)
        allocated_costs[(udate, m)] = round(allocated_costs.get((udate, m), 0.0) + share, 6)


async def sync_anthropic_admin_usage(
    api_key: str,
    http_client: httpx.AsyncClient,
    *,
    start_day: dt_date,
    end_day: dt_date,
) -> list[DailyCostRecord]:
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "Accept": "application/json",
    }
    start_iso = f"{start_day.isoformat()}T00:00:00Z"
    end_iso = f"{(end_day + timedelta(days=1)).isoformat()}T00:00:00Z"

    token_buckets: dict[tuple[str, str], tuple[int, int, int, float | None]] = {}
    page_token: str | None = None
    for _ in range(20):
        params: list[tuple[str, str]] = [
            ("starting_at", start_iso),
            ("ending_at", end_iso),
            ("bucket_width", "1d"),
            ("limit", "31"),
            ("group_by[]", "model"),
        ]
        if page_token:
            params.append(("page", page_token))

        resp = await http_client.get(
            "https://api.anthropic.com/v1/organizations/usage_report/messages",
            headers=headers,
            params=params,
        )
        if resp.status_code >= 400:
            raise RuntimeError(
                f"Anthropic Admin Usage API 回應 HTTP {resp.status_code}: {resp.text[:160]}"
            )
        payload = resp.json()
        buckets = payload.get("data") if isinstance(payload, dict) else []
        if not isinstance(buckets, list):
            buckets = []

        for bucket in buckets:
            if not isinstance(bucket, dict):
                continue
            b_date = _extract_iso_date(bucket.get("starting_at") or bucket.get("date"))
            items = bucket.get("results")
            if not isinstance(items, list):
                items = [bucket]
            for item in items:
                if not isinstance(item, dict):
                    continue
                udate = _extract_iso_date(item.get("starting_at") or item.get("date")) or b_date
                if not udate:
                    continue
                model = str(item.get("model") or "claude-unknown").strip()
                inp = int(
                    item.get("uncached_input_tokens")
                    if item.get("uncached_input_tokens") is not None
                    else item.get("input_tokens") or 0
                )
                outp = int(item.get("output_tokens") or 0)
                cache_read = int(item.get("cache_read_input_tokens") or 0)
                cache_create = int(item.get("cache_creation_input_tokens") or 0)
                cc_obj = item.get("cache_creation")
                if isinstance(cc_obj, dict) and cache_create == 0:
                    cache_create = sum(
                        int(v) for v in cc_obj.values() if isinstance(v, (int, float))
                    )
                cached = cache_read + cache_create
                explicit_cost = (
                    float(item["cost_usd"]) if item.get("cost_usd") is not None else None
                )

                prev = token_buckets.get((udate, model))
                if prev is None:
                    token_buckets[(udate, model)] = (inp, outp, cached, explicit_cost)
                else:
                    merged_cost = (
                        (prev[3] or 0.0) + explicit_cost
                        if (prev[3] is not None or explicit_cost is not None)
                        else None
                    )
                    token_buckets[(udate, model)] = (
                        prev[0] + inp,
                        prev[1] + outp,
                        prev[2] + cached,
                        merged_cost,
                    )

        has_more = bool(payload.get("has_more")) if isinstance(payload, dict) else False
        next_page = payload.get("next_page") if isinstance(payload, dict) else None
        if not has_more or not next_page:
            break
        page_token = str(next_page)

    # Paginate Anthropic Cost Report across all pages before reconciling costs
    token_counts_only: dict[tuple[str, str], tuple[int, int, int]] = {
        k: (v[0], v[1], v[2]) for k, v in token_buckets.items()
    }
    cost_overrides: dict[tuple[str, str], float] = {}
    covered_models: set[tuple[str, str]] = set()
    billed_dates: set[str] = set()
    cost_report_complete = False
    cost_page_token: str | None = None

    for page_idx in range(20):
        cparams: list[tuple[str, str]] = [
            ("starting_at", start_iso),
            ("ending_at", end_iso),
            ("bucket_width", "1d"),
            ("limit", "31"),
            ("group_by[]", "description"),
        ]
        if cost_page_token:
            cparams.append(("page", cost_page_token))

        cost_resp = await http_client.get(
            "https://api.anthropic.com/v1/organizations/cost_report",
            headers=headers,
            params=cparams,
        )
        if cost_resp.status_code in (401, 403, 404) and page_idx == 0:
            break
        if cost_resp.status_code >= 400:
            raise RuntimeError(
                f"Anthropic Admin Cost Report API 回應 HTTP {cost_resp.status_code}: {cost_resp.text[:160]}"
            )

        cost_payload = cost_resp.json()
        cost_buckets = cost_payload.get("data") if isinstance(cost_payload, dict) else []
        if isinstance(cost_buckets, list):
            for cbucket in cost_buckets:
                if not isinstance(cbucket, dict):
                    continue
                b_date = _extract_iso_date(cbucket.get("starting_at") or cbucket.get("date"))
                c_items = cbucket.get("results")
                if not isinstance(c_items, list):
                    c_items = [cbucket]
                for citem in c_items:
                    if not isinstance(citem, dict):
                        continue
                    udate = _extract_iso_date(citem.get("starting_at") or citem.get("date")) or b_date
                    if not udate:
                        continue
                    usd_val: float | None = None
                    if citem.get("cost_usd") is not None:
                        usd_val = float(citem["cost_usd"])
                    elif isinstance(citem.get("amount"), dict):
                        usd_val = float(citem["amount"].get("value") or 0.0)
                    elif citem.get("amount") is not None:
                        # Anthropic cost_report returns `amount` in cents string (e.g. "145.0" = $1.45)
                        usd_val = float(citem["amount"]) / 100.0
                    if usd_val is None:
                        continue
                    billed_dates.add(udate)

                    day_candidates = sorted(
                        m for (d_k, m) in token_buckets.keys() if d_k == udate
                    )
                    raw_model = str(citem.get("model") or "").strip()
                    label_for_match = raw_model or str(citem.get("description") or "")
                    matched = _match_models_for_billing_line(
                        label_for_match,
                        day_candidates,
                        provider="anthropic",
                    )
                    if matched:
                        _allocate_billed_usd(
                            usd_val,
                            matched,
                            udate,
                            token_counts_only,
                            cost_overrides,
                        )
                        for m in matched:
                            covered_models.add((udate, m))
                    else:
                        fallback_model = (
                            raw_model
                            if raw_model
                            else _normalize_anthropic_model_from_desc(
                                str(citem.get("description") or "")
                            )
                        )
                        cost_overrides[(udate, fallback_model)] = round(
                            cost_overrides.get((udate, fallback_model), 0.0) + usd_val, 6
                        )

        has_more = bool(cost_payload.get("has_more")) if isinstance(cost_payload, dict) else False
        next_page = cost_payload.get("next_page") if isinstance(cost_payload, dict) else None
        if not has_more or not next_page:
            cost_report_complete = True
            break
        cost_page_token = str(next_page)

    records: list[DailyCostRecord] = []
    seen_keys = set(token_buckets.keys()) | set(cost_overrides.keys())
    for udate, model in sorted(seen_keys):
        inp, outp, cached, explicit_usd = token_buckets.get((udate, model), (0, 0, 0, None))
        if (udate, model) in cost_overrides or (udate, model) in covered_models:
            final_cost = cost_overrides.get((udate, model), 0.0)
        elif explicit_usd is not None:
            final_cost = explicit_usd
        elif cost_report_complete and udate in billed_dates:
            # Date already has authoritative billed line items; avoid double-counting estimates
            final_cost = 0.0
        else:
            final_cost = estimate_model_cost_usd(model, inp, outp, cached)

        records.append(
            DailyCostRecord(
                usage_date=udate,
                provider="anthropic",
                model=model,
                input_tokens=inp,
                output_tokens=outp,
                cached_tokens=cached,
                cost_usd=round(max(0.0, final_cost), 6),
            )
        )
    return records


async def sync_openai_org_usage(
    api_key: str,
    http_client: httpx.AsyncClient,
    *,
    start_day: dt_date,
    end_day: dt_date,
) -> list[DailyCostRecord]:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
    }
    start_epoch = int(
        datetime(start_day.year, start_day.month, start_day.day, tzinfo=timezone.utc).timestamp()
    )
    end_epoch = int(
        datetime(
            (end_day + timedelta(days=1)).year,
            (end_day + timedelta(days=1)).month,
            (end_day + timedelta(days=1)).day,
            tzinfo=timezone.utc,
        ).timestamp()
    )

    token_map: dict[tuple[str, str], tuple[int, int, int]] = {}
    cost_map: dict[tuple[str, str], float] = {}
    covered_models: set[tuple[str, str]] = set()
    billed_dates: set[str] = set()

    usage_ok = False
    usage_err = ""
    page_cursor: str | None = None
    for page_idx in range(20):
        params: dict[str, str | int] = {
            "start_time": start_epoch,
            "end_time": end_epoch,
            "bucket_width": "1d",
            "group_by": "model",
            "limit": 31,
        }
        if page_cursor:
            params["page"] = page_cursor
        resp = await http_client.get(
            "https://api.openai.com/v1/organization/usage/completions",
            headers=headers,
            params=params,
        )
        if resp.status_code >= 400:
            usage_err = f"OpenAI Usage API HTTP {resp.status_code}: {resp.text[:160]}"
            if page_idx > 0:
                raise RuntimeError(usage_err)
            break
        usage_ok = True
        payload = resp.json()
        buckets = payload.get("data") if isinstance(payload, dict) else []
        if isinstance(buckets, list):
            for bucket in buckets:
                if not isinstance(bucket, dict):
                    continue
                b_date = _extract_iso_date(bucket.get("start_time") or bucket.get("date"))
                results = bucket.get("results")
                if not isinstance(results, list):
                    results = [bucket]
                for item in results:
                    if not isinstance(item, dict):
                        continue
                    udate = _extract_iso_date(item.get("start_time") or item.get("date")) or b_date
                    if not udate:
                        continue
                    model = str(item.get("model") or "openai-completions").strip()
                    inp = int(item.get("input_tokens") or 0)
                    outp = int(item.get("output_tokens") or 0)
                    cached = int(
                        item.get("input_cached_tokens")
                        if item.get("input_cached_tokens") is not None
                        else item.get("cached_tokens") or 0
                    )
                    prev = token_map.get((udate, model), (0, 0, 0))
                    token_map[(udate, model)] = (prev[0] + inp, prev[1] + outp, prev[2] + cached)

        has_more = bool(payload.get("has_more")) if isinstance(payload, dict) else False
        next_page = payload.get("next_page") if isinstance(payload, dict) else None
        if not has_more or not next_page:
            break
        page_cursor = str(next_page)

    costs_ok = False
    costs_complete = False
    costs_err = ""
    cost_cursor: str | None = None
    for page_idx in range(20):
        cparams: dict[str, str | int] = {
            "start_time": start_epoch,
            "end_time": end_epoch,
            "bucket_width": "1d",
            "group_by": "line_item",
            "limit": 31,
        }
        if cost_cursor:
            cparams["page"] = cost_cursor
        cresp = await http_client.get(
            "https://api.openai.com/v1/organization/costs",
            headers=headers,
            params=cparams,
        )
        if cresp.status_code >= 400:
            costs_err = f"OpenAI Costs API HTTP {cresp.status_code}: {cresp.text[:160]}"
            if page_idx > 0:
                raise RuntimeError(costs_err)
            break
        costs_ok = True
        cpayload = cresp.json()
        cbuckets = cpayload.get("data") if isinstance(cpayload, dict) else []
        if isinstance(cbuckets, list):
            for cbucket in cbuckets:
                if not isinstance(cbucket, dict):
                    continue
                b_date = _extract_iso_date(cbucket.get("start_time") or cbucket.get("date"))
                cresults = cbucket.get("results")
                if not isinstance(cresults, list):
                    cresults = [cbucket]
                for citem in cresults:
                    if not isinstance(citem, dict):
                        continue
                    udate = _extract_iso_date(citem.get("start_time") or citem.get("date")) or b_date
                    if not udate:
                        continue
                    amt_obj = citem.get("amount")
                    if isinstance(amt_obj, dict):
                        val = float(amt_obj.get("value") or 0.0)
                    elif citem.get("cost_usd") is not None:
                        val = float(citem["cost_usd"])
                    elif amt_obj is not None:
                        val = float(amt_obj)
                    else:
                        continue
                    billed_dates.add(udate)
                    raw_line = str(
                        citem.get("line_item") or citem.get("model") or "openai-billing"
                    ).strip()

                    day_candidates = sorted(
                        m for (d_k, m) in token_map.keys() if d_k == udate
                    )
                    matched_models = _match_models_for_billing_line(
                        raw_line,
                        day_candidates,
                        provider="openai",
                    )
                    if matched_models:
                        _allocate_billed_usd(
                            val,
                            matched_models,
                            udate,
                            token_map,
                            cost_map,
                        )
                        for m in matched_models:
                            covered_models.add((udate, m))
                    else:
                        cost_map[(udate, raw_line)] = round(
                            cost_map.get((udate, raw_line), 0.0) + val, 6
                        )

        has_more = bool(cpayload.get("has_more")) if isinstance(cpayload, dict) else False
        next_page = cpayload.get("next_page") if isinstance(cpayload, dict) else None
        if not has_more or not next_page:
            costs_complete = True
            break
        cost_cursor = str(next_page)

    if not usage_ok and not costs_ok:
        raise RuntimeError(usage_err or costs_err or "OpenAI Organization API 同步失敗")

    records: list[DailyCostRecord] = []
    all_keys = set(token_map.keys()) | set(cost_map.keys())
    for udate, model in sorted(all_keys):
        inp, outp, cached = token_map.get((udate, model), (0, 0, 0))
        if (udate, model) in cost_map or (udate, model) in covered_models:
            final_usd = cost_map.get((udate, model), 0.0)
        elif costs_complete and udate in billed_dates:
            # Date already has authoritative billed line items; never add duplicate estimates
            final_usd = 0.0
        else:
            final_usd = estimate_model_cost_usd(model, inp, outp, cached)
        records.append(
            DailyCostRecord(
                usage_date=udate,
                provider="openai",
                model=model,
                input_tokens=inp,
                output_tokens=outp,
                cached_tokens=cached,
                cost_usd=round(max(0.0, final_usd), 6),
            )
        )
    return records


async def sync_openrouter_usage(
    api_key: str,
    http_client: httpx.AsyncClient,
    *,
    start_day: dt_date,
    end_day: dt_date,
) -> list[DailyCostRecord]:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
    }
    start_iso = start_day.isoformat()
    end_iso = end_day.isoformat()

    act_resp = await http_client.get(
        "https://openrouter.ai/api/v1/activity",
        headers=headers,
    )
    if act_resp.status_code == 200:
        payload = act_resp.json()
        items = payload.get("data") if isinstance(payload, dict) else []
        if not isinstance(items, list):
            items = []
        records: list[DailyCostRecord] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            udate = _extract_iso_date(item.get("date") or item.get("usage_date"))
            if not udate or udate < start_iso or udate > end_iso:
                continue
            model = str(item.get("model") or item.get("model_permaslug") or "openrouter/unknown").strip()
            inp = int(
                item.get("prompt_tokens")
                if item.get("prompt_tokens") is not None
                else item.get("input_tokens") or 0
            )
            outp = int(
                item.get("completion_tokens")
                if item.get("completion_tokens") is not None
                else item.get("output_tokens") or 0
            ) + int(item.get("reasoning_tokens") or 0)
            cached = int(
                item.get("cached_tokens")
                if item.get("cached_tokens") is not None
                else item.get("cache_read_input_tokens") or 0
            )
            if item.get("usage") is not None:
                cost = float(item["usage"])
            elif item.get("cost_usd") is not None:
                cost = float(item["cost_usd"])
            else:
                cost = estimate_model_cost_usd(model, inp, outp, cached)

            records.append(
                DailyCostRecord(
                    usage_date=udate,
                    provider="openrouter",
                    model=model,
                    input_tokens=inp,
                    output_tokens=outp,
                    cached_tokens=cached,
                    cost_usd=round(max(0.0, cost), 6),
                )
            )
        return records

    # Fallback for regular OpenRouter API keys without provisioning /activity access
    key_resp = await http_client.get(
        "https://openrouter.ai/api/v1/auth/key",
        headers=headers,
    )
    if key_resp.status_code >= 400:
        raise RuntimeError(
            f"OpenRouter API 回應 HTTP {act_resp.status_code} / {key_resp.status_code}: {key_resp.text[:160]}"
        )
    kpayload = key_resp.json()
    kdata = kpayload.get("data") if isinstance(kpayload, dict) else {}
    if not isinstance(kdata, dict):
        kdata = {}
    daily_usd = float(
        kdata.get("usage_daily")
        if kdata.get("usage_daily") is not None
        else kdata.get("usage") or 0.0
    )
    return [
        DailyCostRecord(
            usage_date=end_iso,
            provider="openrouter",
            model="openrouter/key-aggregate",
            input_tokens=0,
            output_tokens=0,
            cached_tokens=0,
            cost_usd=round(max(0.0, daily_usd), 6),
        )
    ]


# ── Rich Deterministic Demo Mode Dataset (Zero Login Required) ──────────────


@dataclass(frozen=True)
class _DemoKeySpec:
    id: int
    provider_kind: CostKeyProvider
    usage_provider: str
    label: str
    masked_key: str


@dataclass(frozen=True)
class _DemoRow:
    key_id: int
    key_label: str
    masked_key: str
    usage_date: str
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    cost_usd: float


DEMO_KEYS: tuple[_DemoKeySpec, ...] = (
    _DemoKeySpec(1, "anthropic_admin", "anthropic", "Prod Anthropic Admin", "sk-ant-...8f2a"),
    _DemoKeySpec(2, "openai_org", "openai", "Core OpenAI Org", "sk-...4c91"),
    _DemoKeySpec(3, "openrouter", "openrouter", "Gateway OpenRouter", "sk-or-...e71b"),
    _DemoKeySpec(4, "custom_ingest", "anthropic", "CI Review Bot Telemetry", "cai_...3d09"),
)

DEMO_MODEL_SPECS: tuple[tuple[int, str, str, int, int, int, float], ...] = (
    # (key_id, provider, model, base_input, base_output, base_cached, phase)
    (1, "anthropic", "claude-sonnet-4-20250514", 310_000, 68_000, 420_000, 0.2),
    (1, "anthropic", "claude-opus-4-1-20250805", 44_000, 14_500, 62_000, 1.1),
    (1, "anthropic", "claude-3-5-haiku-20241022", 260_000, 48_000, 190_000, 2.0),
    (2, "openai", "gpt-4.1-2025-04-14", 340_000, 82_000, 240_000, 0.7),
    (2, "openai", "o3-2025-04-16", 38_000, 16_000, 28_000, 1.8),
    (3, "openrouter", "deepseek/deepseek-r1", 290_000, 95_000, 150_000, 2.5),
    (3, "openrouter", "google/gemini-2.5-pro", 210_000, 52_000, 120_000, 3.1),
    (4, "anthropic", "claude-sonnet-4-20250514", 95_000, 22_000, 130_000, 1.5),
)

DEMO_MONTHLY_BUDGET_USD = 280.00
_DEMO_CACHE: dict[str, list[_DemoRow]] = {}


def _get_demo_rows(today: dt_date) -> list[_DemoRow]:
    cache_key = today.isoformat()
    cached = _DEMO_CACHE.get(cache_key)
    if cached is not None:
        return cached

    key_lookup = {k.id: k for k in DEMO_KEYS}
    rows: list[_DemoRow] = []
    start_day = today - timedelta(days=89)

    for day_idx in range(90):
        cur_day = start_day + timedelta(days=day_idx)
        d_iso = cur_day.isoformat()
        weekday = cur_day.weekday()
        weekend_factor = 0.68 if weekday >= 5 else 1.08
        growth_trend = 0.84 + 0.28 * (day_idx / 89.0)

        for key_id, prov, model, b_in, b_out, b_cache, phase in DEMO_MODEL_SPECS:
            wave = 1.0 + 0.22 * math.sin(day_idx * 0.43 + phase)
            scale = weekend_factor * growth_trend * wave
            inp = int(round(b_in * scale))
            outp = int(round(b_out * scale))
            cac = int(round(b_cache * scale))
            cost = estimate_model_cost_usd(model, inp, outp, cac)
            kspec = key_lookup[key_id]
            rows.append(
                _DemoRow(
                    key_id=key_id,
                    key_label=kspec.label,
                    masked_key=kspec.masked_key,
                    usage_date=d_iso,
                    provider=prov,
                    model=model,
                    input_tokens=inp,
                    output_tokens=outp,
                    cached_tokens=cac,
                    cost_usd=cost,
                )
            )

    if len(_DEMO_CACHE) >= 4:
        _DEMO_CACHE.clear()
    _DEMO_CACHE[cache_key] = rows
    return rows


def build_demo_cost_summary(
    *,
    days: CostDays = 30,
    group_by: CostGroupBy = "provider",
    provider_filter: str | None = None,
    orderby: str = "cost_usd desc",
    top: int = 25,
    skip: int = 0,
    today: dt_date | None = None,
) -> CostSummaryResponse:
    ref_day = today or datetime.now(timezone.utc).date()
    norm_order = orderby.strip().lower() if orderby.strip() else "cost_usd desc"
    if norm_order not in COST_ORDER_BY_SQL:
        raise ValueError(
            f"不支援的 orderby 參數 ({orderby})，允許值：{', '.join(COST_ORDER_BY_SQL.keys())}"
        )

    norm_provider = (
        provider_filter.strip().lower()
        if provider_filter and provider_filter.strip().lower() not in {"", "all"}
        else None
    )
    safe_top = max(1, min(100, int(top)))
    safe_skip = max(0, int(skip))

    all_rows = _get_demo_rows(ref_day)
    start_day = ref_day - timedelta(days=int(days) - 1)
    start_date = start_day.isoformat()
    end_date = ref_day.isoformat()

    year_month = f"{ref_day.year:04d}-{ref_day.month:02d}"
    days_in_month = calendar.monthrange(ref_day.year, ref_day.month)[1]
    month_start = f"{year_month}-01"
    month_end = f"{year_month}-{days_in_month:02d}"

    mtd_usd = sum(
        r.cost_usd for r in all_rows if month_start <= r.usage_date <= month_end
    )
    budget_status = compute_monthly_budget_status(
        monthly_budget_usd=DEMO_MONTHLY_BUDGET_USD,
        month_to_date_usd=mtd_usd,
        today=ref_day,
    )

    window_rows_all_prov = [
        r for r in all_rows if start_date <= r.usage_date <= end_date
    ]
    avail_providers = sorted({r.provider for r in window_rows_all_prov})
    filtered_rows = [
        r
        for r in window_rows_all_prov
        if norm_provider is None or r.provider == norm_provider
    ]

    day_map: dict[str, CostDailyPointOut] = {
        (start_day + timedelta(days=i)).isoformat(): CostDailyPointOut(
            date=(start_day + timedelta(days=i)).isoformat()
        )
        for i in range(int(days))
    }

    win_cost = 0.0
    win_inp = 0
    win_out = 0
    win_cac = 0
    grouped_acc: dict[str, dict[str, Any]] = {}

    for r in filtered_rows:
        win_cost += r.cost_usd
        win_inp += r.input_tokens
        win_out += r.output_tokens
        win_cac += r.cached_tokens

        pt = day_map.get(r.usage_date)
        if pt is not None:
            pt.total_cost_usd = round(pt.total_cost_usd + r.cost_usd, 4)
            pt.input_tokens += r.input_tokens
            pt.output_tokens += r.output_tokens
            pt.cached_tokens += r.cached_tokens
            pt.by_provider[r.provider] = round(
                pt.by_provider.get(r.provider, 0.0) + r.cost_usd, 4
            )

        if group_by == "provider":
            gkey = r.provider
            glabel = format_cost_provider_label(r.provider)
            kid: int | None = None
            masked: str | None = None
        elif group_by == "model":
            gkey = r.model
            glabel = r.model
            kid = None
            masked = None
        else:
            gkey = str(r.key_id)
            glabel = f"{r.key_label} ({r.masked_key})"
            kid = r.key_id
            masked = r.masked_key

        entry = grouped_acc.get(gkey)
        if entry is None:
            grouped_acc[gkey] = {
                "group_key": gkey,
                "group_label": glabel,
                "providers": {r.provider},
                "key_id": kid,
                "masked_key": masked,
                "input_tokens": r.input_tokens,
                "output_tokens": r.output_tokens,
                "cached_tokens": r.cached_tokens,
                "cost_usd": r.cost_usd,
                "dates": {r.usage_date},
            }
        else:
            entry["providers"].add(r.provider)
            entry["input_tokens"] += r.input_tokens
            entry["output_tokens"] += r.output_tokens
            entry["cached_tokens"] += r.cached_tokens
            entry["cost_usd"] += r.cost_usd
            entry["dates"].add(r.usage_date)

    win_total_tokens = win_inp + win_out + win_cac
    all_groups: list[CostGroupRowOut] = []
    for entry in grouped_acc.values():
        rcost = round(float(entry["cost_usd"]), 4)
        rtok = int(entry["input_tokens"] + entry["output_tokens"] + entry["cached_tokens"])
        provs = entry["providers"]
        prov_str = next(iter(provs)) if len(provs) == 1 else "multi"
        share = round((rcost / win_cost) * 100.0, 1) if win_cost > 0 else 0.0
        all_groups.append(
            CostGroupRowOut(
                group_key=entry["group_key"],
                group_label=entry["group_label"],
                provider=prov_str,
                key_id=entry["key_id"],
                masked_key=entry["masked_key"],
                input_tokens=int(entry["input_tokens"]),
                output_tokens=int(entry["output_tokens"]),
                cached_tokens=int(entry["cached_tokens"]),
                total_tokens=rtok,
                cost_usd=rcost,
                share_pct=min(100.0, max(0.0, share)),
                record_days=len(entry["dates"]),
            )
        )

    reverse = norm_order.endswith(" desc")
    field = norm_order.split()[0]
    if field == "cost_usd":
        all_groups.sort(key=lambda g: (g.cost_usd, g.total_tokens), reverse=reverse)
    elif field == "total_tokens":
        all_groups.sort(key=lambda g: (g.total_tokens, g.cost_usd), reverse=reverse)
    elif field == "input_tokens":
        all_groups.sort(key=lambda g: (g.input_tokens, g.cost_usd), reverse=reverse)
    elif field == "output_tokens":
        all_groups.sort(key=lambda g: (g.output_tokens, g.cost_usd), reverse=reverse)
    else:
        all_groups.sort(key=lambda g: g.group_key, reverse=reverse)

    paged_groups = all_groups[safe_skip : safe_skip + safe_top]

    return CostSummaryResponse(
        days=days,
        start_date=start_date,
        end_date=end_date,
        group_by=group_by,
        provider_filter=norm_provider,
        orderby=norm_order,
        top=safe_top,
        skip=safe_skip,
        total_count=len(all_groups),
        totals=CostTotalsOut(
            cost_usd=round(win_cost, 4),
            input_tokens=win_inp,
            output_tokens=win_out,
            cached_tokens=win_cac,
            total_tokens=win_total_tokens,
        ),
        budget=budget_status,
        daily_series=list(day_map.values()),
        groups=paged_groups,
        available_providers=avail_providers,
        bound_keys_count=len(DEMO_KEYS),
        last_synced_at=int(time.time()) - 90,
        sync_cooldown_seconds=0,
        sync_cooldown_remaining=0,
        is_demo=True,
    )


def build_demo_cost_drilldown(
    *,
    group_by: CostGroupBy,
    group_value: str,
    days: CostDays = 30,
    provider_filter: str | None = None,
    today: dt_date | None = None,
) -> CostDrilldownResponse:
    ref_day = today or datetime.now(timezone.utc).date()
    start_date = (ref_day - timedelta(days=int(days) - 1)).isoformat()
    end_date = ref_day.isoformat()
    norm_provider = (
        provider_filter.strip().lower()
        if provider_filter and provider_filter.strip().lower() not in {"", "all"}
        else None
    )
    cleaned_val = group_value.strip()

    rows = [
        r
        for r in _get_demo_rows(ref_day)
        if start_date <= r.usage_date <= end_date
        and (norm_provider is None or r.provider == norm_provider)
    ]

    sub_kind: CostSubBreakdownKind
    if group_by == "provider":
        matched = [r for r in rows if r.provider == cleaned_val.lower()]
        group_label = format_cost_provider_label(cleaned_val)
        sub_kind = "model"
    elif group_by == "model":
        matched = [r for r in rows if r.model == cleaned_val]
        group_label = cleaned_val
        sub_kind = "key"
    else:
        try:
            kid = int(cleaned_val)
        except ValueError as exc:
            raise ValueError("當 group_by=key 時，group_value 必須為整數金鑰 ID") from exc
        matched = [r for r in rows if r.key_id == kid]
        kspec = next((k for k in DEMO_KEYS if k.id == kid), None)
        group_label = (
            f"{kspec.label} ({kspec.masked_key})" if kspec else f"Key #{kid}"
        )
        sub_kind = "model"

    sub_acc: dict[str, dict[str, Any]] = {}
    day_acc: dict[str, dict[str, Any]] = {}

    for r in matched:
        if sub_kind == "model":
            skey = r.model
            slabel = r.model
        else:
            skey = str(r.key_id)
            slabel = f"{r.key_label} ({r.masked_key})"

        sentry = sub_acc.get(skey)
        if sentry is None:
            sub_acc[skey] = {
                "key": skey,
                "label": slabel,
                "provider": r.provider,
                "input_tokens": r.input_tokens,
                "output_tokens": r.output_tokens,
                "cached_tokens": r.cached_tokens,
                "cost_usd": r.cost_usd,
            }
        else:
            sentry["input_tokens"] += r.input_tokens
            sentry["output_tokens"] += r.output_tokens
            sentry["cached_tokens"] += r.cached_tokens
            sentry["cost_usd"] += r.cost_usd

        dentry = day_acc.get(r.usage_date)
        if dentry is None:
            day_acc[r.usage_date] = {
                "date": r.usage_date,
                "input_tokens": r.input_tokens,
                "output_tokens": r.output_tokens,
                "cached_tokens": r.cached_tokens,
                "cost_usd": r.cost_usd,
            }
        else:
            dentry["input_tokens"] += r.input_tokens
            dentry["output_tokens"] += r.output_tokens
            dentry["cached_tokens"] += r.cached_tokens
            dentry["cost_usd"] += r.cost_usd

    total_cost = round(sum(float(v["cost_usd"]) for v in sub_acc.values()), 4)
    total_tok = sum(
        int(v["input_tokens"] + v["output_tokens"] + v["cached_tokens"])
        for v in sub_acc.values()
    )

    sub_list: list[CostDrilldownSubItemOut] = []
    for v in sub_acc.values():
        scost = round(float(v["cost_usd"]), 4)
        stok = int(v["input_tokens"] + v["output_tokens"] + v["cached_tokens"])
        share = round((scost / total_cost) * 100.0, 1) if total_cost > 0 else 0.0
        sub_list.append(
            CostDrilldownSubItemOut(
                key=v["key"],
                label=v["label"],
                provider=v["provider"],
                input_tokens=int(v["input_tokens"]),
                output_tokens=int(v["output_tokens"]),
                cached_tokens=int(v["cached_tokens"]),
                total_tokens=stok,
                cost_usd=scost,
                share_pct=min(100.0, max(0.0, share)),
            )
        )
    sub_list.sort(key=lambda x: (x.cost_usd, x.total_tokens), reverse=True)

    day_list = [
        CostDrilldownDayOut(
            date=d["date"],
            input_tokens=int(d["input_tokens"]),
            output_tokens=int(d["output_tokens"]),
            cached_tokens=int(d["cached_tokens"]),
            total_tokens=int(d["input_tokens"] + d["output_tokens"] + d["cached_tokens"]),
            cost_usd=round(float(d["cost_usd"]), 4),
        )
        for d in sorted(day_acc.values(), key=lambda x: x["date"], reverse=True)
    ]

    return CostDrilldownResponse(
        group_by=group_by,
        group_value=cleaned_val,
        group_label=group_label,
        days=days,
        total_cost_usd=total_cost,
        total_tokens=total_tok,
        sub_breakdown_kind=sub_kind,
        sub_breakdown=sub_list,
        daily_breakdown=day_list,
        is_demo=True,
    )


# ── CostService (Sync Coordinator, Cooldown, Ingest & Budget Alerts) ────────


class CostService:
    def __init__(
        self,
        *,
        db: Database,
        settings: Settings,
        http_client: httpx.AsyncClient,
        alert_manager: AlertManager,
    ) -> None:
        self.db = db
        self.settings = settings
        self.http_client = http_client
        self.alert_manager = alert_manager
        self._user_sync_locks: dict[int, asyncio.Lock] = {}
        self._last_manual_sync: dict[int, int] = {}

    def _user_lock(self, user_id: int) -> asyncio.Lock:
        lock = self._user_sync_locks.get(user_id)
        if lock is None:
            lock = asyncio.Lock()
            self._user_sync_locks[user_id] = lock
        return lock

    def remaining_sync_cooldown(self, user_id: int) -> int:
        last = self._last_manual_sync.get(user_id)
        if last is None:
            return 0
        rem = (last + self.settings.refresh_cooldown_seconds) - int(time.time())
        return max(0, rem)

    async def _safe_evaluate_budget_alerts(
        self,
        user_id: int,
        *,
        today: dt_date | None = None,
    ) -> None:
        try:
            await self.alert_manager.evaluate_budget_alerts(user_id, today=today)
        except Exception:
            pass

    async def _sync_single_key(
        self,
        key: StoredCostApiKey,
        *,
        start_day: dt_date,
        end_day: dt_date,
    ) -> int:
        if key.provider == "anthropic_admin":
            records = await sync_anthropic_admin_usage(
                key.api_key,
                self.http_client,
                start_day=start_day,
                end_day=end_day,
            )
        elif key.provider == "openai_org":
            records = await sync_openai_org_usage(
                key.api_key,
                self.http_client,
                start_day=start_day,
                end_day=end_day,
            )
        elif key.provider == "openrouter":
            records = await sync_openrouter_usage(
                key.api_key,
                self.http_client,
                start_day=start_day,
                end_day=end_day,
            )
        else:
            return 0

        if records:
            return self.db.upsert_daily_cost_records(
                user_id=key.user_id,
                key_id=key.id,
                records=records,
            )
        self.db.mark_cost_key_sync_status(key.id, synced_at=int(time.time()), error=None)
        return 0

    def get_summary_for_user(
        self,
        user_id: int,
        *,
        days: CostDays = 30,
        group_by: CostGroupBy = "provider",
        provider_filter: str | None = None,
        orderby: str = "cost_usd desc",
        top: int = 25,
        skip: int = 0,
        today: dt_date | None = None,
    ) -> CostSummaryResponse:
        rem = self.remaining_sync_cooldown(user_id)
        return self.db.query_cost_summary(
            user_id,
            days=days,
            group_by=group_by,
            provider_filter=provider_filter,
            orderby=orderby,
            top=top,
            skip=skip,
            sync_cooldown_seconds=self.settings.refresh_cooldown_seconds,
            sync_cooldown_remaining=rem,
            today=today,
        )

    async def sync_user_costs(
        self,
        user_id: int,
        *,
        days: CostDays = 30,
        group_by: CostGroupBy = "provider",
        provider_filter: str | None = None,
        orderby: str = "cost_usd desc",
        top: int = 25,
        skip: int = 0,
        bypass_cooldown: bool = False,
        today: dt_date | None = None,
    ) -> CostSyncResponse:
        async with self._user_lock(user_id):
            rem = self.remaining_sync_cooldown(user_id)
            if not bypass_cooldown and rem > 0:
                summary = self.get_summary_for_user(
                    user_id,
                    days=days,
                    group_by=group_by,
                    provider_filter=provider_filter,
                    orderby=orderby,
                    top=top,
                    skip=skip,
                    today=today,
                )
                return CostSyncResponse(
                    ok=True,
                    cooldown_active=True,
                    synced_keys=0,
                    upserted_rows=0,
                    key_errors={},
                    sync_cooldown_seconds=self.settings.refresh_cooldown_seconds,
                    sync_cooldown_remaining=rem,
                    summary=summary,
                )

            ref_day = today or datetime.now(timezone.utc).date()
            lookback_days = max(int(days), 31)
            start_day = ref_day - timedelta(days=lookback_days - 1)

            all_keys = self.db.list_decrypted_cost_api_keys(user_id)
            syncable = [k for k in all_keys if k.provider != "custom_ingest"]

            synced_count = 0
            upserted_total = 0
            errors: dict[str, str] = {}

            if syncable:
                self._last_manual_sync[user_id] = int(time.time())

                async def _run_one(k: StoredCostApiKey) -> None:
                    nonlocal synced_count, upserted_total
                    try:
                        count = await self._sync_single_key(
                            k,
                            start_day=start_day,
                            end_day=ref_day,
                        )
                        synced_count += 1
                        upserted_total += count
                    except Exception as exc:
                        err_text = str(exc)
                        errors[f"{k.label} ({k.masked_key})"] = err_text
                        self.db.mark_cost_key_sync_status(
                            k.id,
                            synced_at=None,
                            error=err_text,
                        )

                await asyncio.gather(*(_run_one(k) for k in syncable))
                await self._safe_evaluate_budget_alerts(user_id, today=ref_day)

            new_rem = self.remaining_sync_cooldown(user_id)
            summary = self.db.query_cost_summary(
                user_id,
                days=days,
                group_by=group_by,
                provider_filter=provider_filter,
                orderby=orderby,
                top=top,
                skip=skip,
                sync_cooldown_seconds=self.settings.refresh_cooldown_seconds,
                sync_cooldown_remaining=new_rem,
                today=ref_day,
            )
            return CostSyncResponse(
                ok=len(errors) == 0,
                cooldown_active=False,
                synced_keys=synced_count,
                upserted_rows=upserted_total,
                key_errors=errors,
                sync_cooldown_seconds=self.settings.refresh_cooldown_seconds,
                sync_cooldown_remaining=new_rem,
                summary=summary,
            )

    async def ingest_records(
        self,
        *,
        user_id: int,
        key_id: int,
        items: Sequence[CostIngestItem],
        today: dt_date | None = None,
    ) -> CostIngestResponse:
        ref_day = today or datetime.now(timezone.utc).date()
        records = [
            DailyCostRecord(
                usage_date=item.usage_date,
                provider=item.provider,
                model=item.model,
                input_tokens=item.input_tokens,
                output_tokens=item.output_tokens,
                cached_tokens=item.cached_tokens,
                cost_usd=(
                    float(item.cost_usd)
                    if item.cost_usd is not None
                    else estimate_model_cost_usd(
                        item.model,
                        item.input_tokens,
                        item.output_tokens,
                        item.cached_tokens,
                    )
                ),
            )
            for item in items
        ]
        upserted = self.db.upsert_daily_cost_records(
            user_id=user_id,
            key_id=key_id,
            records=records,
        )
        await self._safe_evaluate_budget_alerts(user_id, today=ref_day)
        budget = self.db.get_monthly_budget_status(user_id, today=ref_day)
        return CostIngestResponse(
            ok=True,
            key_id=key_id,
            upserted_count=upserted,
            budget=budget,
        )

    async def update_budget(
        self,
        user_id: int,
        monthly_budget_usd: float,
        *,
        today: dt_date | None = None,
    ) -> MonthlyBudgetStatusOut:
        ref_day = today or datetime.now(timezone.utc).date()
        status = self.db.upsert_monthly_budget(
            user_id,
            monthly_budget_usd,
            today=ref_day,
        )
        await self._safe_evaluate_budget_alerts(user_id, today=ref_day)
        return status
