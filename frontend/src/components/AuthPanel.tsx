import { FormEvent, useState } from "react";
import { api, ApiError, AuthSessionResponse } from "../api/client";
import { I18N, LangId } from "../i18n/translations";

interface AuthPanelProps {
  lang: LangId;
  allowRegistration: boolean;
  onAuthenticated: (session: AuthSessionResponse) => void;
  onEnterDemo: () => void;
}

export function AuthPanel({
  lang,
  allowRegistration,
  onAuthenticated,
  onEnterDemo,
}: AuthPanelProps) {
  const t = I18N[lang];
  const [mode, setMode] = useState<"login" | "register">("login");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    setBusy(true);
    try {
      const session =
        mode === "login"
          ? await api.login({ username, password })
          : await api.register({ username, password });
      onAuthenticated(session);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div>
      <div className="auth-header">
        <div className="auth-title">AI USAGE</div>
        <div className="auth-sub">{t.subtitle}</div>
      </div>

      {allowRegistration && (
        <div className="auth-tabs">
          <button
            type="button"
            className={`auth-tab ${mode === "login" ? "active" : ""}`}
            onClick={() => {
              setMode("login");
              setError(null);
            }}
          >
            {t.auth.loginTab}
          </button>
          <button
            type="button"
            className={`auth-tab ${mode === "register" ? "active" : ""}`}
            onClick={() => {
              setMode("register");
              setError(null);
            }}
          >
            {t.auth.registerTab}
          </button>
        </div>
      )}

      <form className="auth-form" onSubmit={handleSubmit}>
        <div>
          <label className="field-label" htmlFor="auth-username">
            {t.auth.username}
          </label>
          <input
            id="auth-username"
            className="pixel-input"
            type="text"
            autoComplete="username"
            required
            minLength={2}
            maxLength={64}
            placeholder={t.auth.usernamePlaceholder}
            value={username}
            onChange={(e) => setUsername(e.target.value)}
          />
        </div>

        <div>
          <label className="field-label" htmlFor="auth-password">
            {t.auth.password}
          </label>
          <input
            id="auth-password"
            className="pixel-input"
            type="password"
            autoComplete={mode === "login" ? "current-password" : "new-password"}
            required
            minLength={6}
            maxLength={128}
            placeholder={t.auth.passwordPlaceholder}
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
        </div>

        {error && <div className="perr">{error}</div>}

        <div className="auth-actions">
          <button type="submit" className="btn primary" disabled={busy}>
            {busy
              ? t.auth.submitting
              : mode === "login"
                ? t.auth.loginBtn
                : t.auth.registerBtn}
          </button>
          <button type="button" className="btn" onClick={onEnterDemo} disabled={busy}>
            {t.auth.demoBtn}
          </button>
        </div>
      </form>
    </div>
  );
}
