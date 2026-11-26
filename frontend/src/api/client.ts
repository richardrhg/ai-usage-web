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

export type CostSummaryResponse = components["schemas"]["CostSummaryResponse"];
export type CostDailyPointOut = components["schemas"]["CostDailyPointOut"];
export type CostGroupRowOut = components["schemas"]["CostGroupRowOut"];
export type CostTotalsOut = components["schemas"]["CostTotalsOut"];
export type MonthlyBudgetStatusOut = components["schemas"]["MonthlyBudgetStatusOut"];
export type CostDrilldownResponse = components["schemas"]["CostDrilldownResponse"];
export type CostDrilldownSubItemOut = components["schemas"]["CostDrilldownSubItemOut"];
export type CostDrilldownDayOut = components["schemas"]["CostDrilldownDayOut"];
export type CostApiKeyOut = components["schemas"]["CostApiKeyOut"];
export type CostApiKeyCreateRequest = components["schemas"]["CostApiKeyCreateRequest"];
export type CostApiKeyCreateResponse = components["schemas"]["CostApiKeyCreateResponse"];
export type CostApiKeyListResponse = components["schemas"]["CostApiKeyListResponse"];
export type CostApiKeyDeleteResponse = components["schemas"]["CostApiKeyDeleteResponse"];
export type CostBudgetUpdateRequest = components["schemas"]["CostBudgetUpdateRequest"];
export type CostSyncResponse = components["schemas"]["CostSyncResponse"];
export type CostIngestRequest = components["schemas"]["CostIngestRequest"];
export type CostIngestResponse = components["schemas"]["CostIngestResponse"];
export type CostDays = CostSummaryResponse["days"];
export type CostGroupBy = CostSummaryResponse["group_by"];
export type CostKeyProvider = CostApiKeyOut["provider"];

export interface CostSummaryQuery {
  days?: CostDays;
  group_by?: CostGroupBy;
  provider?: string | null;
  orderby?: string;
  top?: number;
  skip?: number;
  demo?: boolean;
}

export interface CostDrilldownQuery {
  group_by: CostGroupBy;
  group_value: string;
  days?: CostDays;
  provider?: string | null;
  demo?: boolean;
}

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

  /**
   * Single-shot O(1) cost aggregation endpoint: fetches window totals, monthly budget
   * & linear projection, daily provider-stacked series, and SQLite GROUP BY rows + total_count
   * in a single HTTP request.
   */
  getCostSummary(query: CostSummaryQuery = {}): Promise<CostSummaryResponse> {
    const params = new URLSearchParams();
    if (query.days != null) params.set("days", String(query.days));
    if (query.group_by) params.set("group_by", query.group_by);
    if (query.provider) params.set("provider", query.provider);
    if (query.orderby) params.set("orderby", query.orderby);
    if (query.top != null) params.set("top", String(query.top));
    if (query.skip != null) params.set("skip", String(query.skip));
    if (query.demo) params.set("demo", "true");
    const qs = params.toString();
    return requestJson<CostSummaryResponse>(`/api/costs/summary${qs ? `?${qs}` : ""}`);
  },

  /**
   * Lazy detail endpoint invoked strictly when the user clicks to expand one row.
   */
  getCostDrilldown(query: CostDrilldownQuery): Promise<CostDrilldownResponse> {
    const params = new URLSearchParams();
    params.set("group_by", query.group_by);
    params.set("group_value", query.group_value);
    if (query.days != null) params.set("days", String(query.days));
    if (query.provider) params.set("provider", query.provider);
    if (query.demo) params.set("demo", "true");
    return requestJson<CostDrilldownResponse>(`/api/costs/drilldown?${params.toString()}`);
  },

  syncCostUsage(query: Omit<CostSummaryQuery, "demo"> = {}): Promise<CostSyncResponse> {
    const params = new URLSearchParams();
    if (query.days != null) params.set("days", String(query.days));
    if (query.group_by) params.set("group_by", query.group_by);
    if (query.provider) params.set("provider", query.provider);
    if (query.orderby) params.set("orderby", query.orderby);
    if (query.top != null) params.set("top", String(query.top));
    if (query.skip != null) params.set("skip", String(query.skip));
    const qs = params.toString();
    return requestJson<CostSyncResponse>(`/api/costs/sync${qs ? `?${qs}` : ""}`, {
      method: "POST",
    });
  },

  listCostApiKeys(): Promise<CostApiKeyListResponse> {
    return requestJson<CostApiKeyListResponse>("/api/costs/keys");
  },

  createCostApiKey(payload: CostApiKeyCreateRequest): Promise<CostApiKeyCreateResponse> {
    return requestJson<CostApiKeyCreateResponse>("/api/costs/keys", {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },

  deleteCostApiKey(keyId: number): Promise<CostApiKeyDeleteResponse> {
    return requestJson<CostApiKeyDeleteResponse>(`/api/costs/keys/${keyId}`, {
      method: "DELETE",
    });
  },

  updateCostBudget(monthlyBudgetUsd: number): Promise<MonthlyBudgetStatusOut> {
    const body: CostBudgetUpdateRequest = {
      monthly_budget_usd: monthlyBudgetUsd,
    };
    return requestJson<MonthlyBudgetStatusOut>("/api/costs/budget", {
      method: "PUT",
      body: JSON.stringify(body),
    });
  },

  ingestCostRecords(payload: CostIngestRequest): Promise<CostIngestResponse> {
    return requestJson<CostIngestResponse>("/api/costs/ingest", {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },
};
