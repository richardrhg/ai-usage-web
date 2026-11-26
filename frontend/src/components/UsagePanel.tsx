import { useEffect, useRef, useState } from "react";
import type { ProviderUsageOut, UsageWindowOut } from "../api/client";
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

function AnimatedUsageRow({
  win,
  lang,
  nowEpoch,
}: {
  win: UsageWindowOut;
  lang: LangId;
  nowEpoch: number;
}) {
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

  const cls = target >= 90 ? "crit" : target >= 70 ? "warn" : "";
  const resetText =
    win.reset_epoch != null ? formatDuration(win.reset_epoch - nowEpoch, lang) : null;

  return (
    <div className="row">
      <div className="row-top">
        <div className="label">{resolveWindowLabel(win, lang)}</div>
        <div className="pct">{displayPct}%</div>
      </div>
      <div className="track">
        <div className={`fill ${cls}`} style={{ width: `${barWidth}%` }} />
      </div>
      {resetText && <div className="reset">{resetText}</div>}
    </div>
  );
}

export function UsagePanel({ provider, lang, nowEpoch }: UsagePanelProps) {
  const t = I18N[lang];
  const extra = provider.extra_usage;

  return (
    <div className={`panel p-${provider.id}`}>
      <div className="phead">
        <div className="pname">{provider.name}</div>
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
