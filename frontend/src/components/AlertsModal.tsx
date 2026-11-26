import { FormEvent, useEffect, useState } from "react";
import {
  AlertEventOut,
  AlertSettingsPayload,
  api,
  ApiError,
} from "../api/client";
import { I18N, LangId } from "../i18n/translations";

interface AlertsModalProps {
  lang: LangId;
  onClose: () => void;
}

const DEFAULT_FORM: AlertSettingsPayload = {
  enabled: true,
  discord_webhook_url: "",
  slack_webhook_url: "",
  telegram_webhook_url: "",
  telegram_chat_id: "",
  warn_threshold: 75,
  critical_threshold: 90,
  burn_rate_threshold_per_hour: 25,
  notify_on_burn_rate: true,
  notify_on_reauth: true,
};

export function AlertsModal({ lang, onClose }: AlertsModalProps) {
  const t = I18N[lang].alerts;
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [form, setForm] = useState<AlertSettingsPayload>(DEFAULT_FORM);
  const [recentEvents, setRecentEvents] = useState<AlertEventOut[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    setLoading(true);
    api
      .getAlertSettings()
      .then((res) => {
        if (!active) return;
        setForm({
          enabled: res.enabled,
          discord_webhook_url: res.discord_webhook_url,
          slack_webhook_url: res.slack_webhook_url,
          telegram_webhook_url: res.telegram_webhook_url,
          telegram_chat_id: res.telegram_chat_id,
          warn_threshold: res.warn_threshold,
          critical_threshold: res.critical_threshold,
          burn_rate_threshold_per_hour: res.burn_rate_threshold_per_hour,
          notify_on_burn_rate: res.notify_on_burn_rate,
          notify_on_reauth: res.notify_on_reauth,
        });
        setRecentEvents(res.recent_events ?? []);
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

  async function handleSave(e: FormEvent) {
    e.preventDefault();
    setError(null);
    setNotice(null);
    setSaving(true);
    try {
      const saved = await api.updateAlertSettings(form);
      setRecentEvents(saved.recent_events ?? []);
      setNotice(t.savedMsg);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setSaving(false);
    }
  }

  async function handleTestPing() {
    setError(null);
    setNotice(null);
    setTesting(true);
    try {
      const res = await api.sendTestAlert({
        discord_webhook_url: form.discord_webhook_url,
        slack_webhook_url: form.slack_webhook_url,
        telegram_webhook_url: form.telegram_webhook_url,
        telegram_chat_id: form.telegram_chat_id,
      });
      if (res.ok && res.delivered_channels && res.delivered_channels.length > 0) {
        setNotice(t.testOk(res.delivered_channels.join(", ")));
      } else {
        const errPairs = Object.entries(res.errors ?? {}).map(
          ([ch, msg]) => `${ch}: ${msg}`
        );
        setError(errPairs.join(" | ") || I18N[lang].unknown);
      }
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setTesting(false);
    }
  }

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal-card" onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <div className="modal-title">{t.modalTitle}</div>
          <button type="button" className="btn" onClick={onClose}>
            {t.close}
          </button>
        </div>

        {error && <div className="perr">{error}</div>}
        {notice && <div className="alert-notice">{notice}</div>}

        {loading ? (
          <div className="empty-box">{t.loading}</div>
        ) : (
          <form className="alert-form" onSubmit={handleSave}>
            <label className="check-row">
              <input
                type="checkbox"
                checked={form.enabled}
                onChange={(e) =>
                  setForm((prev) => ({ ...prev, enabled: e.target.checked }))
                }
              />
              <span>{t.enabledLabel}</span>
            </label>

            <div className="alert-section-title">{t.thresholdsTitle}</div>
            <div className="threshold-grid">
              <div>
                <label className="field-label">{t.warnThreshold}</label>
                <input
                  className="pixel-input"
                  type="number"
                  min={1}
                  max={99}
                  step={1}
                  value={form.warn_threshold}
                  onChange={(e) =>
                    setForm((prev) => ({
                      ...prev,
                      warn_threshold: Number(e.target.value),
                    }))
                  }
                />
              </div>
              <div>
                <label className="field-label">{t.critThreshold}</label>
                <input
                  className="pixel-input"
                  type="number"
                  min={1}
                  max={100}
                  step={1}
                  value={form.critical_threshold}
                  onChange={(e) =>
                    setForm((prev) => ({
                      ...prev,
                      critical_threshold: Number(e.target.value),
                    }))
                  }
                />
              </div>
              <div>
                <label className="field-label">{t.burnThreshold}</label>
                <input
                  className="pixel-input"
                  type="number"
                  min={1}
                  max={500}
                  step={1}
                  value={form.burn_rate_threshold_per_hour}
                  onChange={(e) =>
                    setForm((prev) => ({
                      ...prev,
                      burn_rate_threshold_per_hour: Number(e.target.value),
                    }))
                  }
                />
              </div>
            </div>

            <label className="check-row">
              <input
                type="checkbox"
                checked={form.notify_on_burn_rate}
                onChange={(e) =>
                  setForm((prev) => ({
                    ...prev,
                    notify_on_burn_rate: e.target.checked,
                  }))
                }
              />
              <span>{t.notifyBurn}</span>
            </label>

            <label className="check-row">
              <input
                type="checkbox"
                checked={form.notify_on_reauth}
                onChange={(e) =>
                  setForm((prev) => ({
                    ...prev,
                    notify_on_reauth: e.target.checked,
                  }))
                }
              />
              <span>{t.notifyReauth}</span>
            </label>

            <div className="alert-section-title">{t.channelsTitle}</div>
            <div>
              <label className="field-label">{t.discordLabel}</label>
              <input
                className="pixel-input"
                type="url"
                placeholder={t.discordPlaceholder}
                value={form.discord_webhook_url}
                onChange={(e) =>
                  setForm((prev) => ({
                    ...prev,
                    discord_webhook_url: e.target.value,
                  }))
                }
              />
            </div>

            <div>
              <label className="field-label">{t.slackLabel}</label>
              <input
                className="pixel-input"
                type="url"
                placeholder={t.slackPlaceholder}
                value={form.slack_webhook_url}
                onChange={(e) =>
                  setForm((prev) => ({
                    ...prev,
                    slack_webhook_url: e.target.value,
                  }))
                }
              />
            </div>

            <div className="telegram-grid">
              <div>
                <label className="field-label">{t.telegramLabel}</label>
                <input
                  className="pixel-input"
                  type="url"
                  placeholder={t.telegramPlaceholder}
                  value={form.telegram_webhook_url}
                  onChange={(e) =>
                    setForm((prev) => ({
                      ...prev,
                      telegram_webhook_url: e.target.value,
                    }))
                  }
                />
              </div>
              <div>
                <label className="field-label">{t.telegramChatLabel}</label>
                <input
                  className="pixel-input"
                  type="text"
                  placeholder={t.telegramChatPlaceholder}
                  value={form.telegram_chat_id}
                  onChange={(e) =>
                    setForm((prev) => ({
                      ...prev,
                      telegram_chat_id: e.target.value,
                    }))
                  }
                />
              </div>
            </div>

            <div className="alert-actions-row">
              <button type="submit" className="btn primary" disabled={saving || testing}>
                {saving ? t.saving : t.saveBtn}
              </button>
              <button
                type="button"
                className="btn"
                disabled={saving || testing}
                onClick={handleTestPing}
              >
                {testing ? t.testing : t.testBtn}
              </button>
            </div>

            <div className="alert-section-title">{t.recentTitle}</div>
            {recentEvents.length === 0 ? (
              <div className="conn-meta">{t.noRecent}</div>
            ) : (
              <div className="recent-events-list">
                {recentEvents.map((ev) => (
                  <div key={ev.id} className="recent-event-item">
                    <div className="recent-event-msg">{ev.message}</div>
                    <div className="conn-meta">
                      <span className="badge">{ev.alert_level}</span>
                      {ev.delivered_channels && ev.delivered_channels.length > 0 && (
                        <span>{ev.delivered_channels.join(", ")}</span>
                      )}
                    </div>
                  </div>
                ))}
              </div>
            )}
          </form>
        )}
      </div>
    </div>
  );
}
