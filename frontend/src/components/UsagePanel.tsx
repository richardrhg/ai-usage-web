import { useEffect, useRef, useState } from "react";
import type {
  ProviderUsageOut,
  TrendPointOut,
  UsageWindowOut,
} from "../api/client";
import { I18N, LangId } from "../i18n/translations";

interface UsagePanelProps {
  provider: ProviderUsageOut;
  lang: LangId;
  nowEpoch: number;
}

function formatDuration(sec: number, lang: LangId): string {
  const t = I18N[lang];
  if (sec <= 0) return t.now;
  const d = Math.floor(sec / 86400);
  const h = Math.floor((sec % 86400) / 3600);
  const m = Math.floor((sec % 3600) / 60);
  return t.resets(t.dur(d, h, m));
}

function formatEtaDuration(sec: number, lang: LangId): string {
  const t = I18N[lang];
  if (sec <= 0) return t.exhaustNow;
  const d = Math.floor(sec / 86400);
  const h = Math.floor((sec % 86400) / 3600);
  const m = Math.floor((sec % 3600) / 60);
  return t.exhaustEta(t.dur(d, h, m));
}

function resolveWindowLabel(w: UsageWindowOut, lang: LangId): string {
  const t = I18N[lang];
  if (w.kind && w.kind !== "model" && t.kind[w.kind]) {
    const mapped = t.kind[w.kind];
    return w.win ? t.win(mapped, w.win) : mapped;
  }
  return w.label;
}

function resolveErrorText(p: ProviderUsageOut, lang: LangId): string {
  const t = I18N[lang];
  if (p.code && p.code in t.err) {
    const entry = t.err[p.code];
    if (typeof entry === "function") {
      return entry(p.arg ?? "");
    }
    return entry;
  }
  return p.error || t.unknown;
}

function TrendSparkline({
  providerId,
  points,
  isCritical,
  lang,
  peakBurnRate,
}: {
  providerId: string;
  points: TrendPointOut[];
  isCritical: boolean;
  lang: LangId;
  peakBurnRate?: number | null;
}) {
  const t = I18N[lang];
  const width = 220;
  const height = 34;
  const padX = 3;
  const padY = 4;
  const innerW = width - padX * 2;
  const innerH = height - padY * 2;

  const hasCurve = points.length >= 2;
  const tMin = hasCurve ? points[0].t : 0;
  const tMax = hasCurve ? Math.max(points[points.length - 1].t, tMin + 1) : 1;

  const coords = hasCurve
    ? points.map((pt, idx) => {
        const xRatio =
          tMax > tMin
            ? (pt.t - tMin) / (tMax - tMin)
            : idx / Math.max(1, points.length - 1);
        const x = padX + xRatio * innerW;
        const clamped = Math.max(0, Math.min(100, pt.v));
        const y = padY + innerH - (clamped / 100) * innerH;
        return { x, y };
      })
    : [];

  const linePoints = coords.map((c) => `${c.x.toFixed(1)},${c.y.toFixed(1)}`).join(" ");
  const areaPath =
    coords.length >= 2
      ? `M ${coords[0].x.toFixed(1)} ${(padY + innerH).toFixed(1)} ` +
        coords.map((c) => `L ${c.x.toFixed(1)} ${c.y.toFixed(1)}`).join(" ") +
        ` L ${coords[coords.length - 1].x.toFixed(1)} ${(padY + innerH).toFixed(1)} Z`
      : "";

  const strokeColor = isCritical ? "var(--crit)" : "var(--acc-hi)";
  const gradId = `spark-grad-${providerId}`;
  const yWarn = padY + innerH - 0.75 * innerH;
  const yCrit = padY + innerH - 0.9 * innerH;
  const lastCoord = coords.length > 0 ? coords[coords.length - 1] : null;

  return (
    <div className="spark-strip">
      <div className="spark-meta">
        <span className="spark-title">{t.trend24h}</span>
        {peakBurnRate != null && peakBurnRate > 0 ? (
          <span className={`spark-rate ${isCritical ? "crit" : ""}`}>
            {t.burnRate(peakBurnRate)}
          </span>
        ) : !hasCurve ? (
          <span className="spark-collecting">{t.collectingTrend}</span>
        ) : null}
      </div>

      <svg
        className="spark-svg"
        viewBox={`0 0 ${width} ${height}`}
        preserveAspectRatio="none"
        aria-label={t.trend24h}
      >
        <defs>
          <linearGradient id={gradId} x1="0" y1="0" x2="0" y2="1">
            <stop
              offset="0%"
              stopColor={isCritical ? "var(--crit)" : "var(--acc)"}
              stopOpacity="0.38"
            />
            <stop
              offset="100%"
              stopColor={isCritical ? "var(--crit)" : "var(--acc)"}
              stopOpacity="0.02"
            />
          </linearGradient>
        </defs>

        <line
          x1={padX}
          y1={yWarn.toFixed(1)}
          x2={width - padX}
          y2={yWarn.toFixed(1)}
          stroke="var(--warn)"
          strokeOpacity="0.28"
          strokeDasharray="2 3"
          strokeWidth="1"
        />
        <line
          x1={padX}
          y1={yCrit.toFixed(1)}
          x2={width - padX}
          y2={yCrit.toFixed(1)}
          stroke="var(--crit)"
          strokeOpacity="0.3"
          strokeDasharray="2 3"
          strokeWidth="1"
        />

        {hasCurve ? (
          <>
            <path d={areaPath} fill={`url(#${gradId})`} />
            <polyline
              fill="none"
              stroke={strokeColor}
              strokeWidth="1.8"
              strokeLinejoin="round"
              strokeLinecap="round"
              points={linePoints}
            />
            {lastCoord && (
              <circle
                cx={lastCoord.x.toFixed(1)}
                cy={lastCoord.y.toFixed(1)}
                r="2.6"
                fill={strokeColor}
              />
            )}
          </>
        ) : (
          <line
            x1={padX}
            y1={(padY + innerH - ((points[0]?.v ?? 0) / 100) * innerH).toFixed(1)}
            x2={width - padX}
            y2={(padY + innerH - ((points[0]?.v ?? 0) / 100) * innerH).toFixed(1)}
            stroke="var(--acc)"
            strokeOpacity="0.55"
            strokeDasharray="4 3"
            strokeWidth="1.5"
          />
        )}
      </svg>
    </div>
  );
}

