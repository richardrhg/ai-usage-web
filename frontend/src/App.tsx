import { useCallback, useEffect, useState } from "react";
import {
  api,
  ApiError,
  AuthSessionResponse,
  DashboardUsageResponse,
  ProviderId,
} from "./api/client";
import { AccountsModal } from "./components/AccountsModal";
import { AuthPanel } from "./components/AuthPanel";
import { UsagePanel } from "./components/UsagePanel";
import { I18N, LANG_KEY, LangId, THEME_KEY, THEMES } from "./i18n/translations";

function getInitialLang(): LangId {
  const attr = document.documentElement.getAttribute("data-lang");
  return attr === "en" ? "en" : "zh";
}

function getInitialTheme(): string {
  return document.documentElement.getAttribute("data-theme") || "midnight";
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
  const [demoMode, setDemoMode] = useState(false);

  const [dashboard, setDashboard] = useState<DashboardUsageResponse | null>(null);
  const [busy, setBusy] = useState(false);
  const [serverError, setServerError] = useState(false);
  const [cooldown, setCooldown] = useState(0);
  const [nowEpoch, setNowEpoch] = useState(() => Math.floor(Date.now() / 1000));

  const [modalOpen, setModalOpen] = useState(false);
  const [modalInitialProvider, setModalInitialProvider] = useState<ProviderId | null>(
    null
  );

  const t = I18N[lang];

  useEffect(() => {
    const timer = window.setTimeout(() => setBootingAnim(false), 800);
    return () => window.clearTimeout(timer);
  }, []);

  // 1-second clock tick for reset countdowns, age indicator, and manual refresh cooldown
  useEffect(() => {
    const timer = window.setInterval(() => {
      setNowEpoch(Math.floor(Date.now() / 1000));
      setCooldown((prev) => (prev > 0 ? prev - 1 : 0));
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
            : await api.getDashboardUsage(demoMode);
        setDashboard(data);
        setCooldown(data.refresh_cooldown_seconds || 0);
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

  // Bounded O(1) snapshot fetch on login/demo switch + 20s periodic poll
  useEffect(() => {
    if (!session.authenticated && !demoMode) return;
    void loadUsage(false);
    const timer = window.setInterval(() => {
      void loadUsage(false);
    }, 20000);
    return () => window.clearInterval(timer);
  }, [demoMode, loadUsage, session.authenticated]);

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

  const showDashboard = session.authenticated || demoMode;
  const providers = dashboard?.providers ?? [];
  const anyBad =
    serverError || (providers.length > 0 && providers.some((p) => !p.ok));

  function renderStatusText(): string {
    if (serverError) return t.noServer;
    if (!showDashboard) return t.title;
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
        className={`screen ${bootingAnim ? "boot" : ""} ${flickAnim ? "flick" : ""}`}
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
              <div className="topbar-user">
                <span className="badge">
                  {demoMode ? t.demoMode : session.user?.username}
                </span>
              </div>
              <div className="topbar-actions">
                {!demoMode && (
                  <button
                    type="button"
                    className="btn primary"
                    onClick={() => openConnectModal(null)}
                  >
                    {t.accounts.manageBtn}
                  </button>
                )}
                <button type="button" className="btn" onClick={handleLogout}>
                  {demoMode ? t.auth.exitDemo : t.auth.logout}
                </button>
              </div>
            </div>

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
          </>
        )}

        <footer>
          <span className={`dot ${anyBad ? "bad" : ""} ${busy ? "busy" : ""}`} />
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

          {showDashboard && (
            <button
              type="button"
              className="btn"
              disabled={busy || (!demoMode && cooldown > 0)}
              onClick={() => void loadUsage(true)}
            >
              {!demoMode && cooldown > 0 ? t.cooldown(cooldown) : t.refresh}
            </button>
          )}
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
    </>
  );
}
