from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

from .schemas import ProviderUsageOut, TrendPointOut, UsageWindowOut


@dataclass(frozen=True)
class HistorySample:
    recorded_at: int
    percent: float
    reset_epoch: int | None = None


@dataclass(frozen=True)
class BurnRateProjection:
    burn_rate_per_hour: float | None
    projected_exhaustion_at: int | None
    will_exhaust_before_reset: bool


def make_window_key(win: UsageWindowOut) -> str:
    if win.kind and win.kind != "model":
        return f"{win.kind}:{win.win or win.label}".lower()
    return win.label.strip().lower()


def downsample_trend_points(
    samples: Sequence[HistorySample],
    *,
    max_points: int = 24,
) -> list[TrendPointOut]:
    if not samples or max_points <= 0:
        return []

    ordered = sorted(samples, key=lambda s: s.recorded_at)
    # Deduplicate identical timestamps keeping the latest reading
    deduped: list[HistorySample] = []
    for item in ordered:
        if deduped and deduped[-1].recorded_at == item.recorded_at:
            deduped[-1] = item
        else:
            deduped.append(item)

    if len(deduped) <= max_points:
        return [
            TrendPointOut(t=s.recorded_at, v=round(max(0.0, min(100.0, s.percent)), 1))
            for s in deduped
        ]

    if max_points == 1:
        last = deduped[-1]
        return [TrendPointOut(t=last.recorded_at, v=round(max(0.0, min(100.0, last.percent)), 1))]

    # Always preserve exact first and last points, evenly bucketing interior samples
    t_min = deduped[0].recorded_at
    t_max = deduped[-1].recorded_at
    if t_max <= t_min:
        last = deduped[-1]
        return [TrendPointOut(t=last.recorded_at, v=round(max(0.0, min(100.0, last.percent)), 1))]

    interior_slots = max_points - 2
    result: list[TrendPointOut] = [
        TrendPointOut(t=deduped[0].recorded_at, v=round(max(0.0, min(100.0, deduped[0].percent)), 1))
    ]
    if interior_slots > 0:
        interior = deduped[1:-1]
        span = len(interior)
        for slot in range(interior_slots):
            start_idx = int(math.floor(slot * span / interior_slots))
            end_idx = int(math.floor((slot + 1) * span / interior_slots))
            bucket = interior[start_idx : max(start_idx + 1, end_idx)]
            if not bucket:
                continue
            # Pick the latest reading in each bucket so step transitions & peaks stay crisp
            picked = max(bucket, key=lambda s: (s.percent, s.recorded_at))
            if picked.recorded_at > result[-1].t:
                result.append(
                    TrendPointOut(
                        t=picked.recorded_at,
                        v=round(max(0.0, min(100.0, picked.percent)), 1),
                    )
                )

    last = deduped[-1]
    if last.recorded_at > result[-1].t:
        result.append(
            TrendPointOut(t=last.recorded_at, v=round(max(0.0, min(100.0, last.percent)), 1))
        )
    else:
        result[-1] = TrendPointOut(
            t=last.recorded_at,
            v=round(max(0.0, min(100.0, last.percent)), 1),
        )
    return result


