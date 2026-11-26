import { FormEvent, useEffect, useRef, useState } from "react";
import {
  api,
  ApiError,
  OAuthStartResponse,
  ProviderConnectionOut,
  ProviderId,
} from "../api/client";
import { I18N, LangId } from "../i18n/translations";

interface AccountsModalProps {
  lang: LangId;
  connections: ProviderConnectionOut[];
  nowEpoch: number;
  initialProvider?: ProviderId | null;
  onClose: () => void;
  onChanged: () => Promise<void>;
}

export function AccountsModal({
  lang,
  connections,
  nowEpoch,
  initialProvider = null,
  onClose,
  onChanged,
}: AccountsModalProps) {
  const t = I18N[lang];
  const [activeFlow, setActiveFlow] = useState<OAuthStartResponse | null>(null);
  const [codeInput, setCodeInput] = useState("");
  const [busyProvider, setBusyProvider] = useState<ProviderId | null>(null);
  const [copied, setCopied] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (initialProvider) {
      void startFlow(initialProvider);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialProvider]);

  const onChangedRef = useRef(onChanged);
  const unknownTextRef = useRef(t.unknown);
  onChangedRef.current = onChanged;
  unknownTextRef.current = t.unknown;

  const activeProvider = activeFlow?.provider ?? null;
  const activeStateId = activeFlow?.state_id ?? null;
  const activePollInterval = activeFlow?.poll_interval_seconds ?? 5;

  // Automatic background polling ONLY while a Codex Device Code wizard is open
  useEffect(() => {
    if (activeProvider !== "codex" || !activeStateId) return;
    const intervalMs = Math.max(3, activePollInterval) * 1000;
    let cancelled = false;
    let inFlight = false;

    const timer = window.setInterval(async () => {
      if (cancelled || inFlight) return;
      inFlight = true;
      try {
        const res = await api.pollCodexDeviceOAuth(activeStateId);
        if (cancelled) return;
        if (res.status === "completed") {
          setActiveFlow(null);
          await onChangedRef.current();
        } else if (res.status === "failed") {
          setError(res.error || unknownTextRef.current);
          setActiveFlow(null);
        }
      } catch (err) {
        if (!cancelled) {
          setError(err instanceof ApiError ? err.message : String(err));
          setActiveFlow(null);
        }
      } finally {
        inFlight = false;
      }
    }, intervalMs);

    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [activeProvider, activeStateId, activePollInterval]);

  async function startFlow(provider: ProviderId) {
    setError(null);
    setCodeInput("");
    setCopied(false);
    setBusyProvider(provider);
    try {
      const flow = await api.startProviderOAuth(provider);
      setActiveFlow(flow);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setBusyProvider(null);
    }
  }

  async function submitCode(e: FormEvent) {
    e.preventDefault();
    if (!activeFlow || !codeInput.trim()) return;
    setError(null);
    setBusyProvider(activeFlow.provider);
    try {
      const res = await api.completeProviderOAuth(
        activeFlow.provider,
        activeFlow.state_id,
        codeInput.trim()
      );
      if (res.status === "completed") {
        setActiveFlow(null);
        setCodeInput("");
        await onChanged();
      } else {
        setError(res.error || t.unknown);
      }
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setBusyProvider(null);
    }
  }

  async function handleDisconnect(provider: ProviderId) {
    setError(null);
    setBusyProvider(provider);
    try {
      await api.disconnectProviderOAuth(provider);
      if (activeFlow?.provider === provider) {
        setActiveFlow(null);
      }
      await onChanged();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setBusyProvider(null);
    }
  }

  function formatExpiry(expiresAt?: number | null): string | null {
    if (!expiresAt) return null;
    const sec = Math.max(0, expiresAt - nowEpoch);
    const d = Math.floor(sec / 86400);
    const h = Math.floor((sec % 86400) / 3600);
    const m = Math.floor((sec % 3600) / 60);
    return t.accounts.tokenExpiresIn(t.dur(d, h, m));
  }

  async function copyUserCode(code: string) {
    try {
      await navigator.clipboard.writeText(code);
      setCopied(true);
      setTimeout(() => setCopied(false), 1800);
    } catch {
      // Clipboard API may require user gesture or HTTPS
    }
  }

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal-card" onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <div className="modal-title">{t.accounts.modalTitle}</div>
          <button type="button" className="btn" onClick={onClose}>
            {t.accounts.close}
          </button>
        </div>

        {error && <div className="perr">{error}</div>}

        <div className="conn-list">
          {connections.map((conn) => {
            const isFlowOpen = activeFlow?.provider === conn.provider;
            const isBusy = busyProvider === conn.provider;
            const expiryText = conn.connected ? formatExpiry(conn.expires_at) : null;

            return (
              <div key={conn.provider} className="conn-card">
                <div className="conn-top">
                  <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
                    <span className="conn-name">{conn.name}</span>
                    {conn.connected ? (
                      <span
                        className={`badge ${conn.refresh_status === "ok" ? "ok" : "warn"}`}
                      >
                        {conn.refresh_status === "ok"
                          ? t.accounts.connected
                          : t.accounts.tokenExpired}
                      </span>
                    ) : (
                      <span className="badge">{t.accounts.disconnected}</span>
                    )}
                  </div>

                  <div style={{ display: "flex", gap: 8 }}>
                    {isFlowOpen ? (
                      <button
                        type="button"
                        className="btn"
                        onClick={() => setActiveFlow(null)}
                      >
                        {t.accounts.cancelFlow}
                      </button>
                    ) : (
                      <button
                        type="button"
                        className="btn primary"
                        disabled={isBusy}
                        onClick={() => startFlow(conn.provider)}
                      >
                        {conn.connected
                          ? t.accounts.reconnectBtn
                          : t.accounts.connectBtn}
                      </button>
                    )}

                    {conn.connected && (
                      <button
                        type="button"
                        className="btn danger"
                        disabled={isBusy}
                        onClick={() => handleDisconnect(conn.provider)}
                      >
                        {t.accounts.disconnectBtn}
                      </button>
                    )}
                  </div>
                </div>

                {conn.connected && (
                  <div className="conn-meta">
                    {conn.account_label && <span>{conn.account_label}</span>}
                    {conn.auto_refresh && (
                      <span className="badge ok">{t.accounts.autoRefreshOn}</span>
                    )}
                    {expiryText && <span>{expiryText}</span>}
                    {conn.refresh_error && (
                      <span style={{ color: "var(--err)" }}>{conn.refresh_error}</span>
                    )}
                  </div>
                )}

                {isFlowOpen && activeFlow && (
                  <div className="wizard-box">
                    {activeFlow.oauth_method === "device_code" ? (
                      <>
                        <div className="wizard-step">{t.accounts.codexStep}</div>
                        {activeFlow.verification_url && (
                          <a
                            className="btn primary"
                            href={activeFlow.verification_url}
                            target="_blank"
                            rel="noreferrer"
                          >
                            {t.accounts.openAuthPage}
                          </a>
                        )}
                        {activeFlow.user_code && (
                          <div className="device-code-box">
                            <span className="device-code-value">
                              {activeFlow.user_code}
                            </span>
                            <button
                              type="button"
                              className="btn"
                              onClick={() => copyUserCode(activeFlow.user_code!)}
                            >
                              {copied ? t.accounts.copied : t.accounts.copyCode}
                            </button>
                          </div>
                        )}
                        <div className="conn-meta">
                          <span className="dot busy" />
                          <span>{t.accounts.codexPolling}</span>
                        </div>
                      </>
                    ) : (
                      <form
                        onSubmit={submitCode}
                        style={{ display: "flex", flexDirection: "column", gap: 10 }}
                      >
                        <div className="wizard-step">
                          {activeFlow.provider === "claude"
                            ? t.accounts.claudeStep
                            : t.accounts.geminiStep}
                        </div>
                        {activeFlow.provider === "gemini" && (
                          <div className="wizard-note">{t.accounts.geminiNote}</div>
                        )}
                        {activeFlow.authorize_url && (
                          <a
                            className="btn primary"
                            href={activeFlow.authorize_url}
                            target="_blank"
                            rel="noreferrer"
                          >
                            {t.accounts.openAuthPage}
                          </a>
                        )}
                        <input
                          className="pixel-input"
                          type="text"
                          required
                          placeholder={
                            activeFlow.provider === "claude"
                              ? t.accounts.claudePlaceholder
                              : t.accounts.geminiPlaceholder
                          }
                          value={codeInput}
                          onChange={(e) => setCodeInput(e.target.value)}
                        />
                        <button
                          type="submit"
                          className="btn primary"
                          disabled={isBusy || !codeInput.trim()}
                        >
                          {isBusy ? t.accounts.verifying : t.accounts.submitCode}
                        </button>
                      </form>
                    )}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}
