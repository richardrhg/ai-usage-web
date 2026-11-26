from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from .analytics import HistorySample, make_window_key
from .crypto import CredentialCipher
from .schemas import (
    AlertEventOut,
    AlertSettingsPayload,
    AlertSettingsResponse,
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


class Database:
    def __init__(self, db_path: Path, cipher: CredentialCipher) -> None:
        self.db_path = db_path
        self.cipher = cipher
        self._init_schema()

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
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
                WHERE expires_at IS NULL OR expires_at <= ?
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
            if resets_at > 0:
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
