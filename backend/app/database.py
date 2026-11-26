from __future__ import annotations

import json
import sqlite3
import time
import calendar
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date as dt_date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence

from .analytics import HistorySample, make_window_key
from .crypto import CredentialCipher, generate_ingest_key, hash_session_token, mask_api_key
from .schemas import (
    AlertEventOut,
    AlertSettingsPayload,
    AlertSettingsResponse,
    CostApiKeyOut,
    CostDailyPointOut,
    CostDays,
    CostDrilldownDayOut,
    CostDrilldownResponse,
    CostDrilldownSubItemOut,
    CostGroupBy,
    CostGroupRowOut,
    CostKeyProvider,
    CostSubBreakdownKind,
    CostSummaryResponse,
    CostTotalsOut,
    MonthlyBudgetStatusOut,
    ProviderId,
    ProviderUsageOut,
    RefreshStatus,
)


@dataclass
class StoredCredential:
    user_id: int
    provider: ProviderId
    access_token: str
    refresh_token: str | None
    id_token: str | None
    account_id: str | None
    project_id: str | None
    account_label: str | None
    expires_at: int | None
    last_refreshed_at: int | None
    refresh_status: RefreshStatus
    refresh_error: str | None
    updated_at: int
    encrypted_payload: str = ""


@dataclass
class OAuthPendingFlow:
    state_id: str
    user_id: int
    provider: ProviderId
    flow_data: dict[str, Any]
    status: str
    error: str | None
    expires_at: int


@dataclass
class StoredCostApiKey:
    id: int
    user_id: int
    provider: CostKeyProvider
    label: str
    api_key: str
    masked_key: str
    enabled: bool
    last_synced_at: int | None
    last_error: str | None
    created_at: int
    updated_at: int


@dataclass(frozen=True)
class DailyCostRecord:
    usage_date: str
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    cost_usd: float


COST_PROVIDER_LABELS: dict[str, str] = {
    "anthropic": "Anthropic",
    "anthropic_admin": "Anthropic",
    "openai": "OpenAI",
    "openai_org": "OpenAI",
    "openrouter": "OpenRouter",
    "gemini": "Google Gemini",
    "custom": "Custom Ingest",
    "custom_ingest": "Custom Ingest",
}

COST_ORDER_BY_SQL: dict[str, str] = {
    "cost_usd desc": "cost_usd DESC, total_tokens DESC, group_key ASC",
    "cost_usd asc": "cost_usd ASC, total_tokens ASC, group_key ASC",
    "total_tokens desc": "total_tokens DESC, cost_usd DESC, group_key ASC",
    "total_tokens asc": "total_tokens ASC, cost_usd ASC, group_key ASC",
    "input_tokens desc": "input_tokens DESC, cost_usd DESC, group_key ASC",
    "output_tokens desc": "output_tokens DESC, cost_usd DESC, group_key ASC",
    "group_key asc": "group_key ASC",
    "group_key desc": "group_key DESC",
}


def format_cost_provider_label(provider: str) -> str:
    key = provider.strip().lower()
    return COST_PROVIDER_LABELS.get(key, provider.strip().capitalize() if provider.strip() else "Unknown")


def compute_monthly_budget_status(
    *,
    monthly_budget_usd: float,
    month_to_date_usd: float,
    today: dt_date,
) -> MonthlyBudgetStatusOut:
    year_month = f"{today.year:04d}-{today.month:02d}"
    days_in_month = calendar.monthrange(today.year, today.month)[1]
    elapsed_days = max(1, today.day)
    mtd = round(max(0.0, float(month_to_date_usd)), 4)
    projected = round(mtd * (days_in_month / elapsed_days), 4)
    budget = round(max(0.0, float(monthly_budget_usd)), 2)

    if budget > 0.0:
        util_pct = round((mtd / budget) * 100.0, 1)
        proj_pct = round((projected / budget) * 100.0, 1)
        effective_pct = max(util_pct, proj_pct)
        threshold: int | None = (
            100
            if effective_pct >= 100.0
            else 90
            if effective_pct >= 90.0
            else 75
            if effective_pct >= 75.0
            else None
        )
        return MonthlyBudgetStatusOut(
            year_month=year_month,
            monthly_budget_usd=budget,
            month_to_date_usd=mtd,
            projected_month_usd=projected,
            utilization_pct=util_pct,
            projected_pct=proj_pct,
            will_exceed_budget=effective_pct >= 100.0,
            active_threshold_pct=threshold,
        )

    return MonthlyBudgetStatusOut(
        year_month=year_month,
        monthly_budget_usd=0.0,
        month_to_date_usd=mtd,
        projected_month_usd=projected,
        utilization_pct=None,
        projected_pct=None,
        will_exceed_budget=False,
        active_threshold_pct=None,
    )


