import { FormEvent, useEffect, useState } from "react";
import {
  api,
  ApiError,
  CostApiKeyOut,
  CostKeyProvider,
} from "../api/client";
import { I18N, LangId } from "../i18n/translations";

interface CostKeysModalProps {
  lang: LangId;
  nowEpoch: number;
  onClose: () => void;
  onChanged: () => void;
}

const KEY_PROVIDERS: CostKeyProvider[] = [
  "anthropic_admin",
  "openai_org",
  "openrouter",
  "custom_ingest",
];

export function CostKeysModal({
  lang,
  nowEpoch,
  onClose,
  onChanged,
}: CostKeysModalProps) {
  const tc = I18N[lang].costs;
  const [loading, setLoading] = useState(true);
  const [keys, setKeys] = useState<CostApiKeyOut[]>([]);
  const [budgetUsd, setBudgetUsd] = useState<string>("0");
  const [savingBudget, setSavingBudget] = useState(false);

  const [provider, setProvider] = useState<CostKeyProvider>("anthropic_admin");
  const [label, setLabel] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [addingKey, setAddingKey] = useState(false);
  const [deletingId, setDeletingId] = useState<number | null>(null);

  const [generatedSecret, setGeneratedSecret] = useState<string | null>(null);
  const [copiedSecret, setCopiedSecret] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    setLoading(true);
    api
      .listCostApiKeys()
      .then((res) => {
        if (!active) return;
        setKeys(res.keys ?? []);
        setBudgetUsd(String(res.monthly_budget_usd ?? 0));
      })
      .catch((err) => {
        if (!active) return;
        setError(err instanceof ApiError ? err.message : String(err));
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, []);

  async function handleSaveBudget(e: FormEvent) {
    e.preventDefault();
    setError(null);
    setNotice(null);
    setSavingBudget(true);
    try {
      const parsed = Math.max(0, Number(budgetUsd) || 0);
      const res = await api.updateCostBudget(parsed);
      setBudgetUsd(String(res.monthly_budget_usd));
      setNotice(tc.budgetSaved);
      onChanged();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setSavingBudget(false);
    }
  }

  async function handleAddKey(e: FormEvent) {
    e.preventDefault();
    if (!label.trim()) return;
    setError(null);
    setNotice(null);
    setGeneratedSecret(null);
    setAddingKey(true);
    try {
      const res = await api.createCostApiKey({
        provider,
        label: label.trim(),
        api_key: apiKey.trim(),
      });
      setKeys((prev) => [...prev, res.key]);
      setLabel("");
      setApiKey("");
      if (res.generated_secret) {
        setGeneratedSecret(res.generated_secret);
      }
      onChanged();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setAddingKey(false);
    }
  }

  async function handleDeleteKey(keyId: number) {
    setError(null);
    setDeletingId(keyId);
    try {
      await api.deleteCostApiKey(keyId);
      setKeys((prev) => prev.filter((k) => k.id !== keyId));
      onChanged();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setDeletingId(null);
    }
  }

  async function handleCopySecret(secret: string) {
    try {
      await navigator.clipboard.writeText(secret);
      setCopiedSecret(true);
      window.setTimeout(() => setCopiedSecret(false), 1800);
    } catch {
      // Clipboard API requires HTTPS or user gesture
    }
  }

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal-card" onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <div className="modal-title">{tc.modalTitle}</div>
          <button type="button" className="btn" onClick={onClose}>
            {tc.close}
          </button>
        </div>

        {error && <div className="perr">{error}</div>}
        {notice && <div className="alert-notice">{notice}</div>}

        {generatedSecret && (
          <div className="one-time-secret-box">
            <div className="wizard-step">{tc.oneTimeTokenTitle}</div>
            <div className="device-code-box" style={{ marginTop: 8 }}>
              <span className="mono-secret-text">{generatedSecret}</span>
              <button
                type="button"
                className="btn"
                onClick={() => handleCopySecret(generatedSecret)}
              >
                {copiedSecret ? tc.copiedToken : tc.copyTokenBtn}
              </button>
            </div>
          </div>
        )}

        {loading ? (
          <div className="empty-box">{tc.loadingKeys}</div>
        ) : (
          <div className="alert-form">
            <form onSubmit={handleSaveBudget} className="cost-modal-section">
              <div className="alert-section-title" style={{ borderTop: "none", paddingTop: 0 }}>
                {tc.budgetSection}
              </div>
              <div className="budget-inline-row">
                <div style={{ flex: 1 }}>
                  <label className="field-label">{tc.budgetField}</label>
                  <input
                    className="pixel-input"
                    type="number"
                    min={0}
                    max={1000000}
                    step="0.5"
                    value={budgetUsd}
                    onChange={(e) => setBudgetUsd(e.target.value)}
                  />
                </div>
                <button
                  type="submit"
                  className="btn primary"
                  style={{ height: 38 }}
                  disabled={savingBudget}
                >
                  {savingBudget ? tc.savingBudget : tc.saveBudgetBtn}
                </button>
              </div>
            </form>

            <div className="alert-section-title">{tc.keysSection}</div>
            {keys.length === 0 ? (
              <div className="conn-meta">{tc.noKeys}</div>
            ) : (
              <div className="conn-list">
                {keys.map((k) => {
                  const syncAge =
                    k.last_synced_at != null
                      ? tc.lastSynced(Math.max(0, nowEpoch - k.last_synced_at))
                      : tc.neverSynced;
                  return (
                    <div key={k.id} className="conn-card">
                      <div className="conn-top">
                        <div className="cost-key-head">
                          <span className="badge">
                            {tc.providerTypes[k.provider] || k.provider}
                          </span>
                          <span className="cost-key-label">{k.label}</span>
                          <code className="masked-key-pill">{k.masked_key}</code>
                        </div>
                        <button
                          type="button"
                          className="btn danger"
                          disabled={deletingId === k.id}
                          onClick={() => handleDeleteKey(k.id)}
                        >
                          {tc.deleteKeyBtn}
                        </button>
                      </div>
                      <div className="conn-meta">
                        <span>{syncAge}</span>
                        {k.last_error && (
                          <span style={{ color: "var(--err)" }}>{k.last_error}</span>
                        )}
                      </div>
                    </div>
                  );
                })}
              </div>
            )}

            <form onSubmit={handleAddKey} className="cost-modal-section">
              <div className="alert-section-title">{tc.addKeySection}</div>
              <div className="cost-key-form-grid">
                <div>
                  <label className="field-label">{tc.providerField}</label>
                  <select
                    className="pixel-input"
                    value={provider}
                    onChange={(e) => setProvider(e.target.value as CostKeyProvider)}
                  >
                    {KEY_PROVIDERS.map((p) => (
                      <option key={p} value={p}>
                        {tc.providerTypes[p] || p}
                      </option>
                    ))}
                  </select>
                </div>
                <div>
                  <label className="field-label">{tc.labelField}</label>
                  <input
                    className="pixel-input"
                    type="text"
                    required
                    maxLength={80}
                    placeholder={tc.labelPlaceholder}
                    value={label}
                    onChange={(e) => setLabel(e.target.value)}
                  />
                </div>
              </div>

              <div style={{ marginTop: 10 }}>
                <label className="field-label">{tc.secretField}</label>
                <input
                  className="pixel-input"
                  type={provider === "custom_ingest" ? "text" : "password"}
                  required={provider !== "custom_ingest"}
                  placeholder={
                    provider === "custom_ingest"
                      ? tc.customSecretPlaceholder
                      : tc.secretPlaceholder
                  }
                  value={apiKey}
                  onChange={(e) => setApiKey(e.target.value)}
                />
                {provider === "custom_ingest" && (
                  <div className="conn-meta" style={{ marginTop: 6 }}>
                    {tc.customSecretHint}
                  </div>
                )}
              </div>

              <div className="alert-actions-row" style={{ marginTop: 10 }}>
                <button
                  type="submit"
                  className="btn primary"
                  disabled={
                    addingKey ||
                    !label.trim() ||
                    (provider !== "custom_ingest" && apiKey.trim().length < 8)
                  }
                >
                  {addingKey ? tc.addingKey : tc.addKeyBtn}
                </button>
              </div>
            </form>
          </div>
        )}
      </div>
    </div>
  );
}
