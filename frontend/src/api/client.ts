import type { components } from "../types/api/schema";

export type AuthCredentialsRequest = components["schemas"]["AuthCredentialsRequest"];
export type AuthSessionResponse = components["schemas"]["AuthSessionResponse"];
export type DashboardUsageResponse = components["schemas"]["DashboardUsageResponse"];
export type ProviderUsageOut = components["schemas"]["ProviderUsageOut"];
export type ProviderConnectionOut = components["schemas"]["ProviderConnectionOut"];
export type UsageWindowOut = components["schemas"]["UsageWindowOut"];
export type TrendPointOut = components["schemas"]["TrendPointOut"];
export type OAuthStartResponse = components["schemas"]["OAuthStartResponse"];
export type OAuthFlowStatusResponse = components["schemas"]["OAuthFlowStatusResponse"];
export type DisconnectResponse = components["schemas"]["DisconnectResponse"];
export type AlertSettingsPayload = components["schemas"]["AlertSettingsPayload"];
export type AlertSettingsResponse = components["schemas"]["AlertSettingsResponse"];
export type AlertTestRequest = components["schemas"]["AlertTestRequest"];
export type AlertTestResponse = components["schemas"]["AlertTestResponse"];
export type AlertEventOut = components["schemas"]["AlertEventOut"];
export type ProviderId = ProviderConnectionOut["provider"];

export class ApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function requestJson<T>(url: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(url, {
    credentials: "same-origin",
    ...init,
    headers: {
      Accept: "application/json",
      ...(init?.body ? { "Content-Type": "application/json" } : {}),
      ...(init?.headers || {}),
    },
  });

  if (!resp.ok) {
    let detail = `HTTP ${resp.status}`;
    try {
      const errBody = (await resp.json()) as { detail?: unknown };
      if (typeof errBody.detail === "string") {
        detail = errBody.detail;
      } else if (Array.isArray(errBody.detail) && errBody.detail.length > 0) {
        const first = errBody.detail[0] as { msg?: string };
        if (first?.msg) detail = first.msg;
      }
    } catch {
      // Ignore JSON parse failure on non-JSON error responses
    }
    throw new ApiError(resp.status, detail);
  }

  return (await resp.json()) as T;
}

export const api = {
  getAuthSession(): Promise<AuthSessionResponse> {
    return requestJson<AuthSessionResponse>("/api/auth/me");
  },

  login(payload: AuthCredentialsRequest): Promise<AuthSessionResponse> {
    return requestJson<AuthSessionResponse>("/api/auth/login", {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },

  register(payload: AuthCredentialsRequest): Promise<AuthSessionResponse> {
    return requestJson<AuthSessionResponse>("/api/auth/register", {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },

  logout(): Promise<AuthSessionResponse> {
    return requestJson<AuthSessionResponse>("/api/auth/logout", {
      method: "POST",
    });
  },

  /**
   * Single-shot O(1) dashboard endpoint: fetches all bound providers' usage windows,
   * downsampled 24h sparklines, burn-rate exhaustion predictions, AND connection statuses
   * in a single HTTP request (never per-item N+1).
   */
  getDashboardUsage(demo = false, refresh = false): Promise<DashboardUsageResponse> {
    const params = new URLSearchParams();
    if (demo) params.set("demo", "true");
    if (refresh) params.set("refresh", "true");
    const qs = params.toString();
    return requestJson<DashboardUsageResponse>(`/api/usage${qs ? `?${qs}` : ""}`);
  },

  refreshDashboardUsage(): Promise<DashboardUsageResponse> {
    return requestJson<DashboardUsageResponse>("/api/usage?refresh=true");
  },

  startProviderOAuth(provider: ProviderId): Promise<OAuthStartResponse> {
    return requestJson<OAuthStartResponse>(`/api/oauth/${provider}/start`, {
      method: "POST",
    });
  },

  completeProviderOAuth(
    provider: ProviderId,
    stateId: string,
    codeInput: string
  ): Promise<OAuthFlowStatusResponse> {
    const body: components["schemas"]["OAuthCodeSubmitRequest"] = {
      state_id: stateId,
      code_input: codeInput,
    };
    return requestJson<OAuthFlowStatusResponse>(`/api/oauth/${provider}/complete`, {
      method: "POST",
      body: JSON.stringify(body),
    });
  },

  pollCodexDeviceOAuth(stateId: string): Promise<OAuthFlowStatusResponse> {
    const body: components["schemas"]["OAuthDevicePollRequest"] = {
      state_id: stateId,
    };
    return requestJson<OAuthFlowStatusResponse>("/api/oauth/codex/poll", {
      method: "POST",
      body: JSON.stringify(body),
    });
  },

  disconnectProviderOAuth(provider: ProviderId): Promise<DisconnectResponse> {
    return requestJson<DisconnectResponse>(`/api/oauth/${provider}`, {
      method: "DELETE",
    });
  },

  getAlertSettings(): Promise<AlertSettingsResponse> {
    return requestJson<AlertSettingsResponse>("/api/alerts/settings");
  },

  updateAlertSettings(payload: AlertSettingsPayload): Promise<AlertSettingsResponse> {
    return requestJson<AlertSettingsResponse>("/api/alerts/settings", {
      method: "PUT",
      body: JSON.stringify(payload),
    });
  },

  sendTestAlert(payload?: AlertTestRequest): Promise<AlertTestResponse> {
    return requestJson<AlertTestResponse>("/api/alerts/test", {
      method: "POST",
      body: JSON.stringify(payload ?? {}),
    });
  },
};
