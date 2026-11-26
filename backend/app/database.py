from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from .crypto import CredentialCipher
from .schemas import ProviderId, ProviderUsageOut, RefreshStatus


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
        secret = self.cipher.decrypt_json(str(row["encrypted_payload"]))
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

    def mark_credential_status(
        self,
        user_id: int,
        provider: ProviderId,
        *,
        refresh_status: RefreshStatus,
        refresh_error: str | None,
    ) -> None:
        now = int(time.time())
        with self._conn() as conn:
            conn.execute(
                """
                UPDATE provider_credentials
                SET refresh_status = ?, refresh_error = ?, updated_at = ?
                WHERE user_id = ? AND provider = ?
                """,
                (refresh_status, refresh_error, now, user_id, provider),
            )

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
    def upsert_usage_snapshot(self, user_id: int, usage: ProviderUsageOut, fetched_at: int) -> None:
        payload_json = usage.model_dump_json()
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO usage_snapshots (user_id, provider, payload_json, fetched_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id, provider) DO UPDATE SET
                    payload_json = excluded.payload_json,
                    fetched_at = excluded.fetched_at
                """,
                (user_id, usage.id, payload_json, fetched_at),
            )

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
