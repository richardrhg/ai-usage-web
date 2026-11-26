import { useCallback, useEffect, useRef, useState } from "react";
import {
  api,
  ApiError,
  AuthSessionResponse,
  DashboardUsageResponse,
  ProviderId,
} from "./api/client";
import { AccountsModal } from "./components/AccountsModal";
import { AlertsModal } from "./components/AlertsModal";
import { AuthPanel } from "./components/AuthPanel";
import { CostDashboard } from "./components/CostDashboard";
import { CostKeysModal } from "./components/CostKeysModal";
import { UsagePanel } from "./components/UsagePanel";
import { I18N, LANG_KEY, LangId, THEME_KEY, THEMES } from "./i18n/translations";

type DashboardView = "usage" | "costs";

function getInitialLang(): LangId {
  const attr = document.documentElement.getAttribute("data-lang");
  return attr === "en" ? "en" : "zh";
}

function getInitialTheme(): string {
  return document.documentElement.getAttribute("data-theme") || "midnight";
}

function getInitialDemoMode(): boolean {
  try {
    const params = new URLSearchParams(window.location.search);
    const val = (params.get("demo") || "").toLowerCase();
    return val === "1" || val === "true";
  } catch {
    return false;
  }
}

function getInitialView(): DashboardView {
  try {
    const params = new URLSearchParams(window.location.search);
    const val = (params.get("view") || params.get("tab") || "").toLowerCase();
    return val === "costs" || val === "billing" ? "costs" : "usage";
  } catch {
    return "usage";
  }
}