function AnimatedUsageRow({
  win,
  lang,
  nowEpoch,
}: {
  win: UsageWindowOut;
  lang: LangId;
  nowEpoch: number;
}) {
  const t = I18N[lang];
  const target = Math.max(0, Math.min(100, win.percent));
  const [displayPct, setDisplayPct] = useState(0);
  const [barWidth, setBarWidth] = useState(0);
  const prevTargetRef = useRef(0);

  useEffect(() => {
    const from = prevTargetRef.current;
    prevTargetRef.current = target;
    const rafBar = requestAnimationFrame(() => setBarWidth(target));

    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (reduced || from === target) {
      setDisplayPct(Math.round(target));
      return () => cancelAnimationFrame(rafBar);
    }

    const duration = 560;
    const start = performance.now();
    let frame = 0;
    const step = (now: number) => {
      const k = Math.min(1, (now - start) / duration);
      const eased = 1 - Math.pow(1 - k, 3);
      setDisplayPct(Math.round(from + (target - from) * eased));
      if (k < 1) frame = requestAnimationFrame(step);
    };
    frame = requestAnimationFrame(step);
    return () => {
      cancelAnimationFrame(rafBar);
      cancelAnimationFrame(frame);
    };
  }, [target]);

  const cls = target >= 90 ? "crit" : target >= 75 ? "warn" : "";
  const resetText =
    win.reset_epoch != null ? formatDuration(win.reset_epoch - nowEpoch, lang) : null;
  const willExhaust = Boolean(win.will_exhaust_before_reset);
  const etaText =
    willExhaust && win.projected_exhaustion_at != null
      ? formatEtaDuration(win.projected_exhaustion_at - nowEpoch, lang)
      : null;

  return (
    <div className="row">
      <div className="row-top">
        <div className="label">{resolveWindowLabel(win, lang)}</div>
        <div className="pct">{displayPct}%</div>
      </div>
      <div className="track">
        <div className={`fill ${cls}`} style={{ width: `${barWidth}%` }} />
      </div>
      <div className="row-foot">
        <span>{resetText || ""}</span>
        <span className="row-burn-group">
          {win.burn_rate_per_hour != null && win.burn_rate_per_hour > 0 && (
            <span className="burn-rate-inline">
              {t.burnRate(win.burn_rate_per_hour)}
            </span>
          )}
          {etaText ? (
            <span className="badge crit">{etaText}</span>
          ) : win.burn_rate_per_hour != null ? (
            <span className="badge ok">{t.safeBadge}</span>
          ) : null}
        </span>
      </div>
    </div>
  );
}

export function UsagePanel({ provider, lang, nowEpoch }: UsagePanelProps) {
  const t = I18N[lang];
  const extra = provider.extra_usage;
  const willExhaust = Boolean(provider.will_exhaust_before_reset);
  const headerEta =
    willExhaust && provider.projected_exhaustion_at != null
      ? formatEtaDuration(provider.projected_exhaustion_at - nowEpoch, lang)
      : null;
  const sparkPoints =
    provider.trend_24h && provider.trend_24h.length > 0
      ? provider.trend_24h
      : provider.windows?.[0]?.trend_points ?? [];

  return (
    <div className={`panel p-${provider.id}`}>
      <div className="phead">
        <div className="phead-left">
          <div className="pname">{provider.name}</div>
          {provider.ok && (
            <span className={`badge ${headerEta ? "crit" : "ok"}`}>
              {headerEta || t.safeBadge}
            </span>
          )}
        </div>
        <div className="psrc">
          {provider.account_label || t.usage}
          {provider.plan ? (
            <>
              <br />
              {provider.plan}
            </>
          ) : null}
        </div>
      </div>

      {!provider.ok ? (
        <div className="perr">{resolveErrorText(provider, lang)}</div>
      ) : (
        <>
          {sparkPoints.length > 0 && (
            <TrendSparkline
              providerId={provider.id}
              points={sparkPoints}
              isCritical={willExhaust}
              lang={lang}
              peakBurnRate={provider.peak_burn_rate_per_hour}
            />
          )}
          {(provider.windows ?? []).map((w) => (
            <AnimatedUsageRow
              key={`${provider.id}|${w.label}`}
              win={w}
              lang={lang}
              nowEpoch={nowEpoch}
            />
          ))}
          {extra?.enabled && extra.used_amount != null && extra.limit_amount != null && (
            <div className="reset">
              {t.extraCredits(extra.used_amount, extra.limit_amount, extra.currency)}
            </div>
          )}
        </>
      )}
    </div>
  );
}
