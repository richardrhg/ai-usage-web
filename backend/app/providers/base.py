from __future__ import annotations

from datetime import datetime
from typing import Any


class UpstreamUnauthorizedError(Exception):
    """Raised when an upstream quota endpoint rejects the current access_token with HTTP 401."""


class OAuthExchangeError(Exception):
    """Raised when interactive OAuth token exchange fails."""


def fmt_window(seconds: int | float | None) -> str | None:
    if not seconds:
        return None
    sec = int(seconds)
    if sec <= 0:
        return None
    if sec % 86400 == 0:
        return f"{sec // 86400}d"
    if sec % 3600 == 0:
        return f"{sec // 3600}h"
    return f"{round(sec / 3600)}h"


def to_epoch(raw: Any) -> int | None:
    if raw is None or raw == "":
        return None
    if isinstance(raw, (int, float)):
        val = int(raw / 1000) if raw > 10_000_000_000 else int(raw)
        return val if val > 0 else None
    text = str(raw).strip()
    if not text:
        return None
    if text.isdigit():
        return to_epoch(int(text))
    try:
        return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp())
    except Exception:
        return None


def clamp_pct(value: float | int) -> float:
    return round(max(0.0, min(100.0, float(value))), 1)