export function App() {
  const [lang, setLang] = useState<LangId>(getInitialLang);
  const [theme, setTheme] = useState<string>(getInitialTheme);
  const [bootingAnim, setBootingAnim] = useState(true);
  const [flickAnim, setFlickAnim] = useState(false);

  const [authReady, setAuthReady] = useState(false);
  const [session, setSession] = useState<AuthSessionResponse>({
    authenticated: false,
    user: null,
    allow_registration: true,
  });
  const [demoMode, setDemoMode] = useState<boolean>(getInitialDemoMode);
  const [activeView, setActiveView] = useState<DashboardView>(getInitialView);

  const [dashboard, setDashboard] = useState<DashboardUsageResponse | null>(null);
  const [busy, setBusy] = useState(false);
  const [serverError, setServerError] = useState(false);
  const [cooldown, setCooldown] = useState(0);
  const [nowEpoch, setNowEpoch] = useState(() => Math.floor(Date.now() / 1000));

  // Cost tab footer status & sync wiring
  const [costBusy, setCostBusy] = useState(false);
  const [costError, setCostError] = useState(false);
  const [costCooldown, setCostCooldown] = useState(0);
  const [costLastSyncedAt, setCostLastSyncedAt] = useState<number | null>(null);
  const [costReloadVersion, setCostReloadVersion] = useState(0);
  const costSyncHandlerRef = useRef<(() => Promise<void>) | null>(null);

  const [modalOpen, setModalOpen] = useState(false);
  const [modalInitialProvider, setModalInitialProvider] = useState<ProviderId | null>(
    null
  );
  const [alertsModalOpen, setAlertsModalOpen] = useState(false);
  const [costKeysModalOpen, setCostKeysModalOpen] = useState(false);

  const t = I18N[lang];

  useEffect(() => {
    const timer = window.setTimeout(() => setBootingAnim(false), 800);
    return () => window.clearTimeout(timer);
  }, []);

  // 1-second clock tick for reset countdowns, age indicator, and manual refresh/sync cooldowns
  useEffect(() => {
    const timer = window.setInterval(() => {
      setNowEpoch(Math.floor(Date.now() / 1000));
      setCooldown((prev) => (prev > 0 ? prev - 1 : 0));
      setCostCooldown((prev) => (prev > 0 ? prev - 1 : 0));
    }, 1000);
    return () => window.clearInterval(timer);
  }, []);

  const applyLang = useCallback((next: LangId) => {
    setLang(next);
    document.documentElement.setAttribute("data-lang", next);
    document.documentElement.setAttribute("lang", next === "zh" ? "zh-Hant" : "en");
    document.title = I18N[next].title;
    try {
      localStorage.setItem(LANG_KEY, next);
    } catch {
      // Ignore storage quota errors
    }
  }, []);

  const applyTheme = useCallback((nextId: string) => {
    const valid = THEMES.find((x) => x.id === nextId) || THEMES[0];
    setTheme(valid.id);
    document.documentElement.setAttribute("data-theme", valid.id);
    try {
      localStorage.setItem(THEME_KEY, valid.id);
    } catch {
      // Ignore storage quota errors
    }
    if (!window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
      setFlickAnim(false);
      requestAnimationFrame(() => {
        setFlickAnim(true);
        window.setTimeout(() => setFlickAnim(false), 300);
      });
    }
  }, []);

  // Keyboard shortcuts: T (theme cycle) & L (language toggle), ignored when typing in inputs
  useEffect(() => {
    function onKeyDown(e: KeyboardEvent) {
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      const tag = (e.target as HTMLElement | null)?.tagName;
      if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;
      const k = e.key.toLowerCase();
      if (k === "t") {
        const idx = THEMES.findIndex((x) => x.id === theme);
        applyTheme(THEMES[(idx + 1) % THEMES.length].id);
      } else if (k === "l") {
        applyLang(lang === "zh" ? "en" : "zh");
      }
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [applyLang, applyTheme, lang, theme]);

  // Initial session check (1 bounded call)
  useEffect(() => {
    let active = true;
    api
      .getAuthSession()
      .then((res) => {
        if (!active) return;
        setSession(res);
        setAuthReady(true);
      })
      .catch(() => {
        if (!active) return;
        setServerError(true);
        setAuthReady(true);
      });
    return () => {
      active = false;
    };
  }, []);

  const loadUsage = useCallback(
    async (isManualRefresh = false) => {
      if (!session.authenticated && !demoMode) return;
      setBusy(true);
      setServerError(false);
      try {
        const data =
          isManualRefresh && !demoMode
            ? await api.refreshDashboardUsage()
            : await api.getDashboardUsage(demoMode, false);
        setDashboard(data);
        setCooldown(
          data.refresh_cooldown_remaining ?? data.refresh_cooldown_seconds ?? 0
        );
      } catch (err) {
        if (err instanceof ApiError && err.status === 401 && !demoMode) {
          setSession((prev) => ({ ...prev, authenticated: false, user: null }));
          setDashboard(null);
        } else {
          setServerError(true);
        }
      } finally {
        setBusy(false);
      }
    },
    [demoMode, session.authenticated]
  );

  // Bounded O(1) snapshot fetch ONLY when on subscription usage view
  useEffect(() => {
    if ((!session.authenticated && !demoMode) || activeView !== "usage") return;
    void loadUsage(false);
    const timer = window.setInterval(() => {
      void loadUsage(false);
    }, 20000);
    return () => window.clearInterval(timer);
  }, [activeView, demoMode, loadUsage, session.authenticated]);

  async function handleLogout() {
    if (demoMode) {
      setDemoMode(false);
      setDashboard(null);
      return;
    }
    await api.logout();
    setSession((prev) => ({ ...prev, authenticated: false, user: null }));
    setDashboard(null);
  }

  function openConnectModal(provider: ProviderId | null = null) {
    setModalInitialProvider(provider);
    setModalOpen(true);
  }

  const handleModalClose = useCallback(() => {
    setModalOpen(false);
    setModalInitialProvider(null);
  }, []);

  const handleModalChanged = useCallback(() => loadUsage(false), [loadUsage]);
  const handleAlertsClose = useCallback(() => setAlertsModalOpen(false), []);
  const handleCostKeysClose = useCallback(() => setCostKeysModalOpen(false), []);
  const handleCostKeysChanged = useCallback(
    () => setCostReloadVersion((v) => v + 1),
    []
  );
  const handleSessionExpired = useCallback(() => {
    setSession((prev) => ({ ...prev, authenticated: false, user: null }));
    setDashboard(null);
  }, []);

  const handleCostStatusUpdate = useCallback(
    (meta: {
      busy: boolean;
      error: boolean;
      cooldown: number;
      lastSyncedAt: number | null;
    }) => {
      setCostBusy(meta.busy);
      setCostError(meta.error);
      if (meta.cooldown > 0) setCostCooldown(meta.cooldown);
      setCostLastSyncedAt(meta.lastSyncedAt);
    },
    []
  );

  const registerCostSyncHandler = useCallback((fn: () => Promise<void>) => {
    costSyncHandlerRef.current = fn;
  }, []);

  const showDashboard = session.authenticated || demoMode;
  const providers = dashboard?.providers ?? [];
  const isCostView = showDashboard && activeView === "costs";

  const anyBad = isCostView
    ? costError
    : serverError || (providers.length > 0 && providers.some((p) => !p.ok));
  const dotBusy = isCostView ? costBusy : busy;

  function renderStatusText(): string {
    if (!showDashboard) return serverError ? t.noServer : t.title;
    if (isCostView) {
      if (costError) return t.unknown;
      if (demoMode) return t.demoMode;
      if (costBusy) return t.connecting;
      if (costLastSyncedAt != null) {
        const age = Math.max(0, nowEpoch - costLastSyncedAt);
        return t.ok + t.age(age);
      }
      return t.ok;
    }
    if (serverError) return t.noServer;
    if (!dashboard) return t.connecting;
    if (demoMode) return t.demoMode;
    if (!providers.length) return t.noData;
    if (!dashboard.fetched_at) return t.waiting;
    const age = Math.max(0, nowEpoch - dashboard.fetched_at);
    const badCount = providers.filter((p) => !p.ok).length;
    return (badCount ? t.errors(badCount) : t.ok) + t.age(age);
  }

  return (
    <>
      <div
        className={`screen ${isCostView ? "screen-wide" : ""} ${
          bootingAnim ? "boot" : ""
        } ${flickAnim ? "flick" : ""}`}
      >
        <div className="sweep" />

        {!authReady ? (
          <div className="empty-box">{t.connecting}</div>
        ) : !showDashboard ? (
          <AuthPanel
            lang={lang}
            allowRegistration={session.allow_registration}
            onAuthenticated={(nextSession) => {
              setDemoMode(false);
              setSession(nextSession);
            }}
            onEnterDemo={() => {
              setDemoMode(true);
            }}
          />
        ) : (
          <>
            <div className="topbar">
              <div className="topbar-left">
                <span className="badge">
                  {demoMode ? t.demoMode : session.user?.username}
                </span>
                <nav className="view-switch" aria-label="Dashboard View">
                  <button
                    type="button"
                    className={`view-tab ${activeView === "usage" ? "active" : ""}`}
                    onClick={() => setActiveView("usage")}
                  >
                    {t.nav.subscriptions}
                  </button>
                  <button
                    type="button"
                    className={`view-tab ${activeView === "costs" ? "active" : ""}`}
                    onClick={() => setActiveView("costs")}
                  >
                    {t.nav.costs}
                  </button>
                </nav>
              </div>

              <div className="topbar-actions">
                {!demoMode && (
                  <>
                    {activeView === "usage" ? (
                      <button
                        type="button"
                        className="btn primary"
                        onClick={() => openConnectModal(null)}
                      >
                        {t.accounts.manageBtn}
                      </button>
                    ) : (
                      <button
                        type="button"
                        className="btn primary"
                        onClick={() => setCostKeysModalOpen(true)}
                      >
                        {t.costs.manageKeysBtn}
                      </button>
                    )}
                    <button
                      type="button"
                      className="btn"
                      onClick={() => setAlertsModalOpen(true)}
                    >
                      {t.alerts.openBtn}
                    </button>
                  </>
                )}
                <button type="button" className="btn" onClick={handleLogout}>
                  {demoMode ? t.auth.exitDemo : t.auth.logout}
                </button>
              </div>
            </div>

            {activeView === "costs" ? (
              <CostDashboard
                lang={lang}
                demoMode={demoMode}
                reloadVersion={costReloadVersion}
                onOpenKeysModal={() => setCostKeysModalOpen(true)}
                onSessionExpired={handleSessionExpired}
                onStatusUpdate={handleCostStatusUpdate}
                registerSyncHandler={registerCostSyncHandler}
              />
            ) : (
              <div id="panels">
                {providers.length > 0 ? (
                  providers.map((p) => (
                    <UsagePanel
                      key={p.id}
                      provider={p}
                      lang={lang}
                      nowEpoch={nowEpoch}
                    />
                  ))
                ) : (
                  <div className="empty-box">
                    <div className="empty-title">{t.noData}</div>
                    <div className="empty-hint">{t.noDataHint}</div>
                    {!demoMode && (
                      <div className="quick-connect-row">
                        <button
                          type="button"
                          className="btn primary"
                          onClick={() => openConnectModal("claude")}
                        >
                          + Claude
                        </button>
                        <button
                          type="button"
                          className="btn primary"
                          onClick={() => openConnectModal("codex")}
                        >
                          + Codex
                        </button>
                        <button
                          type="button"
                          className="btn primary"
                          onClick={() => openConnectModal("gemini")}
                        >
                          + Gemini
                        </button>
                      </div>
                    )}
                  </div>
                )}
              </div>
            )}
          </>
        )}

        <footer>
          <span className={`dot ${anyBad ? "bad" : ""} ${dotBusy ? "busy" : ""}`} />
          <span>{renderStatusText()}</span>

          <span className="swatches">
            {THEMES.map((th) => (
              <button
                key={th.id}
                type="button"
                className="sw"
                title={th.name}
                aria-label={th.name}
                aria-pressed={theme === th.id}
                style={
                  {
                    "--sw-a": th.a,
                    "--sw-b": th.b,
                  } as React.CSSProperties
                }
                onClick={() => applyTheme(th.id)}
              />
            ))}
          </span>

          <button
            type="button"
            className="btn lang-btn"
            title={t.switchToLang}
            onClick={() => applyLang(lang === "zh" ? "en" : "zh")}
          >
            {t.otherLang}
          </button>

          {showDashboard &&
            (activeView === "costs" ? (
              <button
                type="button"
                className="btn"
                disabled={costBusy || (!demoMode && costCooldown > 0)}
                onClick={() => {
                  if (costSyncHandlerRef.current) {
                    void costSyncHandlerRef.current();
                  }
                }}
              >
                {!demoMode && costCooldown > 0
                  ? t.costs.syncCooldown(costCooldown)
                  : demoMode
                  ? t.refresh
                  : t.costs.syncBtn}
              </button>
            ) : (
              <button
                type="button"
                className="btn"
                disabled={busy || (!demoMode && cooldown > 0)}
                onClick={() => void loadUsage(true)}
              >
                {!demoMode && cooldown > 0 ? t.cooldown(cooldown) : t.refresh}
              </button>
            ))}
        </footer>
      </div>

      {modalOpen && !demoMode && dashboard && (
        <AccountsModal
          lang={lang}
          connections={dashboard.connections}
          nowEpoch={nowEpoch}
          initialProvider={modalInitialProvider}
          onClose={handleModalClose}
          onChanged={handleModalChanged}
        />
      )}

      {alertsModalOpen && !demoMode && (
        <AlertsModal lang={lang} onClose={handleAlertsClose} />
      )}

      {costKeysModalOpen && !demoMode && (
        <CostKeysModal
          lang={lang}
          nowEpoch={nowEpoch}
          onClose={handleCostKeysClose}
          onChanged={handleCostKeysChanged}
        />
      )}
    </>
  );
}
