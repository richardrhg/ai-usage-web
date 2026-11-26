import { useCallback, useEffect, useState } from "react";
import {
  api,
  ApiError,
  CostDailyPointOut,
  CostDays,
  CostDrilldownResponse,
  CostGroupBy,
  CostGroupRowOut,
  CostSummaryResponse,
} from "../api/client";
import { I18N, LangId } from "../i18n/translations";

interface CostDashboardProps {
  lang: LangId;
  demoMode: boolean;
  reloadVersion: number;
  onOpenKeysModal: () => void;
  onSessionExpired: () => void;
  onStatusUpdate: (state: {
    busy: boolean;
    error: boolean;
    cooldown: number;
    lastSyncedAt: number | null;
  }) => void;
  registerSyncHandler: (fn: () => Promise<void>) => void;
}

const DAY_OPTIONS: CostDays[] = [7, 30, 90];
const PAGE_SIZE = 15;

function formatCompactTokens(n: number): string {
  if (n >= 1_000_000_000) return `${(n / 1_000_000_000).toFixed(2)}B`;
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(2)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`;
  return String(n);
}

function formatUsd(val: number): string {
  if (val >= 100) return `$${val.toFixed(2)}`;
  if (val >= 1) return `$${val.toFixed(2)}`;
  if (val === 0) return "$0.00";
  return `$${val.toFixed(4)}`;
}

function formatProviderBadgeName(prov: string | null | undefined): string {
  if (!prov) return "";
  const lower = prov.toLowerCase();
  if (lower.includes("anthropic")) return "Anthropic";
  if (lower.includes("openai")) return "OpenAI";
  if (lower.includes("openrouter")) return "OpenRouter";
  if (lower.includes("custom")) return "Custom";
  if (lower === "multi") return "Multi";
  return prov;
}

function providerColorVar(prov: string): string {
  const lower = prov.toLowerCase();
  if (lower.includes("anthropic")) return "var(--p1)";
  if (lower.includes("openai")) return "var(--p2)";
  if (lower.includes("openrouter")) return "var(--p3)";
  return "var(--warn)";
}

function DailyStackedBarChart({
  series,
  days,
  monthlyBudgetUsd,
  providerFilter,
  lang,
}: {
  series: CostDailyPointOut[];
  days: CostDays;
  monthlyBudgetUsd: number;
  providerFilter: string | null;
  lang: LangId;
}) {
  const tc = I18N[lang].costs;
  const [hoverIdx, setHoverIdx] = useState<number | null>(null);

  const width = 680;
  const height = 148;
  const padLeft = 40;
  const padRight = 10;
  const padTop = 12;
  const padBottom = 22;
  const plotW = width - padLeft - padRight;
  const plotH = height - padTop - padBottom;

  const dailyBudgetRef =
    !providerFilter && monthlyBudgetUsd > 0 ? monthlyBudgetUsd / 30.0 : 0;

  let peakDayUsd = 0;
  let sumUsd = 0;
  const providerSet = new Set<string>();

  for (const pt of series) {
    if (pt.total_cost_usd > peakDayUsd) peakDayUsd = pt.total_cost_usd;
    sumUsd += pt.total_cost_usd;
    if (pt.by_provider) {
      for (const k of Object.keys(pt.by_provider)) {
        if ((pt.by_provider[k] ?? 0) > 0) providerSet.add(k);
      }
    }
  }

  const avgDayUsd = series.length > 0 ? sumUsd / series.length : 0;
  const yMax = Math.max(peakDayUsd * 1.12, dailyBudgetRef * 1.15, 1.0);
  const count = Math.max(1, series.length);
  const slotW = plotW / count;
  const barW = Math.max(2, Math.min(26, slotW * (days === 90 ? 0.72 : 0.76)));

  const orderedProviders = ["anthropic", "openai", "openrouter"].filter((p) =>
    providerSet.has(p)
  );
  for (const p of Array.from(providerSet).sort()) {
    if (!orderedProviders.includes(p)) orderedProviders.push(p);
  }

  const activePoint =
    hoverIdx != null && hoverIdx >= 0 && hoverIdx < series.length
      ? series[hoverIdx]
      : null;

  const yBudget =
    dailyBudgetRef > 0
      ? padTop + plotH - Math.min(1, dailyBudgetRef / yMax) * plotH
      : null;

  return (
    <div className="cost-chart-card">
      <div className="cost-chart-head">
        <div>
          <div className="cost-section-label">{tc.chartTitle(days)}</div>
          <div className="cost-chart-sub">
            {activePoint ? (
              <span className="cost-hover-readout">
                <strong>{activePoint.date}</strong> · {formatUsd(activePoint.total_cost_usd)}
                {orderedProviders.map((prov) => {
                  const val = activePoint.by_provider?.[prov] ?? 0;
                  if (val <= 0) return null;
                  return (
                    <span key={prov} className="cost-hover-chip">
                      {formatProviderBadgeName(prov)} {formatUsd(val)}
                    </span>
                  );
                })}
              </span>
            ) : (
              tc.chartAvg(avgDayUsd, peakDayUsd)
            )}
          </div>
        </div>

        <div className="cost-legend">
          {orderedProviders.map((prov) => (
            <span key={prov} className="cost-legend-item">
              <span
                className="cost-legend-swatch"
                style={{ background: providerColorVar(prov) }}
              />
              {formatProviderBadgeName(prov)}
            </span>
          ))}
        </div>
      </div>

      <svg
        className="cost-chart-svg"
        viewBox={`0 0 ${width} ${height}`}
        preserveAspectRatio="none"
        onMouseLeave={() => setHoverIdx(null)}
        aria-label={tc.chartTitle(days)}
      >
        {[0, 0.5, 1].map((ratio) => {
          const y = padTop + plotH - ratio * plotH;
          const labelVal = ratio * yMax;
          return (
            <g key={ratio}>
              <line
                x1={padLeft}
                y1={y.toFixed(1)}
                x2={width - padRight}
                y2={y.toFixed(1)}
                stroke="var(--line)"
                strokeDasharray={ratio === 0 ? undefined : "3 3"}
                strokeWidth="1"
              />
              <text
                x={padLeft - 6}
                y={(y + 3.5).toFixed(1)}
                textAnchor="end"
                className="cost-svg-axis"
              >
                ${labelVal >= 10 ? labelVal.toFixed(0) : labelVal.toFixed(1)}
              </text>
            </g>
          );
        })}

        {yBudget != null && (
          <line
            x1={padLeft}
            y1={yBudget.toFixed(1)}
            x2={width - padRight}
            y2={yBudget.toFixed(1)}
            stroke="var(--warn)"
            strokeOpacity="0.65"
            strokeDasharray="4 3"
            strokeWidth="1.2"
          />
        )}

        {series.map((pt, idx) => {
          const centerX = padLeft + idx * slotW + slotW / 2;
          const x = centerX - barW / 2;
          let accHeight = 0;
          const isHovered = hoverIdx === idx;

          const segments = orderedProviders
            .map((prov) => {
              const val = pt.by_provider?.[prov] ?? 0;
              if (val <= 0) return null;
              const segH = Math.max(1.2, (val / yMax) * plotH);
              const y = padTop + plotH - accHeight - segH;
              accHeight += segH;
              return { prov, y, segH };
            })
            .filter(
              (s): s is { prov: string; y: number; segH: number } => s !== null
            );

          const showXTick =
            idx === 0 ||
            idx === series.length - 1 ||
            (days === 7
              ? true
              : days === 30
              ? idx % 6 === 0
              : idx % 18 === 0);

          return (
            <g
              key={pt.date}
              onMouseEnter={() => setHoverIdx(idx)}
              style={{ cursor: "crosshair" }}
            >
              <rect
                x={(padLeft + idx * slotW).toFixed(1)}
                y={padTop}
                width={slotW.toFixed(1)}
                height={plotH}
                fill={isHovered ? "rgba(255, 255, 255, 0.06)" : "transparent"}
              />
              {segments.map((seg) => (
                <rect
                  key={seg.prov}
                  x={x.toFixed(1)}
                  y={Math.max(padTop, seg.y).toFixed(1)}
                  width={barW.toFixed(1)}
                  height={seg.segH.toFixed(1)}
                  rx="1.5"
                  fill={providerColorVar(seg.prov)}
                  opacity={hoverIdx == null || isHovered ? 0.92 : 0.55}
                />
              ))}
              {showXTick && (
                <text
                  x={centerX.toFixed(1)}
                  y={height - 5}
                  textAnchor={
                    idx === 0
                      ? "start"
                      : idx === series.length - 1
                      ? "end"
                      : "middle"
                  }
                  className="cost-svg-axis"
                >
                  {pt.date.slice(5)}
                </text>
              )}
            </g>
          );
        })}
      </svg>
    </div>
  );
}

export function CostDashboard({
  lang,
  demoMode,
  reloadVersion,
  onOpenKeysModal,
  onSessionExpired,
  onStatusUpdate,
  registerSyncHandler,
}: CostDashboardProps) {
  const tc = I18N[lang].costs;

  const [days, setDays] = useState<CostDays>(30);
  const [groupBy, setGroupBy] = useState<CostGroupBy>("provider");
  const [providerFilter, setProviderFilter] = useState<string | null>(null);
  const [orderBy, setOrderBy] = useState<string>("cost_usd desc");
  const [skip, setSkip] = useState<number>(0);

  const [summary, setSummary] = useState<CostSummaryResponse | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);

  const [expandedRowKey, setExpandedRowKey] = useState<string | null>(null);
  const [drilldownCache, setDrilldownCache] = useState<
    Record<string, CostDrilldownResponse>
  >({});
  const [drilldownBusyKey, setDrilldownBusyKey] = useState<string | null>(null);
  const [drilldownError, setDrilldownError] = useState<string | null>(null);

  // Reset pagination & expanded row when filter/group dimensions change
  useEffect(() => {
    setExpandedRowKey(null);
    setDrilldownError(null);
  }, [days, groupBy, providerFilter, demoMode, reloadVersion]);

  // Invalidate cached lazy drilldowns whenever managed keys/budgets or mode change
  useEffect(() => {
    setDrilldownCache({});
  }, [demoMode, reloadVersion]);

  const fetchSummary = useCallback(async () => {
    setLoading(true);
    setError(null);
    onStatusUpdate({
      busy: true,
      error: false,
      cooldown: 0,
      lastSyncedAt: null,
    });
    try {
      const res = await api.getCostSummary({
        days,
        group_by: groupBy,
        provider: providerFilter,
        orderby: orderBy,
        top: PAGE_SIZE,
        skip,
        demo: demoMode,
      });
      setSummary(res);
      onStatusUpdate({
        busy: false,
        error: false,
        cooldown: res.sync_cooldown_remaining ?? 0,
        lastSyncedAt: res.last_synced_at ?? null,
      });
    } catch (err) {
      if (err instanceof ApiError && err.status === 401 && !demoMode) {
        onSessionExpired();
        return;
      }
      const msg = err instanceof ApiError ? err.message : String(err);
      setError(msg);
      onStatusUpdate({
        busy: false,
        error: true,
        cooldown: 0,
        lastSyncedAt: null,
      });
    } finally {
      setLoading(false);
    }
  }, [
    days,
    demoMode,
    groupBy,
    onSessionExpired,
    onStatusUpdate,
    orderBy,
    providerFilter,
    skip,
  ]);

  // Single O(1) summary query when view or filter parameters change
  useEffect(() => {
    void fetchSummary();
  }, [fetchSummary, reloadVersion]);

  const triggerManualSync = useCallback(async () => {
    if (demoMode) {
      await fetchSummary();
      return;
    }
    setLoading(true);
    setError(null);
    onStatusUpdate({
      busy: true,
      error: false,
      cooldown: 0,
      lastSyncedAt: summary?.last_synced_at ?? null,
    });
    try {
      const res = await api.syncCostUsage({
        days,
        group_by: groupBy,
        provider: providerFilter,
        orderby: orderBy,
        top: PAGE_SIZE,
        skip,
      });
      setSummary(res.summary);
      setDrilldownCache({});
      setExpandedRowKey(null);
      if (!res.ok && res.key_errors) {
        const errs = Object.entries(res.key_errors).map(
          ([k, v]) => `${k}: ${v}`
        );
        if (errs.length > 0) setError(errs.join(" | "));
      }
      onStatusUpdate({
        busy: false,
        error: !res.ok,
        cooldown: res.sync_cooldown_remaining ?? 0,
        lastSyncedAt: res.summary.last_synced_at ?? null,
      });
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        onSessionExpired();
        return;
      }
      setError(err instanceof ApiError ? err.message : String(err));
      onStatusUpdate({
        busy: false,
        error: true,
        cooldown: 0,
        lastSyncedAt: summary?.last_synced_at ?? null,
      });
    } finally {
      setLoading(false);
    }
  }, [
    days,
    demoMode,
    fetchSummary,
    groupBy,
    onSessionExpired,
    onStatusUpdate,
    orderBy,
    providerFilter,
    skip,
    summary?.last_synced_at,
  ]);

  useEffect(() => {
    registerSyncHandler(triggerManualSync);
  }, [registerSyncHandler, triggerManualSync]);

  async function handleToggleRow(row: CostGroupRowOut) {
    if (expandedRowKey === row.group_key) {
      setExpandedRowKey(null);
      return;
    }
    setExpandedRowKey(row.group_key);
    setDrilldownError(null);

    const cacheId = `${demoMode ? "demo" : "live"}|${days}|${groupBy}|${
      providerFilter || "all"
    }|${row.group_key}`;
    if (drilldownCache[cacheId]) {
      return;
    }

    setDrilldownBusyKey(row.group_key);
    try {
      const detail = await api.getCostDrilldown({
        group_by: groupBy,
        group_value: row.group_key,
        days,
        provider: providerFilter,
        demo: demoMode,
      });
      setDrilldownCache((prev) => ({ ...prev, [cacheId]: detail }));
    } catch (err) {
      setDrilldownError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setDrilldownBusyKey(null);
    }
  }

  if (!summary && loading) {
    return <div className="empty-box">{tc.loadingSummary}</div>;
  }

  if (!summary && error) {
    return <div className="perr">{error}</div>;
  }

  if (!summary) {
    return null;
  }

  const totals = summary.totals;
  const budget = summary.budget;
  const groups = summary.groups ?? [];
  const dailySeries = summary.daily_series ?? [];
  const availProviders = summary.available_providers ?? [];

  const cacheHitPct =
    totals.input_tokens + totals.cached_tokens > 0
      ? (totals.cached_tokens / (totals.input_tokens + totals.cached_tokens)) *
        100
      : 0;

  const hasBudget = budget.monthly_budget_usd > 0;
  const utilPct = Math.max(0, Math.min(100, budget.utilization_pct ?? 0));
  const projPct = Math.max(0, Math.min(100, budget.projected_pct ?? 0));
  const effectivePct = Math.max(
    budget.utilization_pct ?? 0,
    budget.projected_pct ?? 0
  );
  const budgetStateCls = !hasBudget
    ? ""
    : effectivePct >= 100
    ? "crit"
    : effectivePct >= 75
    ? "warn"
    : "ok";
  const budgetBadgeText = !hasBudget
    ? tc.budgetUnset
    : effectivePct >= 100
    ? tc.budgetExceed
    : effectivePct >= 75
    ? tc.budgetWarn
    : tc.budgetSafe;

  const pageStart = summary.total_count === 0 ? 0 : skip + 1;
  const pageEnd = Math.min(summary.total_count, skip + groups.length);

  return (
    <div className="cost-dashboard">
      {error && <div className="perr">{error}</div>}

      {/* Top Filter Toolbar: Days (7d / 30d / 90d) & Provider Filter */}
      <div className="cost-toolbar">
        <div className="pill-group">
          {DAY_OPTIONS.map((d) => (
            <button
              key={d}
              type="button"
              className={`pill-btn ${days === d ? "active" : ""}`}
              onClick={() => {
                setDays(d);
                setSkip(0);
              }}
            >
              {d}D
            </button>
          ))}
        </div>

        <div className="pill-group">
          <button
            type="button"
            className={`pill-btn ${providerFilter === null ? "active" : ""}`}
            onClick={() => {
              setProviderFilter(null);
              setSkip(0);
            }}
          >
            {tc.allProviders}
          </button>
          {availProviders.map((prov) => (
            <button
              key={prov}
              type="button"
              className={`pill-btn ${providerFilter === prov ? "active" : ""}`}
              onClick={() => {
                setProviderFilter(prov);
                setSkip(0);
              }}
            >
              {formatProviderBadgeName(prov)}
            </button>
          ))}
        </div>
      </div>

      {/* 4 Summary KPI Cards */}
      <div className="cost-kpi-grid">
        <div className="cost-kpi-card highlight">
          <div className="cost-kpi-label">{tc.windowSpend(days)}</div>
          <div className="cost-kpi-value usd">{formatUsd(totals.cost_usd)}</div>
          <div className="cost-kpi-sub">
            {tc.activeModelsKeys(summary.bound_keys_count ?? 0)}
          </div>
        </div>

        <div className="cost-kpi-card">
          <div className="cost-kpi-label">{tc.inputTokens}</div>
          <div className="cost-kpi-value">
            {formatCompactTokens(totals.input_tokens)}
          </div>
          <div className="cost-kpi-sub">
            {totals.input_tokens.toLocaleString()}
          </div>
        </div>

        <div className="cost-kpi-card">
          <div className="cost-kpi-label">{tc.outputTokens}</div>
          <div className="cost-kpi-value">
            {formatCompactTokens(totals.output_tokens)}
          </div>
          <div className="cost-kpi-sub">
            {totals.output_tokens.toLocaleString()}
          </div>
        </div>

        <div className="cost-kpi-card">
          <div className="cost-kpi-label">{tc.cachedTokens}</div>
          <div className="cost-kpi-value">
            {formatCompactTokens(totals.cached_tokens)}
          </div>
          <div className="cost-kpi-sub">{tc.cacheShareLabel(cacheHitPct)}</div>
        </div>
      </div>

      {/* Monthly Budget Progress & EOM Linear Forecast */}
      <div className="cost-budget-card">
        <div className="row-top">
          <div className="cost-budget-title-wrap">
            <span className="label">{tc.budgetTitle(budget.year_month)}</span>
            <span className={`badge ${budgetStateCls}`}>{budgetBadgeText}</span>
          </div>
          {hasBudget ? (
            <div className="pct cost-budget-pct">
              {(budget.utilization_pct ?? 0).toFixed(0)}%
            </div>
          ) : !demoMode ? (
            <button
              type="button"
              className="btn"
              onClick={onOpenKeysModal}
            >
              {tc.manageKeysBtn}
            </button>
          ) : null}
        </div>

        {hasBudget ? (
          <>
            <div className="track cost-budget-track">
              <div
                className={`fill ${
                  utilPct >= 90 ? "crit" : utilPct >= 75 ? "warn" : ""
                }`}
                style={{ width: `${utilPct}%` }}
              />
              {projPct > utilPct && (
                <div
                  className="budget-proj-marker"
                  style={{ left: `${Math.min(99.2, projPct)}%` }}
                  title={tc.projectedSpend(budget.projected_month_usd)}
                />
              )}
            </div>
            <div className="row-foot">
              <span>
                {tc.mtdSpend(
                  budget.month_to_date_usd,
                  budget.monthly_budget_usd
                )}
              </span>
              <span
                className={
                  budget.will_exceed_budget ? "cost-proj-crit" : "cost-proj-ok"
                }
              >
                {tc.projectedSpend(budget.projected_month_usd)}
                {budget.projected_pct != null
                  ? ` (${budget.projected_pct.toFixed(0)}%)`
                  : ""}
              </span>
            </div>
          </>
        ) : (
          <div className="row-foot">
            <span>{tc.mtdNoLimit(budget.month_to_date_usd)}</span>
            <span>{tc.budgetSetHint}</span>
          </div>
        )}
      </div>

      {/* Pure SVG Daily Stacked Cost Bar Chart */}
      <DailyStackedBarChart
        series={dailySeries}
        days={days}
        monthlyBudgetUsd={budget.monthly_budget_usd}
        providerFilter={providerFilter}
        lang={lang}
      />

      {/* Single-Query GROUP BY Breakdown Table (By Provider / By Model / By Key) */}
      <div className="cost-table-card">
        <div className="cost-table-toolbar">
          <div className="pill-group">
            <button
              type="button"
              className={`pill-btn ${groupBy === "provider" ? "active" : ""}`}
              onClick={() => {
                setGroupBy("provider");
                setSkip(0);
              }}
            >
              {tc.groupByProvider}
            </button>
            <button
              type="button"
              className={`pill-btn ${groupBy === "model" ? "active" : ""}`}
              onClick={() => {
                setGroupBy("model");
                setSkip(0);
              }}
            >
              {tc.groupByModel}
            </button>
            <button
              type="button"
              className={`pill-btn ${groupBy === "key" ? "active" : ""}`}
              onClick={() => {
                setGroupBy("key");
                setSkip(0);
              }}
            >
              {tc.groupByKey}
            </button>
          </div>

          <select
            className="cost-sort-select"
            value={orderBy}
            onChange={(e) => {
              setOrderBy(e.target.value);
              setSkip(0);
            }}
          >
            <option value="cost_usd desc">{tc.sortCostDesc}</option>
            <option value="total_tokens desc">{tc.sortTokensDesc}</option>
            <option value="group_key asc">{tc.sortNameAsc}</option>
          </select>
        </div>

        <div className="cost-table-hint">{tc.clickRowHint}</div>

        {groups.length === 0 ? (
          <div className="empty-box" style={{ padding: "18px 8px" }}>
            <div className="empty-title">{tc.emptyTitle}</div>
            <div className="empty-hint">{tc.emptyHint}</div>
            {!demoMode && (
              <button
                type="button"
                className="btn primary"
                onClick={onOpenKeysModal}
              >
                {tc.manageKeysBtn}
              </button>
            )}
          </div>
        ) : (
          <div className="cost-group-list">
            {groups.map((row) => {
              const isOpen = expandedRowKey === row.group_key;
              const cacheId = `${
                demoMode ? "demo" : "live"
              }|${days}|${groupBy}|${providerFilter || "all"}|${row.group_key}`;
              const detail = drilldownCache[cacheId];
              const isRowLoading = drilldownBusyKey === row.group_key;

              return (
                <div
                  key={row.group_key}
                  className={`cost-group-item ${isOpen ? "expanded" : ""}`}
                >
                  <button
                    type="button"
                    className="cost-group-row-btn"
                    aria-expanded={isOpen}
                    onClick={() => void handleToggleRow(row)}
                  >
                    <div className="cost-row-main">
                      <span className="cost-chevron">{isOpen ? "▾" : "▸"}</span>
                      <div className="cost-row-title-block">
                        <div className="cost-row-title-line">
                          <span className="cost-row-name">
                            {row.group_label}
                          </span>
                          {row.provider && groupBy !== "provider" && (
                            <span className="badge">
                              {formatProviderBadgeName(row.provider)}
                            </span>
                          )}
                        </div>
                        <div className="cost-row-tokens">
                          <span>
                            In {formatCompactTokens(row.input_tokens)}
                          </span>
                          <span>·</span>
                          <span>
                            Out {formatCompactTokens(row.output_tokens)}
                          </span>
                          <span>·</span>
                          <span>
                            Cache {formatCompactTokens(row.cached_tokens)}
                          </span>
                          <span>·</span>
                          <span>{tc.recordDays(row.record_days)}</span>
                        </div>
                      </div>
                    </div>

                    <div className="cost-row-metrics">
                      <div className="cost-row-usd">
                        {formatUsd(row.cost_usd)}
                      </div>
                      <div className="cost-share-wrap">
                        <div className="cost-share-bar">
                          <div
                            className="cost-share-fill"
                            style={{
                              width: `${Math.min(100, row.share_pct)}%`,
                              background: providerColorVar(
                                row.provider || row.group_key
                              ),
                            }}
                          />
                        </div>
                        <span className="cost-share-pct">
                          {row.share_pct.toFixed(1)}%
                        </span>
                      </div>
                    </div>
                  </button>

                  {isOpen && (
                    <div className="cost-drilldown-box">
                      {isRowLoading ? (
                        <div className="conn-meta">{tc.loadingDrilldown}</div>
                      ) : drilldownError && !detail ? (
                        <div className="perr">{drilldownError}</div>
                      ) : detail ? (
                        <div className="cost-drilldown-grid">
                          <div>
                            <div className="cost-drill-heading">
                              {detail.sub_breakdown_kind === "key"
                                ? tc.subByKey
                                : tc.subByModel}
                            </div>
                            <div className="cost-drill-list">
                              {(detail.sub_breakdown ?? []).map((sub) => (
                                <div key={sub.key} className="cost-drill-row">
                                  <div className="cost-drill-left">
                                    <span className="cost-drill-name">
                                      {sub.label}
                                    </span>
                                    <span className="cost-drill-tok">
                                      {formatCompactTokens(sub.total_tokens)} tok
                                    </span>
                                  </div>
                                  <div className="cost-drill-right">
                                    <strong>{formatUsd(sub.cost_usd)}</strong>
                                    <span>({sub.share_pct.toFixed(1)}%)</span>
                                  </div>
                                </div>
                              ))}
                            </div>
                          </div>

                          <div>
                            <div className="cost-drill-heading">
                              {tc.recentDailyTitle}
                            </div>
                            <div className="cost-drill-list">
                              {(detail.daily_breakdown ?? [])
                                .slice(0, 7)
                                .map((d) => (
                                  <div key={d.date} className="cost-drill-row">
                                    <div className="cost-drill-left">
                                      <span className="cost-drill-name">
                                        {d.date}
                                      </span>
                                      <span className="cost-drill-tok">
                                        {formatCompactTokens(d.total_tokens)} tok
                                      </span>
                                    </div>
                                    <div className="cost-drill-right">
                                      <strong>{formatUsd(d.cost_usd)}</strong>
                                    </div>
                                  </div>
                                ))}
                            </div>
                          </div>
                        </div>
                      ) : null}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        )}

        {summary.total_count > 0 && (
          <div className="cost-pagination">
            <span>{tc.pageInfo(pageStart, pageEnd, summary.total_count)}</span>
            {summary.total_count > PAGE_SIZE && (
              <div style={{ display: "flex", gap: 6 }}>
                <button
                  type="button"
                  className="btn"
                  disabled={skip <= 0 || loading}
                  onClick={() => setSkip((prev) => Math.max(0, prev - PAGE_SIZE))}
                >
                  {tc.prevPage}
                </button>
                <button
                  type="button"
                  className="btn"
                  disabled={skip + PAGE_SIZE >= summary.total_count || loading}
                  onClick={() => setSkip((prev) => prev + PAGE_SIZE)}
                >
                  {tc.nextPage}
                </button>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