def compute_burn_rate_projection(
    samples: Sequence[HistorySample],
    *,
    current_percent: float,
    reset_epoch: int | None,
    now: int,
    short_window_seconds: int = 2 * 3600,
    max_lookback_seconds: int = 6 * 3600,
    min_span_seconds: int = 60,
) -> BurnRateProjection:
    clamped_pct = max(0.0, min(100.0, current_percent))
    if clamped_pct >= 100.0:
        will_exhaust = reset_epoch is None or now < reset_epoch
        return BurnRateProjection(
            burn_rate_per_hour=None,
            projected_exhaustion_at=now,
            will_exhaust_before_reset=will_exhaust,
        )

    cutoff = now - max_lookback_seconds
    ordered = [s for s in sorted(samples, key=lambda x: x.recorded_at) if cutoff <= s.recorded_at <= now + 5]

    # Filter to samples belonging to the current quota reset cycle
    same_cycle: list[HistorySample] = []
    for sample in ordered:
        if (
            reset_epoch is not None
            and sample.reset_epoch is not None
            and abs(sample.reset_epoch - reset_epoch) > 600
        ):
            continue
        # Detect an unmarked quota reset drop (e.g. 82% -> 3%) and restart series after reset
        if same_cycle and sample.percent + 5.0 < same_cycle[-1].percent:
            same_cycle = [sample]
        else:
            same_cycle.append(sample)

    if not same_cycle or same_cycle[-1].recorded_at < now:
        if same_cycle and clamped_pct + 5.0 < same_cycle[-1].percent:
            same_cycle = [HistorySample(recorded_at=now, percent=clamped_pct, reset_epoch=reset_epoch)]
        else:
            same_cycle.append(
                HistorySample(recorded_at=now, percent=clamped_pct, reset_epoch=reset_epoch)
            )

    if len(same_cycle) < 2:
        return BurnRateProjection(
            burn_rate_per_hour=None,
            projected_exhaustion_at=None,
            will_exhaust_before_reset=False,
        )

    # Prefer recent 2h window if it has sufficient span, capturing acute burn-rate spikes
    recent = [s for s in same_cycle if s.recorded_at >= now - short_window_seconds]
    active = (
        recent
        if len(recent) >= 2 and (recent[-1].recorded_at - recent[0].recorded_at) >= min_span_seconds
        else same_cycle
    )

    span_sec = active[-1].recorded_at - active[0].recorded_at
    if span_sec < min_span_seconds:
        return BurnRateProjection(
            burn_rate_per_hour=None,
            projected_exhaustion_at=None,
            will_exhaust_before_reset=False,
        )

    # Ordinary least squares (OLS) slope in percentage points per second
    n = len(active)
    mean_t = sum(s.recorded_at for s in active) / n
    mean_p = sum(s.percent for s in active) / n
    denom = sum((s.recorded_at - mean_t) ** 2 for s in active)
    if denom <= 0:
        return BurnRateProjection(
            burn_rate_per_hour=None,
            projected_exhaustion_at=None,
            will_exhaust_before_reset=False,
        )

    numer = sum((s.recorded_at - mean_t) * (s.percent - mean_p) for s in active)
    slope_per_sec = numer / denom
    burn_rate_hr = max(0.0, round(slope_per_sec * 3600.0, 2))

    if burn_rate_hr < 0.05:
        return BurnRateProjection(
            burn_rate_per_hour=0.0,
            projected_exhaustion_at=None,
            will_exhaust_before_reset=False,
        )

    remaining_pct = max(0.0, 100.0 - clamped_pct)
    seconds_to_exhaust = (remaining_pct / burn_rate_hr) * 3600.0
    if seconds_to_exhaust > 90 * 86400:
        return BurnRateProjection(
            burn_rate_per_hour=burn_rate_hr,
            projected_exhaustion_at=None,
            will_exhaust_before_reset=False,
        )

    exhaust_epoch = int(round(now + seconds_to_exhaust))
    will_exhaust = bool(reset_epoch is not None and exhaust_epoch < reset_epoch)
    return BurnRateProjection(
        burn_rate_per_hour=burn_rate_hr,
        projected_exhaustion_at=exhaust_epoch,
        will_exhaust_before_reset=will_exhaust,
    )


def enrich_provider_with_history(
    usage: ProviderUsageOut,
    history_by_window: dict[str, list[HistorySample]],
    *,
    now: int,
    max_sparkline_points: int = 24,
) -> ProviderUsageOut:
    if not usage.ok or not usage.windows:
        return usage

    enriched_windows: list[UsageWindowOut] = []
    primary_trend: list[TrendPointOut] = []
    peak_burn: float | None = None
    earliest_exhaust: int | None = None
    any_exhaust = False

    for idx, win in enumerate(usage.windows):
        wkey = make_window_key(win)
        samples = list(history_by_window.get(wkey) or [])
        if not samples or samples[-1].recorded_at < now:
            samples.append(
                HistorySample(
                    recorded_at=now,
                    percent=win.percent,
                    reset_epoch=win.reset_epoch,
                )
            )
        trend = downsample_trend_points(samples, max_points=max_sparkline_points)
        proj = compute_burn_rate_projection(
            samples,
            current_percent=win.percent,
            reset_epoch=win.reset_epoch,
            now=now,
        )

        if idx == 0:
            primary_trend = trend
        if proj.burn_rate_per_hour is not None:
            if peak_burn is None or proj.burn_rate_per_hour > peak_burn:
                peak_burn = proj.burn_rate_per_hour
        if proj.will_exhaust_before_reset and proj.projected_exhaustion_at is not None:
            any_exhaust = True
            if earliest_exhaust is None or proj.projected_exhaustion_at < earliest_exhaust:
                earliest_exhaust = proj.projected_exhaustion_at

        enriched_windows.append(
            win.model_copy(
                update={
                    "burn_rate_per_hour": proj.burn_rate_per_hour,
                    "projected_exhaustion_at": proj.projected_exhaustion_at,
                    "will_exhaust_before_reset": proj.will_exhaust_before_reset,
                    "trend_points": trend,
                }
            )
        )

    return usage.model_copy(
        update={
            "windows": enriched_windows,
            "trend_24h": primary_trend,
            "peak_burn_rate_per_hour": peak_burn,
            "projected_exhaustion_at": earliest_exhaust,
            "will_exhaust_before_reset": any_exhaust,
        }
    )
