from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATA_DIR = REPO_ROOT / "data"


def _load_or_create_secret_key(data_dir: Path) -> str:
    env_key = os.environ.get("APP_SECRET_KEY", "").strip()
    if env_key:
        return env_key
    data_dir.mkdir(parents=True, exist_ok=True)
    key_file = data_dir / ".secret_key"
    if key_file.exists():
        existing = key_file.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    generated = secrets.token_urlsafe(48)
    key_file.write_text(generated, encoding="utf-8")
    try:
        os.chmod(key_file, 0o600)
    except OSError:
        pass
    return generated


@dataclass(frozen=True)
class Settings:
    db_path: Path
    secret_key: str
    poll_interval_seconds: int = 120
    refresh_cooldown_seconds: int = 30
    proactive_refresh_buffer_seconds: int = 300
    session_ttl_seconds: int = 30 * 86400
    cookie_name: str = "ai_usage_session"
    cookie_secure: bool = False
    allow_registration: bool = True
    enable_background_poller: bool = True
    frontend_dist_dir: Path = REPO_ROOT / "frontend" / "dist"
    claude_client_id: str = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
    codex_client_id: str = "app_EMoamEEZ73f0CkXaXp7hrann"
    codex_issuer: str = "https://auth.openai.com"
    gemini_client_id: str = ""
    gemini_client_secret: str = ""


def load_settings() -> Settings:
    data_dir = Path(os.environ.get("AI_USAGE_DATA_DIR", str(DEFAULT_DATA_DIR))).resolve()
    db_path = Path(os.environ.get("AI_USAGE_DB_PATH", str(data_dir / "ai_usage.db"))).resolve()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return Settings(
        db_path=db_path,
        secret_key=_load_or_create_secret_key(db_path.parent),
        poll_interval_seconds=max(30, int(os.environ.get("POLL_INTERVAL_SECONDS", "120"))),
        refresh_cooldown_seconds=max(5, int(os.environ.get("REFRESH_COOLDOWN_SECONDS", "30"))),
        proactive_refresh_buffer_seconds=max(
            60, int(os.environ.get("PROACTIVE_REFRESH_BUFFER_SECONDS", "300"))
        ),
        session_ttl_seconds=max(3600, int(os.environ.get("SESSION_TTL_SECONDS", str(30 * 86400)))),
        cookie_secure=os.environ.get("COOKIE_SECURE", "0").strip().lower() in {"1", "true", "yes"},
        allow_registration=os.environ.get("ALLOW_REGISTRATION", "1").strip().lower()
        not in {"0", "false", "no"},
        enable_background_poller=os.environ.get("ENABLE_BACKGROUND_POLLER", "1").strip().lower()
        not in {"0", "false", "no"},
        claude_client_id=os.environ.get(
            "CLAUDE_OAUTH_CLIENT_ID", "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
        ).strip(),
        codex_client_id=os.environ.get(
            "CODEX_OAUTH_CLIENT_ID", "app_EMoamEEZ73f0CkXaXp7hrann"
        ).strip(),
        codex_issuer=os.environ.get("CODEX_OAUTH_ISSUER", "https://auth.openai.com").strip().rstrip("/"),
        gemini_client_id=os.environ.get("GEMINI_OAUTH_CLIENT_ID", "").strip(),
        gemini_client_secret=os.environ.get("GEMINI_OAUTH_CLIENT_SECRET", "").strip(),
    )