class Database:
    def __init__(self, db_path: Path, cipher: CredentialCipher) -> None:
        self.db_path = db_path
        self.cipher = cipher
        self.sql_trace_callback: Any | None = None
        self._init_schema()

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        if self.sql_trace_callback is not None:
            conn.set_trace_callback(self.sql_trace_callback)
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._conn() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    password_hash TEXT NOT NULL,
                    created_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    expires_at INTEGER NOT NULL,
                    created_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
                CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at);

                CREATE TABLE IF NOT EXISTS provider_credentials (
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    provider TEXT NOT NULL,
                    encrypted_payload TEXT NOT NULL,
                    account_label TEXT,
                    expires_at INTEGER,
                    last_refreshed_at INTEGER,
                    refresh_status TEXT NOT NULL DEFAULT 'ok',
                    refresh_error TEXT,
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY (user_id, provider)
                );
                CREATE INDEX IF NOT EXISTS idx_creds_expires ON provider_credentials(expires_at);

                CREATE TABLE IF NOT EXISTS oauth_pending_states (
                    state_id TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    provider TEXT NOT NULL,
                    encrypted_flow_data TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    error TEXT,
                    expires_at INTEGER NOT NULL,
                    created_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_oauth_states_user ON oauth_pending_states(user_id);

                CREATE TABLE IF NOT EXISTS usage_snapshots (
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    provider TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    fetched_at INTEGER NOT NULL,
                    PRIMARY KEY (user_id, provider)
                );

                CREATE TABLE IF NOT EXISTS usage_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    provider TEXT NOT NULL,
                    window_key TEXT NOT NULL,
                    window_label TEXT NOT NULL,
                    percent REAL NOT NULL,
                    reset_epoch INTEGER,
                    recorded_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_usage_history_lookup
                    ON usage_history(user_id, provider, recorded_at);

                CREATE TABLE IF NOT EXISTS alert_settings (
                    user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    encrypted_webhooks TEXT NOT NULL,
                    warn_threshold REAL NOT NULL DEFAULT 75.0,
                    critical_threshold REAL NOT NULL DEFAULT 90.0,
                    burn_rate_threshold_per_hour REAL NOT NULL DEFAULT 25.0,
                    notify_on_burn_rate INTEGER NOT NULL DEFAULT 1,
                    notify_on_reauth INTEGER NOT NULL DEFAULT 1,
                    updated_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS alert_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    provider TEXT NOT NULL,
                    window_key TEXT NOT NULL,
                    alert_level TEXT NOT NULL,
                    resets_at INTEGER NOT NULL DEFAULT 0,
                    message TEXT NOT NULL,
                    delivered_channels TEXT NOT NULL DEFAULT '[]',
                    created_at INTEGER NOT NULL,
                    UNIQUE(user_id, provider, window_key, alert_level, resets_at)
                );
                CREATE INDEX IF NOT EXISTS idx_alert_events_user_time
                    ON alert_events(user_id, created_at DESC);

                CREATE TABLE IF NOT EXISTS cost_api_keys (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    provider TEXT NOT NULL,
                    label TEXT NOT NULL,
                    encrypted_key TEXT NOT NULL,
                    masked_key TEXT NOT NULL,
                    key_hash TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    last_synced_at INTEGER,
                    last_error TEXT,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_cost_keys_user ON cost_api_keys(user_id);
                CREATE INDEX IF NOT EXISTS idx_cost_keys_hash ON cost_api_keys(key_hash);

                CREATE TABLE IF NOT EXISTS cost_daily_usage (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    key_id INTEGER NOT NULL REFERENCES cost_api_keys(id) ON DELETE CASCADE,
                    usage_date TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    cached_tokens INTEGER NOT NULL DEFAULT 0,
                    cost_usd REAL NOT NULL DEFAULT 0.0,
                    updated_at INTEGER NOT NULL,
                    UNIQUE(user_id, key_id, usage_date, provider, model)
                );
                CREATE INDEX IF NOT EXISTS idx_cost_daily_user_date
                    ON cost_daily_usage(user_id, usage_date, provider);
                CREATE INDEX IF NOT EXISTS idx_cost_daily_user_model
                    ON cost_daily_usage(user_id, model, usage_date);
                CREATE INDEX IF NOT EXISTS idx_cost_daily_user_key
                    ON cost_daily_usage(user_id, key_id, usage_date);

                CREATE TABLE IF NOT EXISTS cost_budgets (
                    user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                    monthly_budget_usd REAL NOT NULL DEFAULT 0.0,
                    updated_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS cost_budget_alerts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    year_month TEXT NOT NULL,
                    threshold_pct INTEGER NOT NULL,
                    trigger_kind TEXT NOT NULL,
                    actual_usd REAL NOT NULL,
                    projected_usd REAL NOT NULL,
                    budget_usd REAL NOT NULL,
                    message TEXT NOT NULL,
                    delivered_channels TEXT NOT NULL DEFAULT '[]',
                    created_at INTEGER NOT NULL,
                    UNIQUE(user_id, year_month, threshold_pct)
                );
                CREATE INDEX IF NOT EXISTS idx_cost_budget_alerts_lookup
                    ON cost_budget_alerts(user_id, year_month);
                """
            )

    # ── Users & Sessions ────────────────────────────────────────────────────
    def create_user(self, username: str, password_hash: str) -> dict[str, Any]:
        now = int(time.time())
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)",
                (username.strip(), password_hash, now),
            )
            return {"id": int(cur.lastrowid), "username": username.strip(), "created_at": now}

    def get_user_by_username(self, username: str) -> sqlite3.Row | None:
        with self._conn() as conn:
            return conn.execute(
                "SELECT id, username, password_hash, created_at FROM users WHERE username = ?",
                (username.strip(),),
            ).fetchone()

    def create_session(self, token_hash: str, user_id: int, ttl_seconds: int) -> None:
        now = int(time.time())
        with self._conn() as conn:
            conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (now,))
            conn.execute(
                "INSERT INTO sessions (token_hash, user_id, expires_at, created_at) VALUES (?, ?, ?, ?)",
                (token_hash, user_id, now + ttl_seconds, now),
            )

    def get_user_by_session(self, token_hash: str) -> dict[str, Any] | None:
        now = int(time.time())
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT u.id, u.username, u.created_at
                FROM sessions s
                JOIN users u ON u.id = s.user_id
                WHERE s.token_hash = ? AND s.expires_at > ?
                """,
                (token_hash, now),
            ).fetchone()
            if not row:
                return None
            return {"id": int(row["id"]), "username": str(row["username"]), "created_at": int(row["created_at"])}

    def delete_session(self, token_hash: str) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))

    # ── Encrypted Provider Credentials ──────────────────────────────────────
    def _row_to_credential(self, row: sqlite3.Row) -> StoredCredential:
        raw_encrypted = str(row["encrypted_payload"])
        secret = self.cipher.decrypt_json(raw_encrypted)
        return StoredCredential(
            user_id=int(row["user_id"]),
            provider=row["provider"],
            access_token=str(secret.get("access_token") or ""),
            refresh_token=secret.get("refresh_token"),
            id_token=secret.get("id_token"),
            account_id=secret.get("account_id"),
            project_id=secret.get("project_id"),
            account_label=row["account_label"],
            expires_at=int(row["expires_at"]) if row["expires_at"] is not None else None,
            last_refreshed_at=(
                int(row["last_refreshed_at"]) if row["last_refreshed_at"] is not None else None
            ),
            refresh_status=row["refresh_status"],
            refresh_error=row["refresh_error"],
            updated_at=int(row["updated_at"]),
            encrypted_payload=raw_encrypted,
        )

    def upsert_credential(
        self,
        *,
        user_id: int,
        provider: ProviderId,
        access_token: str,
        refresh_token: str | None,
        id_token: str | None = None,
        account_id: str | None = None,
        project_id: str | None = None,
        account_label: str | None = None,
        expires_at: int | None = None,
        last_refreshed_at: int | None = None,
        refresh_status: RefreshStatus = "ok",
        refresh_error: str | None = None,
    ) -> StoredCredential:
        now = int(time.time())
        encrypted = self.cipher.encrypt_json(
            {
                "access_token": access_token,
                "refresh_token": refresh_token,
                "id_token": id_token,
                "account_id": account_id,
                "project_id": project_id,
            }
        )
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO provider_credentials (
                    user_id, provider, encrypted_payload, account_label,
                    expires_at, last_refreshed_at, refresh_status, refresh_error, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id, provider) DO UPDATE SET
                    encrypted_payload = excluded.encrypted_payload,
                    account_label = excluded.account_label,
                    expires_at = excluded.expires_at,
                    last_refreshed_at = excluded.last_refreshed_at,
                    refresh_status = excluded.refresh_status,
                    refresh_error = excluded.refresh_error,
                    updated_at = excluded.updated_at
                """,
                (
                    user_id,
                    provider,
                    encrypted,
                    account_label,
                    expires_at,
                    last_refreshed_at,
                    refresh_status,
                    refresh_error,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM provider_credentials WHERE user_id = ? AND provider = ?",
                (user_id, provider),
            ).fetchone()
            assert row is not None
            return self._row_to_credential(row)

    def update_refreshed_credential(
        self,
        *,
        user_id: int,
        provider: ProviderId,
        expected_encrypted_payload: str,
        access_token: str,
        refresh_token: str | None,
        id_token: str | None = None,
        account_id: str | None = None,
        project_id: str | None = None,
        account_label: str | None = None,
        expires_at: int | None = None,
        last_refreshed_at: int | None = None,
        refresh_status: RefreshStatus = "ok",
        refresh_error: str | None = None,
    ) -> StoredCredential | None:
        now = int(time.time())
        encrypted = self.cipher.encrypt_json(
            {
                "access_token": access_token,
                "refresh_token": refresh_token,
                "id_token": id_token,
                "account_id": account_id,
                "project_id": project_id,
            }
        )
        with self._conn() as conn:
            cur = conn.execute(
                """
                UPDATE provider_credentials
                SET encrypted_payload = ?,
                    account_label = ?,
                    expires_at = ?,
                    last_refreshed_at = ?,
                    refresh_status = ?,
                    refresh_error = ?,
                    updated_at = ?
                WHERE user_id = ? AND provider = ? AND encrypted_payload = ?
                """,
                (
                    encrypted,
                    account_label,
                    expires_at,
                    last_refreshed_at,
                    refresh_status,
                    refresh_error,
                    now,
                    user_id,
                    provider,
                    expected_encrypted_payload,
                ),
            )
            if cur.rowcount == 0:
                return None
            row = conn.execute(
                "SELECT * FROM provider_credentials WHERE user_id = ? AND provider = ?",
                (user_id, provider),
            ).fetchone()
            return self._row_to_credential(row) if row else None

    def mark_credential_status(
        self,
        user_id: int,
        provider: ProviderId,
        *,
        refresh_status: RefreshStatus,
        refresh_error: str | None,
        expected_encrypted_payload: str | None = None,
    ) -> bool:
        now = int(time.time())
        with self._conn() as conn:
            if expected_encrypted_payload is not None:
                cur = conn.execute(
                    """
                    UPDATE provider_credentials
                    SET refresh_status = ?, refresh_error = ?, updated_at = ?
                    WHERE user_id = ? AND provider = ? AND encrypted_payload = ?
                    """,
                    (
                        refresh_status,
                        refresh_error,
                        now,
                        user_id,
                        provider,
                        expected_encrypted_payload,
                    ),
                )
            else:
                cur = conn.execute(
                    """
                    UPDATE provider_credentials
                    SET refresh_status = ?, refresh_error = ?, updated_at = ?
                    WHERE user_id = ? AND provider = ?
                    """,
                    (refresh_status, refresh_error, now, user_id, provider),
                )
            return cur.rowcount > 0

    def get_credential(self, user_id: int, provider: ProviderId) -> StoredCredential | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM provider_credentials WHERE user_id = ? AND provider = ?",
                (user_id, provider),
            ).fetchone()
            return self._row_to_credential(row) if row else None

    def list_user_credentials(self, user_id: int) -> dict[ProviderId, StoredCredential]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM provider_credentials WHERE user_id = ?",
                (user_id,),
            ).fetchall()
        out: dict[ProviderId, StoredCredential] = {}
        for row in rows:
            cred = self._row_to_credential(row)
            out[cred.provider] = cred
        return out

    def list_all_credential_keys(self) -> list[tuple[int, ProviderId]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT user_id, provider FROM provider_credentials WHERE refresh_status != 'expired'"
            ).fetchall()
        return [(int(r["user_id"]), r["provider"]) for r in rows]

    def list_expiring_credential_keys(self, threshold_epoch: int) -> list[tuple[int, ProviderId]]:
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT user_id, provider FROM provider_credentials
                WHERE (expires_at IS NULL OR expires_at <= ?)
                  AND refresh_status != 'expired'
                """,
                (threshold_epoch,),
            ).fetchall()
        return [(int(r["user_id"]), r["provider"]) for r in rows]

    def delete_credential(self, user_id: int, provider: ProviderId) -> bool:
        with self._conn() as conn:
            cur = conn.execute(
                "DELETE FROM provider_credentials WHERE user_id = ? AND provider = ?",
                (user_id, provider),
            )
            conn.execute(
                "DELETE FROM usage_snapshots WHERE user_id = ? AND provider = ?",
                (user_id, provider),
            )
            conn.execute(
                "DELETE FROM usage_history WHERE user_id = ? AND provider = ?",
                (user_id, provider),
            )
            return cur.rowcount > 0

    # ── Pending OAuth States ────────────────────────────────────────────────
    def create_oauth_state(
        self,
        *,
        state_id: str,
        user_id: int,
        provider: ProviderId,
        flow_data: dict[str, Any],
        ttl_seconds: int = 900,
    ) -> int:
        now = int(time.time())
        expires_at = now + ttl_seconds
        encrypted = self.cipher.encrypt_json(flow_data)
        with self._conn() as conn:
            conn.execute("DELETE FROM oauth_pending_states WHERE expires_at <= ?", (now,))
            conn.execute(
                """
                INSERT INTO oauth_pending_states (
                    state_id, user_id, provider, encrypted_flow_data, status, expires_at, created_at
                )
                VALUES (?, ?, ?, ?, 'pending', ?, ?)
                """,
                (state_id, user_id, provider, encrypted, expires_at, now),
            )
        return expires_at

    def get_oauth_state(
        self, state_id: str, user_id: int, provider: ProviderId
    ) -> OAuthPendingFlow | None:
        now = int(time.time())
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT * FROM oauth_pending_states
                WHERE state_id = ? AND user_id = ? AND provider = ? AND expires_at > ?
                """,
                (state_id, user_id, provider, now),
            ).fetchone()
            if not row:
                return None
            return OAuthPendingFlow(
                state_id=str(row["state_id"]),
                user_id=int(row["user_id"]),
                provider=row["provider"],
                flow_data=self.cipher.decrypt_json(str(row["encrypted_flow_data"])),
                status=str(row["status"]),
                error=row["error"],
                expires_at=int(row["expires_at"]),
            )

    def update_oauth_state_status(
        self, state_id: str, *, status: str, error: str | None = None
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE oauth_pending_states SET status = ?, error = ? WHERE state_id = ?",
                (status, error, state_id),
            )

    def delete_oauth_state(self, state_id: str) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM oauth_pending_states WHERE state_id = ?", (state_id,))

    # ── Cached Usage Snapshots (Single-Query Read per User) ─────────────────
    def upsert_usage_snapshot(
        self,
        user_id: int,
        usage: ProviderUsageOut,
        fetched_at: int,
        *,
        expected_encrypted_payload: str | None = None,
    ) -> bool:
        payload_json = usage.model_dump_json()
        with self._conn() as conn:
            if expected_encrypted_payload is not None:
                cur = conn.execute(
                    """
                    INSERT INTO usage_snapshots (user_id, provider, payload_json, fetched_at)
                    SELECT ?, ?, ?, ?
                    WHERE EXISTS (
                        SELECT 1 FROM provider_credentials
                        WHERE user_id = ? AND provider = ? AND encrypted_payload = ?
                    )
                    ON CONFLICT(user_id, provider) DO UPDATE SET
                        payload_json = excluded.payload_json,
                        fetched_at = excluded.fetched_at
                    """,
                    (
                        user_id,
                        usage.id,
                        payload_json,
                        fetched_at,
                        user_id,
                        usage.id,
                        expected_encrypted_payload,
                    ),
                )
            else:
                cur = conn.execute(
                    """
                    INSERT INTO usage_snapshots (user_id, provider, payload_json, fetched_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(user_id, provider) DO UPDATE SET
                        payload_json = excluded.payload_json,
                        fetched_at = excluded.fetched_at
                    """,
                    (user_id, usage.id, payload_json, fetched_at),
                )
            return cur.rowcount > 0

    def list_user_snapshots(self, user_id: int) -> tuple[dict[ProviderId, ProviderUsageOut], int | None]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT provider, payload_json, fetched_at FROM usage_snapshots WHERE user_id = ?",
                (user_id,),
            ).fetchall()
        snapshots: dict[ProviderId, ProviderUsageOut] = {}
        latest_fetched: int | None = None
        for row in rows:
            provider: ProviderId = row["provider"]
            snapshots[provider] = ProviderUsageOut.model_validate_json(str(row["payload_json"]))
            ts = int(row["fetched_at"])
            if latest_fetched is None or ts > latest_fetched:
                latest_fetched = ts
        return snapshots, latest_fetched

    # ── Time-Series Usage History (Indexed, Spacing-Limited & Auto-Pruned) ──
    def record_usage_history(
        self,
        user_id: int,
        usage: ProviderUsageOut,
        recorded_at: int,
        *,
        min_interval_seconds: int = 60,
        retention_days: int = 30,
        expected_encrypted_payload: str | None = None,
    ) -> int:
        if not usage.ok or not usage.windows:
            return 0

        prune_before = recorded_at - max(1, retention_days) * 86400
        inserted = 0
        with self._conn() as conn:
            if expected_encrypted_payload is not None:
                still_valid = conn.execute(
                    """
                    SELECT 1 FROM provider_credentials
                    WHERE user_id = ? AND provider = ? AND encrypted_payload = ?
                    """,
                    (user_id, usage.id, expected_encrypted_payload),
                ).fetchone()
                if not still_valid:
                    return 0

            conn.execute("DELETE FROM usage_history WHERE recorded_at < ?", (prune_before,))

            for win in usage.windows:
                wkey = make_window_key(win)
                if min_interval_seconds > 0:
                    last_row = conn.execute(
                        """
                        SELECT recorded_at, percent, reset_epoch
                        FROM usage_history
                        WHERE user_id = ? AND provider = ? AND window_key = ?
                        ORDER BY recorded_at DESC
                        LIMIT 1
                        """,
                        (user_id, usage.id, wkey),
                    ).fetchone()
                    if last_row is not None:
                        elapsed = recorded_at - int(last_row["recorded_at"])
                        prev_reset = (
                            int(last_row["reset_epoch"])
                            if last_row["reset_epoch"] is not None
                            else None
                        )
                        same_reset = (
                            prev_reset == win.reset_epoch
                            or (
                                prev_reset is not None
                                and win.reset_epoch is not None
                                and abs(prev_reset - win.reset_epoch) <= 600
                            )
                        )
                        reset_dropped = float(win.percent) + 5.0 < float(last_row["percent"])
                        if 0 <= elapsed < min_interval_seconds and same_reset and not reset_dropped:
                            continue

                conn.execute(
                    """
                    INSERT INTO usage_history (
                        user_id, provider, window_key, window_label, percent, reset_epoch, recorded_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        user_id,
                        usage.id,
                        wkey,
                        win.label,
                        float(win.percent),
                        win.reset_epoch,
                        recorded_at,
                    ),
                )
                inserted += 1
        return inserted

    def list_user_history_since(
        self,
        user_id: int,
        since_epoch: int,
    ) -> dict[ProviderId, dict[str, list[HistorySample]]]:
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT provider, window_key, percent, reset_epoch, recorded_at
                FROM usage_history
                WHERE user_id = ? AND recorded_at >= ?
                ORDER BY recorded_at ASC
                """,
                (user_id, since_epoch),
            ).fetchall()

        grouped: dict[ProviderId, dict[str, list[HistorySample]]] = {}
        for row in rows:
            prov: ProviderId = row["provider"]
            wkey = str(row["window_key"])
            sample = HistorySample(
                recorded_at=int(row["recorded_at"]),
                percent=float(row["percent"]),
                reset_epoch=int(row["reset_epoch"]) if row["reset_epoch"] is not None else None,
            )
            grouped.setdefault(prov, {}).setdefault(wkey, []).append(sample)
        return grouped

    # ── Proactive Webhook Alert Settings & Deduplicated Events ──────────────
    def list_recent_alert_events(self, user_id: int, limit: int = 10) -> list[AlertEventOut]:
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT id, provider, window_key, alert_level, resets_at, message, delivered_channels, created_at
                FROM alert_events
                WHERE user_id = ?
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (user_id, limit),
            ).fetchall()
        out: list[AlertEventOut] = []
        for row in rows:
            try:
                channels = json.loads(str(row["delivered_channels"] or "[]"))
                if not isinstance(channels, list):
                    channels = []
            except Exception:
                channels = []
            out.append(
                AlertEventOut(
                    id=int(row["id"]),
                    provider=str(row["provider"]),
                    window_key=str(row["window_key"]),
                    alert_level=str(row["alert_level"]),
                    resets_at=int(row["resets_at"]),
                    message=str(row["message"]),
                    delivered_channels=[str(c) for c in channels],
                    created_at=int(row["created_at"]),
                )
            )
        return out

    def get_alert_settings(self, user_id: int) -> AlertSettingsResponse:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM alert_settings WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        recent = self.list_recent_alert_events(user_id, limit=8)
        if row is None:
            return AlertSettingsResponse(recent_events=recent)

        hooks = self.cipher.decrypt_json(str(row["encrypted_webhooks"]))
        return AlertSettingsResponse(
            enabled=bool(row["enabled"]),
            discord_webhook_url=str(hooks.get("discord_webhook_url") or ""),
            slack_webhook_url=str(hooks.get("slack_webhook_url") or ""),
            telegram_webhook_url=str(hooks.get("telegram_webhook_url") or ""),
            telegram_chat_id=str(hooks.get("telegram_chat_id") or ""),
            warn_threshold=float(row["warn_threshold"]),
            critical_threshold=float(row["critical_threshold"]),
            burn_rate_threshold_per_hour=float(row["burn_rate_threshold_per_hour"]),
            notify_on_burn_rate=bool(row["notify_on_burn_rate"]),
            notify_on_reauth=bool(row["notify_on_reauth"]),
            updated_at=int(row["updated_at"]),
            recent_events=recent,
        )

    def upsert_alert_settings(
        self,
        user_id: int,
        payload: AlertSettingsPayload,
    ) -> AlertSettingsResponse:
        now = int(time.time())
        encrypted = self.cipher.encrypt_json(
            {
                "discord_webhook_url": payload.discord_webhook_url.strip(),
                "slack_webhook_url": payload.slack_webhook_url.strip(),
                "telegram_webhook_url": payload.telegram_webhook_url.strip(),
                "telegram_chat_id": payload.telegram_chat_id.strip(),
            }
        )
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO alert_settings (
                    user_id, enabled, encrypted_webhooks, warn_threshold, critical_threshold,
                    burn_rate_threshold_per_hour, notify_on_burn_rate, notify_on_reauth, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    enabled = excluded.enabled,
                    encrypted_webhooks = excluded.encrypted_webhooks,
                    warn_threshold = excluded.warn_threshold,
                    critical_threshold = excluded.critical_threshold,
                    burn_rate_threshold_per_hour = excluded.burn_rate_threshold_per_hour,
                    notify_on_burn_rate = excluded.notify_on_burn_rate,
                    notify_on_reauth = excluded.notify_on_reauth,
                    updated_at = excluded.updated_at
                """,
                (
                    user_id,
                    1 if payload.enabled else 0,
                    encrypted,
                    float(payload.warn_threshold),
                    float(payload.critical_threshold),
                    float(payload.burn_rate_threshold_per_hour),
                    1 if payload.notify_on_burn_rate else 0,
                    1 if payload.notify_on_reauth else 0,
                    now,
                ),
            )
        return self.get_alert_settings(user_id)

    def claim_alert_event(
        self,
        *,
        user_id: int,
        provider: str,
        window_key: str,
        alert_level: str,
        resets_at: int,
        message: str,
        created_at: int,
    ) -> int | None:
        prune_before = created_at - 90 * 86400
        with self._conn() as conn:
            conn.execute("DELETE FROM alert_events WHERE created_at < ?", (prune_before,))
            if resets_at > 0 and alert_level != "needs_reauth":
                near_dup = conn.execute(
                    """
                    SELECT id FROM alert_events
                    WHERE user_id = ? AND provider = ? AND window_key = ? AND alert_level = ?
                      AND resets_at > 0 AND ABS(resets_at - ?) <= 120
                    LIMIT 1
                    """,
                    (user_id, provider, window_key, alert_level, resets_at),
                ).fetchone()
                if near_dup is not None:
                    return None

            cur = conn.execute(
                """
                INSERT OR IGNORE INTO alert_events (
                    user_id, provider, window_key, alert_level, resets_at, message, delivered_channels, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, '[]', ?)
                """,
                (user_id, provider, window_key, alert_level, resets_at, message, created_at),
            )
            if cur.rowcount == 0 or cur.lastrowid is None:
                return None
            return int(cur.lastrowid)

    def finalize_alert_event_channels(self, event_id: int, channels: list[str]) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE alert_events SET delivered_channels = ? WHERE id = ?",
                (json.dumps(channels), event_id),
            )

    def delete_alert_event(self, event_id: int) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM alert_events WHERE id = ?", (event_id,))

    # ── Encrypted Cost API Keys & Monthly Budgets ───────────────────────────
    def _row_to_cost_key_out(self, row: sqlite3.Row) -> CostApiKeyOut:
        return CostApiKeyOut(
            id=int(row["id"]),
            provider=row["provider"],
            label=str(row["label"]),
            masked_key=str(row["masked_key"]),
            enabled=bool(row["enabled"]),
            last_synced_at=int(row["last_synced_at"]) if row["last_synced_at"] is not None else None,
            last_error=str(row["last_error"]) if row["last_error"] is not None else None,
            created_at=int(row["created_at"]),
        )

    def create_cost_api_key(
        self,
        *,
        user_id: int,
        provider: CostKeyProvider,
        label: str,
        raw_api_key: str = "",
    ) -> tuple[CostApiKeyOut, str | None]:
        now = int(time.time())
        cleaned_key = raw_api_key.strip()
        generated_secret: str | None = None
        if provider == "custom_ingest" and not cleaned_key:
            cleaned_key = generate_ingest_key()
            generated_secret = cleaned_key

        masked = mask_api_key(cleaned_key)
        khash = hash_session_token(cleaned_key)
        encrypted = self.cipher.encrypt_json({"api_key": cleaned_key})

        with self._conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO cost_api_keys (
                    user_id, provider, label, encrypted_key, masked_key, key_hash,
                    enabled, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (
                    user_id,
                    provider,
                    label.strip(),
                    encrypted,
                    masked,
                    khash,
                    now,
                    now,
                ),
            )
            key_id = int(cur.lastrowid)
            row = conn.execute(
                """
                SELECT id, provider, label, masked_key, enabled, last_synced_at, last_error, created_at
                FROM cost_api_keys
                WHERE id = ? AND user_id = ?
                """,
                (key_id, user_id),
            ).fetchone()
            assert row is not None
            return self._row_to_cost_key_out(row), generated_secret

    def list_cost_api_keys(self, user_id: int) -> list[CostApiKeyOut]:
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT id, provider, label, masked_key, enabled, last_synced_at, last_error, created_at
                FROM cost_api_keys
                WHERE user_id = ?
                ORDER BY id ASC
                """,
                (user_id,),
            ).fetchall()
        return [self._row_to_cost_key_out(r) for r in rows]

    def list_decrypted_cost_api_keys(self, user_id: int) -> list[StoredCostApiKey]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM cost_api_keys WHERE user_id = ? AND enabled = 1 ORDER BY id ASC",
                (user_id,),
            ).fetchall()
        out: list[StoredCostApiKey] = []
        for row in rows:
            secret = self.cipher.decrypt_json(str(row["encrypted_key"]))
            out.append(
                StoredCostApiKey(
                    id=int(row["id"]),
                    user_id=int(row["user_id"]),
                    provider=row["provider"],
                    label=str(row["label"]),
                    api_key=str(secret.get("api_key") or ""),
                    masked_key=str(row["masked_key"]),
                    enabled=bool(row["enabled"]),
                    last_synced_at=(
                        int(row["last_synced_at"]) if row["last_synced_at"] is not None else None
                    ),
                    last_error=str(row["last_error"]) if row["last_error"] is not None else None,
                    created_at=int(row["created_at"]),
                    updated_at=int(row["updated_at"]),
                )
            )
        return out

    def list_all_syncable_cost_users(self) -> list[int]:
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT user_id FROM cost_api_keys
                WHERE enabled = 1 AND provider != 'custom_ingest'
                """
            ).fetchall()
        return [int(r["user_id"]) for r in rows]

    def get_cost_api_key_meta(self, user_id: int, key_id: int) -> CostApiKeyOut | None:
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT id, provider, label, masked_key, enabled, last_synced_at, last_error, created_at
                FROM cost_api_keys
                WHERE id = ? AND user_id = ?
                """,
                (key_id, user_id),
            ).fetchone()
        return self._row_to_cost_key_out(row) if row else None

    def resolve_ingest_bearer_key(self, raw_token: str) -> tuple[int, int] | None:
        cleaned = raw_token.strip()
        if not cleaned:
            return None
        khash = hash_session_token(cleaned)
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT id, user_id, encrypted_key
                FROM cost_api_keys
                WHERE key_hash = ? AND provider = 'custom_ingest' AND enabled = 1
                """,
                (khash,),
            ).fetchall()
        for row in rows:
            secret = self.cipher.decrypt_json(str(row["encrypted_key"]))
            if secret.get("api_key") == cleaned:
                return int(row["user_id"]), int(row["id"])
        return None

    def get_or_create_default_ingest_key(self, user_id: int) -> int:
        with self._conn() as conn:
            existing = conn.execute(
                """
                SELECT id FROM cost_api_keys
                WHERE user_id = ? AND provider = 'custom_ingest'
                ORDER BY id ASC LIMIT 1
                """,
                (user_id,),
            ).fetchone()
            if existing is not None:
                return int(existing["id"])
        created, _ = self.create_cost_api_key(
            user_id=user_id,
            provider="custom_ingest",
            label="Default Telemetry Ingest",
            raw_api_key="",
        )
        return created.id

    def delete_cost_api_key(self, user_id: int, key_id: int) -> bool:
        with self._conn() as conn:
            cur = conn.execute(
                "DELETE FROM cost_api_keys WHERE id = ? AND user_id = ?",
                (key_id, user_id),
            )
            return cur.rowcount > 0

    def mark_cost_key_sync_status(
        self,
        key_id: int,
        *,
        synced_at: int | None,
        error: str | None,
    ) -> None:
        now = int(time.time())
        with self._conn() as conn:
            if synced_at is not None:
                conn.execute(
                    """
                    UPDATE cost_api_keys
                    SET last_synced_at = ?, last_error = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (synced_at, error, now, key_id),
                )
            else:
                conn.execute(
                    """
                    UPDATE cost_api_keys
                    SET last_error = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (error, now, key_id),
                )

    def get_monthly_budget(self, user_id: int) -> float:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT monthly_budget_usd FROM cost_budgets WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        return round(float(row["monthly_budget_usd"]), 2) if row is not None else 0.0

    def get_monthly_budget_status(
        self,
        user_id: int,
        *,
        today: dt_date | None = None,
    ) -> MonthlyBudgetStatusOut:
        ref_day = today or datetime.now(timezone.utc).date()
        year_month = f"{ref_day.year:04d}-{ref_day.month:02d}"
        days_in_month = calendar.monthrange(ref_day.year, ref_day.month)[1]
        month_start = f"{year_month}-01"
        month_end = f"{year_month}-{days_in_month:02d}"
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT
                    (SELECT monthly_budget_usd FROM cost_budgets WHERE user_id = ?) AS budget_usd,
                    (SELECT COALESCE(SUM(cost_usd), 0.0)
                     FROM cost_daily_usage
                     WHERE user_id = ? AND usage_date >= ? AND usage_date <= ?) AS mtd_usd
                """,
                (user_id, user_id, month_start, month_end),
            ).fetchone()
        budget_usd = float(row["budget_usd"]) if row and row["budget_usd"] is not None else 0.0
        mtd_usd = float(row["mtd_usd"]) if row and row["mtd_usd"] is not None else 0.0
        return compute_monthly_budget_status(
            monthly_budget_usd=budget_usd,
            month_to_date_usd=mtd_usd,
            today=ref_day,
        )

    def upsert_monthly_budget(
        self,
        user_id: int,
        monthly_budget_usd: float,
        *,
        today: dt_date | None = None,
    ) -> MonthlyBudgetStatusOut:
        now = int(time.time())
        clean_budget = round(max(0.0, float(monthly_budget_usd)), 2)
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO cost_budgets (user_id, monthly_budget_usd, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    monthly_budget_usd = excluded.monthly_budget_usd,
                    updated_at = excluded.updated_at
                """,
                (user_id, clean_budget, now),
            )
        return self.get_monthly_budget_status(user_id, today=today)

    # ── Idempotent Daily Usage Upsert & Single-Query GROUP BY Aggregations ──
    def upsert_daily_cost_records(
        self,
        *,
        user_id: int,
        key_id: int,
        records: Sequence[DailyCostRecord],
    ) -> int:
        if not records:
            return 0

        # Coalesce multiple line-items on the same (usage_date, provider, model) within a batch
        merged: dict[tuple[str, str, str], tuple[int, int, int, float]] = {}
        for rec in records:
            bucket_key = (
                rec.usage_date.strip(),
                rec.provider.strip().lower(),
                rec.model.strip(),
            )
            prev = merged.get(bucket_key)
            if prev is None:
                merged[bucket_key] = (
                    max(0, int(rec.input_tokens)),
                    max(0, int(rec.output_tokens)),
                    max(0, int(rec.cached_tokens)),
                    round(max(0.0, float(rec.cost_usd)), 6),
                )
            else:
                merged[bucket_key] = (
                    prev[0] + max(0, int(rec.input_tokens)),
                    prev[1] + max(0, int(rec.output_tokens)),
                    prev[2] + max(0, int(rec.cached_tokens)),
                    round(prev[3] + max(0.0, float(rec.cost_usd)), 6),
                )

        now = int(time.time())
        params = [
            (
                user_id,
                key_id,
                udate,
                prov,
                model,
                inp,
                outp,
                cached,
                cost,
                now,
            )
            for (udate, prov, model), (inp, outp, cached, cost) in merged.items()
        ]
        with self._conn() as conn:
            conn.executemany(
                """
                INSERT INTO cost_daily_usage (
                    user_id, key_id, usage_date, provider, model,
                    input_tokens, output_tokens, cached_tokens, cost_usd, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id, key_id, usage_date, provider, model) DO UPDATE SET
                    input_tokens = excluded.input_tokens,
                    output_tokens = excluded.output_tokens,
                    cached_tokens = excluded.cached_tokens,
                    cost_usd = excluded.cost_usd,
                    updated_at = excluded.updated_at
                """,
                params,
            )
            conn.execute(
                "UPDATE cost_api_keys SET last_synced_at = ?, last_error = NULL, updated_at = ? WHERE id = ? AND user_id = ?",
                (now, now, key_id, user_id),
            )
        return len(params)

    def query_cost_summary(
        self,
        user_id: int,
        *,
        days: CostDays = 30,
        group_by: CostGroupBy = "provider",
        provider_filter: str | None = None,
        orderby: str = "cost_usd desc",
        top: int = 25,
        skip: int = 0,
        sync_cooldown_seconds: int = 0,
        sync_cooldown_remaining: int = 0,
        today: dt_date | None = None,
    ) -> CostSummaryResponse:
        ref_day = today or datetime.now(timezone.utc).date()
        start_day = ref_day - timedelta(days=int(days) - 1)
        start_date = start_day.isoformat()
        end_date = ref_day.isoformat()

        norm_provider = (
            provider_filter.strip().lower()
            if provider_filter and provider_filter.strip().lower() not in {"", "all"}
            else None
        )
        norm_order = orderby.strip().lower() if orderby.strip() else "cost_usd desc"
        order_sql = COST_ORDER_BY_SQL.get(norm_order)
        if order_sql is None:
            raise ValueError(
                f"不支援的 orderby 參數 ({orderby})，允許值：{', '.join(COST_ORDER_BY_SQL.keys())}"
            )

        safe_top = max(1, min(100, int(top)))
        safe_skip = max(0, int(skip))

        year_month = f"{ref_day.year:04d}-{ref_day.month:02d}"
        days_in_month = calendar.monthrange(ref_day.year, ref_day.month)[1]
        month_start = f"{year_month}-01"
        month_end = f"{year_month}-{days_in_month:02d}"

        if group_by == "provider":
            select_group_cols = """
                u.provider AS group_key,
                u.provider AS provider,
                NULL AS key_id,
                NULL AS key_label,
                NULL AS masked_key
            """
            group_expr = "u.provider"
        elif group_by == "model":
            select_group_cols = """
                u.model AS group_key,
                CASE WHEN COUNT(DISTINCT u.provider) = 1 THEN MAX(u.provider) ELSE 'multi' END AS provider,
                NULL AS key_id,
                NULL AS key_label,
                NULL AS masked_key
            """
            group_expr = "u.model"
        else:
            select_group_cols = """
                CAST(u.key_id AS TEXT) AS group_key,
                COALESCE(MAX(k.provider), MAX(u.provider)) AS provider,
                u.key_id AS key_id,
                MAX(k.label) AS key_label,
                MAX(k.masked_key) AS masked_key
            """
            group_expr = "u.key_id"

        with self._conn() as conn:
            # Statement 1 (O(1)): Scalar metadata (budget, MTD spend, bound keys, available providers)
            meta_row = conn.execute(
                """
                SELECT
                    (SELECT monthly_budget_usd FROM cost_budgets WHERE user_id = ?) AS budget_usd,
                    (SELECT COALESCE(SUM(cost_usd), 0.0)
                     FROM cost_daily_usage
                     WHERE user_id = ? AND usage_date >= ? AND usage_date <= ?) AS mtd_usd,
                    (SELECT COUNT(*) FROM cost_api_keys WHERE user_id = ?) AS bound_keys_count,
                    (SELECT MAX(last_synced_at) FROM cost_api_keys WHERE user_id = ?) AS last_synced_at,
                    (SELECT GROUP_CONCAT(DISTINCT provider)
                     FROM cost_daily_usage
                     WHERE user_id = ? AND usage_date >= ? AND usage_date <= ?) AS avail_providers
                """,
                (
                    user_id,
                    user_id,
                    month_start,
                    month_end,
                    user_id,
                    user_id,
                    user_id,
                    start_date,
                    end_date,
                ),
            ).fetchone()

            # Statement 2 (O(1)): Single GROUP BY (usage_date, provider) -> window totals + stacked daily series
            daily_rows = conn.execute(
                """
                SELECT
                    usage_date,
                    provider,
                    COALESCE(SUM(input_tokens), 0) AS input_tokens,
                    COALESCE(SUM(output_tokens), 0) AS output_tokens,
                    COALESCE(SUM(cached_tokens), 0) AS cached_tokens,
                    COALESCE(SUM(cost_usd), 0.0) AS cost_usd
                FROM cost_daily_usage
                WHERE user_id = ? AND usage_date >= ? AND usage_date <= ?
                  AND (? IS NULL OR provider = ?)
                GROUP BY usage_date, provider
                ORDER BY usage_date ASC, provider ASC
                """,
                (user_id, start_date, end_date, norm_provider, norm_provider),
            ).fetchall()

            # Statement 3 (O(1)): Single GROUP BY with window COUNT(*) OVER() -> paginated groups + total_count
            group_rows = conn.execute(
                f"""
                SELECT
                    {select_group_cols},
                    COALESCE(SUM(u.input_tokens), 0) AS input_tokens,
                    COALESCE(SUM(u.output_tokens), 0) AS output_tokens,
                    COALESCE(SUM(u.cached_tokens), 0) AS cached_tokens,
                    COALESCE(SUM(u.input_tokens + u.output_tokens + u.cached_tokens), 0) AS total_tokens,
                    COALESCE(SUM(u.cost_usd), 0.0) AS cost_usd,
                    COUNT(DISTINCT u.usage_date) AS record_days,
                    COUNT(*) OVER() AS total_groups
                FROM cost_daily_usage u
                LEFT JOIN cost_api_keys k ON k.id = u.key_id AND k.user_id = u.user_id
                WHERE u.user_id = ? AND u.usage_date >= ? AND u.usage_date <= ?
                  AND (? IS NULL OR u.provider = ?)
                GROUP BY {group_expr}
                ORDER BY {order_sql}
                LIMIT ? OFFSET ?
                """,
                (
                    user_id,
                    start_date,
                    end_date,
                    norm_provider,
                    norm_provider,
                    safe_top,
                    safe_skip,
                ),
            ).fetchall()

            total_count = int(group_rows[0]["total_groups"]) if group_rows else 0
            if not group_rows and safe_skip > 0:
                cnt_row = conn.execute(
                    f"""
                    SELECT COUNT(DISTINCT {group_expr}) AS cnt
                    FROM cost_daily_usage u
                    WHERE u.user_id = ? AND u.usage_date >= ? AND u.usage_date <= ?
                      AND (? IS NULL OR u.provider = ?)
                    """,
                    (user_id, start_date, end_date, norm_provider, norm_provider),
                ).fetchone()
                total_count = int(cnt_row["cnt"]) if cnt_row else 0

        # Build continuous daily series & overall window totals from Statement 2
        day_map: dict[str, CostDailyPointOut] = {}
        for offset in range(int(days)):
            d_iso = (start_day + timedelta(days=offset)).isoformat()
            day_map[d_iso] = CostDailyPointOut(date=d_iso)

        win_cost = 0.0
        win_inp = 0
        win_out = 0
        win_cached = 0
        for drow in daily_rows:
            d_iso = str(drow["usage_date"])
            prov = str(drow["provider"])
            c_usd = float(drow["cost_usd"])
            inp_t = int(drow["input_tokens"])
            out_t = int(drow["output_tokens"])
            cac_t = int(drow["cached_tokens"])

            win_cost += c_usd
            win_inp += inp_t
            win_out += out_t
            win_cached += cac_t

            pt = day_map.get(d_iso)
            if pt is not None:
                pt.total_cost_usd = round(pt.total_cost_usd + c_usd, 4)
                pt.input_tokens += inp_t
                pt.output_tokens += out_t
                pt.cached_tokens += cac_t
                pt.by_provider[prov] = round(pt.by_provider.get(prov, 0.0) + c_usd, 4)

        win_total_tokens = win_inp + win_out + win_cached
        totals_out = CostTotalsOut(
            cost_usd=round(win_cost, 4),
            input_tokens=win_inp,
            output_tokens=win_out,
            cached_tokens=win_cached,
            total_tokens=win_total_tokens,
        )

        groups_out: list[CostGroupRowOut] = []
        for grow in group_rows:
            gkey = str(grow["group_key"])
            prov_val = str(grow["provider"]) if grow["provider"] is not None else None
            kid = int(grow["key_id"]) if grow["key_id"] is not None else None
            masked = str(grow["masked_key"]) if grow["masked_key"] is not None else None
            klabel = str(grow["key_label"]) if grow["key_label"] is not None else None

            if group_by == "provider":
                glabel = format_cost_provider_label(gkey)
            elif group_by == "model":
                glabel = gkey
            else:
                base_lbl = klabel or f"Key #{gkey}"
                glabel = f"{base_lbl} ({masked})" if masked else base_lbl

            row_cost = round(float(grow["cost_usd"]), 4)
            row_tot_tok = int(grow["total_tokens"])
            if win_cost > 0.0:
                share = round((row_cost / win_cost) * 100.0, 1)
            elif win_total_tokens > 0:
                share = round((row_tot_tok / win_total_tokens) * 100.0, 1)
            else:
                share = 0.0

            groups_out.append(
                CostGroupRowOut(
                    group_key=gkey,
                    group_label=glabel,
                    provider=prov_val,
                    key_id=kid,
                    masked_key=masked,
                    input_tokens=int(grow["input_tokens"]),
                    output_tokens=int(grow["output_tokens"]),
                    cached_tokens=int(grow["cached_tokens"]),
                    total_tokens=row_tot_tok,
                    cost_usd=row_cost,
                    share_pct=min(100.0, max(0.0, share)),
                    record_days=int(grow["record_days"]),
                )
            )

        budget_usd = (
            float(meta_row["budget_usd"])
            if meta_row and meta_row["budget_usd"] is not None
            else 0.0
        )
        mtd_usd = (
            float(meta_row["mtd_usd"]) if meta_row and meta_row["mtd_usd"] is not None else 0.0
        )
        budget_status = compute_monthly_budget_status(
            monthly_budget_usd=budget_usd,
            month_to_date_usd=mtd_usd,
            today=ref_day,
        )

        raw_avail = (
            str(meta_row["avail_providers"])
            if meta_row and meta_row["avail_providers"]
            else ""
        )
        avail_list = sorted({p.strip() for p in raw_avail.split(",") if p.strip()})

        return CostSummaryResponse(
            days=days,
            start_date=start_date,
            end_date=end_date,
            group_by=group_by,
            provider_filter=norm_provider,
            orderby=norm_order,
            top=safe_top,
            skip=safe_skip,
            total_count=total_count,
            totals=totals_out,
            budget=budget_status,
            daily_series=list(day_map.values()),
            groups=groups_out,
            available_providers=avail_list,
            bound_keys_count=int(meta_row["bound_keys_count"]) if meta_row else 0,
            last_synced_at=(
                int(meta_row["last_synced_at"])
                if meta_row and meta_row["last_synced_at"] is not None
                else None
            ),
            sync_cooldown_seconds=sync_cooldown_seconds,
            sync_cooldown_remaining=sync_cooldown_remaining,
            is_demo=False,
        )

    def query_cost_drilldown(
        self,
        user_id: int,
        *,
        group_by: CostGroupBy,
        group_value: str,
        days: CostDays = 30,
        provider_filter: str | None = None,
        today: dt_date | None = None,
    ) -> CostDrilldownResponse:
        ref_day = today or datetime.now(timezone.utc).date()
        start_date = (ref_day - timedelta(days=int(days) - 1)).isoformat()
        end_date = ref_day.isoformat()
        norm_provider = (
            provider_filter.strip().lower()
            if provider_filter and provider_filter.strip().lower() not in {"", "all"}
            else None
        )
        cleaned_val = group_value.strip()

        sub_kind: CostSubBreakdownKind
        if group_by == "provider":
            target_clause = "u.provider = ?"
            target_param: str | int = cleaned_val.lower()
            group_label = format_cost_provider_label(cleaned_val)
            sub_kind = "model"
            sub_sql = """
                SELECT
                    u.model AS sub_key,
                    u.model AS sub_label,
                    MAX(u.provider) AS provider,
                    COALESCE(SUM(u.input_tokens), 0) AS input_tokens,
                    COALESCE(SUM(u.output_tokens), 0) AS output_tokens,
                    COALESCE(SUM(u.cached_tokens), 0) AS cached_tokens,
                    COALESCE(SUM(u.input_tokens + u.output_tokens + u.cached_tokens), 0) AS total_tokens,
                    COALESCE(SUM(u.cost_usd), 0.0) AS cost_usd
                FROM cost_daily_usage u
                WHERE u.user_id = ? AND u.usage_date >= ? AND u.usage_date <= ?
                  AND (? IS NULL OR u.provider = ?)
                  AND u.provider = ?
                GROUP BY u.model
                ORDER BY cost_usd DESC, total_tokens DESC
            """
        elif group_by == "model":
            target_clause = "u.model = ?"
            target_param = cleaned_val
            group_label = cleaned_val
            sub_kind = "key"
            sub_sql = """
                SELECT
                    CAST(u.key_id AS TEXT) AS sub_key,
                    COALESCE(MAX(k.label), 'Key #' || CAST(u.key_id AS TEXT)) AS sub_label,
                    MAX(k.masked_key) AS masked_key,
                    MAX(u.provider) AS provider,
                    COALESCE(SUM(u.input_tokens), 0) AS input_tokens,
                    COALESCE(SUM(u.output_tokens), 0) AS output_tokens,
                    COALESCE(SUM(u.cached_tokens), 0) AS cached_tokens,
                    COALESCE(SUM(u.input_tokens + u.output_tokens + u.cached_tokens), 0) AS total_tokens,
                    COALESCE(SUM(u.cost_usd), 0.0) AS cost_usd
                FROM cost_daily_usage u
                LEFT JOIN cost_api_keys k ON k.id = u.key_id AND k.user_id = u.user_id
                WHERE u.user_id = ? AND u.usage_date >= ? AND u.usage_date <= ?
                  AND (? IS NULL OR u.provider = ?)
                  AND u.model = ?
                GROUP BY u.key_id
                ORDER BY cost_usd DESC, total_tokens DESC
            """
        else:
            try:
                target_param = int(cleaned_val)
            except ValueError as exc:
                raise ValueError("當 group_by=key 時，group_value 必須為整數金鑰 ID") from exc
            target_clause = "u.key_id = ?"
            group_label = f"Key #{target_param}"
            sub_kind = "model"
            sub_sql = """
                SELECT
                    u.model AS sub_key,
                    u.model AS sub_label,
                    MAX(u.provider) AS provider,
                    COALESCE(SUM(u.input_tokens), 0) AS input_tokens,
                    COALESCE(SUM(u.output_tokens), 0) AS output_tokens,
                    COALESCE(SUM(u.cached_tokens), 0) AS cached_tokens,
                    COALESCE(SUM(u.input_tokens + u.output_tokens + u.cached_tokens), 0) AS total_tokens,
                    COALESCE(SUM(u.cost_usd), 0.0) AS cost_usd
                FROM cost_daily_usage u
                WHERE u.user_id = ? AND u.usage_date >= ? AND u.usage_date <= ?
                  AND (? IS NULL OR u.provider = ?)
                  AND u.key_id = ?
                GROUP BY u.model
                ORDER BY cost_usd DESC, total_tokens DESC
            """

        with self._conn() as conn:
            if group_by == "key":
                krow = conn.execute(
                    "SELECT label, masked_key FROM cost_api_keys WHERE id = ? AND user_id = ?",
                    (target_param, user_id),
                ).fetchone()
                if krow is not None:
                    group_label = f"{krow['label']} ({krow['masked_key']})"

            sub_rows = conn.execute(
                sub_sql,
                (user_id, start_date, end_date, norm_provider, norm_provider, target_param),
            ).fetchall()

            day_rows = conn.execute(
                f"""
                SELECT
                    u.usage_date,
                    COALESCE(SUM(u.input_tokens), 0) AS input_tokens,
                    COALESCE(SUM(u.output_tokens), 0) AS output_tokens,
                    COALESCE(SUM(u.cached_tokens), 0) AS cached_tokens,
                    COALESCE(SUM(u.input_tokens + u.output_tokens + u.cached_tokens), 0) AS total_tokens,
                    COALESCE(SUM(u.cost_usd), 0.0) AS cost_usd
                FROM cost_daily_usage u
                WHERE u.user_id = ? AND u.usage_date >= ? AND u.usage_date <= ?
                  AND (? IS NULL OR u.provider = ?)
                  AND {target_clause}
                GROUP BY u.usage_date
                ORDER BY u.usage_date DESC
                """,
                (user_id, start_date, end_date, norm_provider, norm_provider, target_param),
            ).fetchall()

        total_cost = round(sum(float(r["cost_usd"]) for r in sub_rows), 4)
        total_tok = sum(int(r["total_tokens"]) for r in sub_rows)

        sub_out: list[CostDrilldownSubItemOut] = []
        for srow in sub_rows:
            scost = round(float(srow["cost_usd"]), 4)
            stok = int(srow["total_tokens"])
            lbl = str(srow["sub_label"])
            if sub_kind == "key" and "masked_key" in srow.keys() and srow["masked_key"]:
                lbl = f"{lbl} ({srow['masked_key']})"
            if total_cost > 0.0:
                share = round((scost / total_cost) * 100.0, 1)
            elif total_tok > 0:
                share = round((stok / total_tok) * 100.0, 1)
            else:
                share = 0.0
            sub_out.append(
                CostDrilldownSubItemOut(
                    key=str(srow["sub_key"]),
                    label=lbl,
                    provider=str(srow["provider"]) if srow["provider"] is not None else None,
                    input_tokens=int(srow["input_tokens"]),
                    output_tokens=int(srow["output_tokens"]),
                    cached_tokens=int(srow["cached_tokens"]),
                    total_tokens=stok,
                    cost_usd=scost,
                    share_pct=min(100.0, max(0.0, share)),
                )
            )

        days_out = [
            CostDrilldownDayOut(
                date=str(drow["usage_date"]),
                input_tokens=int(drow["input_tokens"]),
                output_tokens=int(drow["output_tokens"]),
                cached_tokens=int(drow["cached_tokens"]),
                total_tokens=int(drow["total_tokens"]),
                cost_usd=round(float(drow["cost_usd"]), 4),
            )
            for drow in day_rows
        ]

        return CostDrilldownResponse(
            group_by=group_by,
            group_value=cleaned_val,
            group_label=group_label,
            days=days,
            total_cost_usd=total_cost,
            total_tokens=total_tok,
            sub_breakdown_kind=sub_kind,
            sub_breakdown=sub_out,
            daily_breakdown=days_out,
            is_demo=False,
        )

    # ── Monthly Budget Alert Deduplication (user_id, year_month, threshold_pct) ──
    def claim_cost_budget_alert(
        self,
        *,
        user_id: int,
        year_month: str,
        threshold_pct: int,
        trigger_kind: str,
        actual_usd: float,
        projected_usd: float,
        budget_usd: float,
        message: str,
        created_at: int,
    ) -> int | None:
        with self._conn() as conn:
            max_row = conn.execute(
                """
                SELECT MAX(threshold_pct) AS max_pct
                FROM cost_budget_alerts
                WHERE user_id = ? AND year_month = ?
                """,
                (user_id, year_month),
            ).fetchone()
            if max_row is not None and max_row["max_pct"] is not None:
                if int(max_row["max_pct"]) >= int(threshold_pct):
                    return None

            cur = conn.execute(
                """
                INSERT OR IGNORE INTO cost_budget_alerts (
                    user_id, year_month, threshold_pct, trigger_kind,
                    actual_usd, projected_usd, budget_usd, message, delivered_channels, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, '[]', ?)
                """,
                (
                    user_id,
                    year_month,
                    int(threshold_pct),
                    trigger_kind,
                    round(actual_usd, 4),
                    round(projected_usd, 4),
                    round(budget_usd, 2),
                    message,
                    created_at,
                ),
            )
            if cur.rowcount == 0 or cur.lastrowid is None:
                return None
            return int(cur.lastrowid)

    def finalize_cost_budget_alert_channels(
        self,
        *,
        alert_id: int,
        user_id: int,
        year_month: str,
        threshold_pct: int,
        message: str,
        channels: list[str],
        created_at: int,
    ) -> None:
        channels_json = json.dumps(channels)
        with self._conn() as conn:
            conn.execute(
                "UPDATE cost_budget_alerts SET delivered_channels = ? WHERE id = ?",
                (channels_json, alert_id),
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO alert_events (
                    user_id, provider, window_key, alert_level, resets_at, message, delivered_channels, created_at
                )
                VALUES (?, 'cost_budget', ?, ?, 0, ?, ?, ?)
                """,
                (
                    user_id,
                    f"budget:{year_month}",
                    f"budget_{threshold_pct}",
                    message,
                    channels_json,
                    created_at,
                ),
            )

    def delete_cost_budget_alert(self, alert_id: int) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM cost_budget_alerts WHERE id = ?", (alert_id,))
